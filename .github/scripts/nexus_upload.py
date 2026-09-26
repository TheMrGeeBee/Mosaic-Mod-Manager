#!/usr/bin/env python3
"""Upload a file as a new version of an existing Nexus Mods file slot.

Replaces Nexus-Mods/upload-action for release.yml. Same flow (multipart upload,
finalise, POST /mod-files/{id}/versions, optional changelog), plus one thing the
action cannot do: it names the version being replaced (``previous_version_id``)
so ``archive_existing_file`` has something to archive. Sent without it, the
previous Main file was left as "Old version" and had to be archived by hand.

Stdlib only (the runner needs no pip install). The API key comes from the
NEXUSMODS_API_KEY environment variable, never from argv. NEXUSMODS_API_BASE
overrides the API root (tests point it at a local mock server).

Exit status is non-zero only when the upload itself fails. A failed
"was the old version archived?" check is a ::warning:: — the file is already live.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path

API_BASE = os.environ.get("NEXUSMODS_API_BASE", "").strip() or "https://api.nexusmods.com/v3"
USER_AGENT = "Mosaic-Mod-Manager/release-workflow"
RETRIES = 4


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, what: str):
        super().__init__(f"{what}: HTTP {status} - {body[:600]}")
        self.status = status


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"::warning::{msg}", flush=True)


def _request(req: urllib.request.Request, what: str) -> tuple[int, dict, bytes]:
    """One HTTP round trip with retries on network errors and 5xx/429."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code < 500 and exc.code != 429:
                raise ApiError(exc.code, body, what) from None
            last = ApiError(exc.code, body, what)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        if attempt < RETRIES - 1:
            delay = 2 * 3 ** attempt
            log(f"  {what} failed ({last}); retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError(f"{what} failed after {RETRIES} attempts: {last}")


class Nexus:
    def __init__(self, api_key: str):
        self._key = api_key

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            API_BASE + path, data=data, method=method,
            headers={"Content-Type": "application/json", "apikey": self._key,
                     "User-Agent": USER_AGENT})
        _, _, raw = _request(req, f"{method} {path}")
        return json.loads(raw) if raw else {}


def _put_part(url: str, chunk: bytes, number: int, total: int) -> dict:
    log(f"  part {number}/{total} ({len(chunk)} bytes)")
    req = urllib.request.Request(
        url, data=chunk, method="PUT",
        headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(chunk))})
    _, headers, _ = _request(req, f"upload part {number}")
    etag = next((v for k, v in headers.items() if k.lower() == "etag"), None)
    if not etag:
        raise RuntimeError(f"no ETag returned for part {number}")
    return {"partNumber": number, "etag": etag.replace('"', "")}


def upload_file(api: Nexus, path: Path) -> str:
    """Multipart-upload *path*; return the upload id once Nexus has processed it."""
    size = path.stat().st_size
    log(f"Uploading {path.name} ({size} bytes)")
    info = api.call("POST", "/uploads/multipart",
                    {"filename": path.name, "size_bytes": str(size)})["data"]
    upload_id, urls, part_size = info["id"], info["part_presigned_urls"], info["part_size_bytes"]

    def one(item: tuple[int, str]) -> dict:
        number, url = item
        with open(path, "rb") as fh:
            fh.seek((number - 1) * part_size)
            return _put_part(url, fh.read(part_size), number, len(urls))

    with ThreadPoolExecutor(max_workers=4) as pool:
        parts = list(pool.map(one, enumerate(urls, start=1)))

    xml = "<CompleteMultipartUpload>\n" + "\n".join(
        f"  <Part>\n    <PartNumber>{p['partNumber']}</PartNumber>\n"
        f"    <ETag>{p['etag']}</ETag>\n  </Part>" for p in parts
    ) + "\n</CompleteMultipartUpload>"
    _request(urllib.request.Request(
        info["complete_presigned_url"], data=xml.encode(), method="POST",
        headers={"Content-Type": "application/xml"}), "complete multipart upload")

    api.call("POST", f"/uploads/{upload_id}/finalise")
    delay = 2.0
    for _ in range(60):
        state = api.call("GET", f"/uploads/{upload_id}")["data"]["state"]
        log(f"  upload {upload_id}: {state}")
        if state == "available":
            return upload_id
        time.sleep(min(delay, 30))
        delay *= 1.5
    raise RuntimeError(f"upload {upload_id} still not available after polling")


def _position(version: dict) -> Decimal:
    try:
        return Decimal(str(version.get("position")))
    except InvalidOperation:
        return Decimal(-1)


def find_previous(versions: list[dict]) -> dict | None:
    """The version a new upload replaces: the newest Main file of the slot, else
    the newest one that is not already archived/removed."""
    for wanted in (lambda v: v.get("category") == "main",
                   lambda v: v.get("category") not in ("archived", "removed")):
        candidates = [v for v in versions if wanted(v)]
        if candidates:
            return max(candidates, key=_position)
    return None


def list_versions(api: Nexus, file_id: str) -> list[dict]:
    return api.call("GET", f"/mod-files/{file_id}/versions")["data"]["versions"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file-id", required=True, help="Nexus file slot id")
    ap.add_argument("--file", required=True, type=Path, help="zip to upload")
    ap.add_argument("--name", required=True, help="display name of the file version")
    ap.add_argument("--version", required=True)
    ap.add_argument("--category", default="main")
    ap.add_argument("--allow-mod-manager-download", choices=("true", "false"), default="false")
    ap.add_argument("--archive-existing", action="store_true",
                    help="archive the version this one replaces")
    ap.add_argument("--update-mod-version", action="store_true")
    ap.add_argument("--mod-id", default="", help="mod uid; needed with --changelog-file")
    ap.add_argument("--changelog-file", type=Path, default=None)
    args = ap.parse_args(argv)

    key = os.environ.get("NEXUSMODS_API_KEY", "").strip()
    if not key:
        log("NEXUSMODS_API_KEY is not set")
        return 2
    if not args.file.is_file():
        log(f"File not found: {args.file}")
        return 2
    changelog = ""
    if args.changelog_file and args.changelog_file.is_file():
        changelog = args.changelog_file.read_text(encoding="utf-8").strip()
    if changelog and not args.mod_id:
        log("--mod-id is required to add a changelog")
        return 2

    api = Nexus(key)

    previous = None
    if args.archive_existing:
        try:
            previous = find_previous(list_versions(api, args.file_id))
        except Exception as exc:                          # noqa: BLE001
            warn(f"Could not look up the version being replaced ({exc}); "
                 "it may end up as Old instead of Archived")
        if previous:
            log(f"Replacing {previous['version']} (id {previous['id']}, "
                f"category {previous['category']})")

    upload_id = upload_file(api, args.file)

    body = {
        "upload_id": upload_id,
        "name": args.name,
        "version": args.version,
        "file_category": args.category,
        "allow_mod_manager_download": args.allow_mod_manager_download == "true",
        "update_mod_version": args.update_mod_version,
        "archive_existing_file": args.archive_existing,
    }
    if previous:
        body["previous_version_id"] = previous["id"]
    path = f"/mod-files/{args.file_id}/versions"
    try:
        created = api.call("POST", path, body)
    except ApiError as exc:
        # The upload is already on Nexus; a 4xx caused by previous_version_id must not
        # cost the release. Retry without it (worst case: the old file lands as Old).
        if "previous_version_id" not in body or exc.status >= 500:
            raise
        warn(f"Nexus refused previous_version_id ({exc}); retrying without it")
        body.pop("previous_version_id")
        created = api.call("POST", path, body)
    new_id = (created.get("data", {}).get("version") or {}).get("id")
    log(f"Created version {args.version} (id {new_id})")

    if changelog:
        api.call("POST", f"/mods/{args.mod_id}/changelogs",
                 {"version": args.version, "changelog": changelog})
        log("Changelog added")

    # Report what Nexus actually did, so a wrong guess shows up in the run log
    # instead of being discovered on the Files tab.
    if previous:
        try:
            after = {v["id"]: v for v in list_versions(api, args.file_id)}
            state = after.get(previous["id"], {}).get("category", "missing")
            if state == "archived":
                log(f"Verified: previous version {previous['version']} is Archived")
            else:
                warn(f"Previous version {previous['version']} is '{state}', not archived "
                     "— archive it by hand on the Files tab")
        except Exception as exc:                          # noqa: BLE001
            warn(f"Could not verify the archive state ({exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

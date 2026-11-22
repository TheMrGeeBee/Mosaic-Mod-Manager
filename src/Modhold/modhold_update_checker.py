"""
modhold_update_checker.py
Find installed Modhold mods that have a newer file.

Reading a mod needs no key, so this works for anyone with Modhold mods installed.
``/stats`` carries no version, so each distinct mod is read once (``get_mod``,
four at a time through the client's rate limiter) and every installed folder of
that mod is compared with its *counterpart* in the mod's current file list:

1. the file with the same id (still current), else
2. the file with the same label, else
3. the only file of the same kind (``main``/``optional``).

A counterpart with a higher id is an update (Modhold's file ids only grow, so a
lower one is a rollback, not something to "update" to). The same id with a
different SHA-256 is an update too: the author replaced the file in place. A mod
whose installed file has no counterpart left is reported as unknown.

The result is written back to meta.ini (``modholdLatest*``); the modlist reads
those keys to paint the update flag.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from Modhold.modhold_api import ModholdAPI, ModholdError, ModholdNotFound
from Modhold.modhold_meta import ModholdMeta, read_modhold_meta, write_modhold_meta
from Utils.app_log import app_log

ProgressCallback = Callable[[str], None]

_WORKERS = 4
_ID_RE = re.compile(r"(?im)^\s*modholdModId\s*=\s*([1-9]\d*)\s*$")


@dataclass
class ModholdUpdateInfo:
    """One installed mod folder's update status."""
    mod_name: str = ""              # staging folder name
    mod_id: int = 0
    installed_file_id: int = 0
    installed_version: str = ""
    latest_file_id: int = 0
    latest_version: str = ""
    has_update: bool = False
    # The installed file is gone from the mod and nothing replaces it, so the
    # state is genuinely unknown rather than up to date.
    unknown: bool = False


def has_modhold_mods(staging_root: Path) -> bool:
    """Cheap pre-check: does any mod folder carry a Modhold id? Reads text, no parsing."""
    try:
        for folder in staging_root.iterdir():
            meta = folder / "meta.ini"
            if not meta.is_file():
                continue
            try:
                if _ID_RE.search(meta.read_text(encoding="utf-8", errors="ignore")):
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def find_counterpart(installed: ModholdMeta, files: list[dict]) -> Optional[dict]:
    """The file in *files* that is the current form of the installed file, or None."""
    for f in files:
        if int(f.get("id") or 0) == installed.file_id:
            return f
    label = (installed.file_label or "").strip().lower()
    if label:
        same = [f for f in files if (f.get("label") or "").strip().lower() == label]
        if len(same) == 1:
            return same[0]
    if installed.file_kind:
        same = [f for f in files if f.get("kind") == installed.file_kind]
        if len(same) == 1:
            return same[0]
    return None


def is_update(installed: ModholdMeta, counterpart: dict) -> bool:
    """True when *counterpart* is newer than the installed file."""
    cid = int(counterpart.get("id") or 0)
    if cid > installed.file_id:
        return True
    if cid == installed.file_id:
        sha = str(counterpart.get("sha256") or "").lower()
        return bool(installed.sha256 and sha and sha != installed.sha256.lower())
    return False       # lower id: a rollback, not an update


def check_for_updates(staging_root: Path, api: ModholdAPI,
                      progress_cb: Optional[ProgressCallback] = None,
                      only_names: Optional[set[str]] = None
                      ) -> list[ModholdUpdateInfo]:
    """Update info for every Modhold mod under *staging_root* that has a newer file
    (or an unknown state). Up-to-date and ignored mods are omitted. meta.ini is
    refreshed with the latest file. *only_names* limits the check to those folders.
    Never raises on a single mod's failure."""
    log = progress_cb or (lambda m: None)

    targets: list[tuple[Path, ModholdMeta]] = []
    for folder in sorted(p for p in staging_root.iterdir() if p.is_dir()):
        if only_names is not None and folder.name not in only_names:
            continue
        meta_path = folder / "meta.ini"
        if not meta_path.is_file():
            continue
        meta = read_modhold_meta(meta_path)
        if meta.mod_id > 0:
            targets.append((folder, meta))
    if not targets:
        log("Modhold: no Modhold mods to check.")
        return []

    mod_ids = sorted({m.mod_id for _, m in targets})
    log(f"Modhold: checking {len(mod_ids)} mod(s)…")

    def fetch(mod_id: int):
        try:
            return mod_id, api.get_mod(mod_id), ""
        except ModholdNotFound:
            return mod_id, None, "no longer on Modhold"
        except ModholdError as e:
            return mod_id, None, str(e)
        except Exception as e:                      # one bad reply must not stop the rest
            return mod_id, None, str(e)

    details: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="modhold-check") as pool:
        for mod_id, detail, err in pool.map(fetch, mod_ids):
            if detail is None:
                log(f"Modhold: mod {mod_id} — {err}, skipped.")
            else:
                details[mod_id] = detail

    results: list[ModholdUpdateInfo] = []
    for folder, meta in targets:
        detail = details.get(meta.mod_id)
        if detail is None:
            continue
        files = [f for f in (detail.get("files") or []) if f.get("id")]
        counter = find_counterpart(meta, files)
        info = ModholdUpdateInfo(mod_name=folder.name, mod_id=meta.mod_id,
                                 installed_file_id=meta.file_id,
                                 installed_version=meta.version)
        if counter is None:
            info.unknown = True
            info.latest_version = str(detail.get("version") or "")
            results.append(info)
            continue
        latest_id = int(counter.get("id") or 0)
        latest_ver = str(counter.get("version") or detail.get("version") or "")
        latest_sha = str(counter.get("sha256") or "").lower()
        info.latest_file_id, info.latest_version = latest_id, latest_ver
        newer = is_update(meta, counter)
        ignored = bool(meta.ignore_update and meta.ignored_version == latest_ver)
        if newer and not ignored:
            info.has_update = True
            results.append(info)
        # Persist what we learned (the modlist paints its flag from these keys).
        # A rollback keeps the installed id as "latest" so it never reads as newer.
        rollback = latest_id < meta.file_id
        keep_id = meta.file_id if rollback else latest_id
        keep_sha = meta.sha256.lower() if rollback else latest_sha
        keep_ver = meta.version if rollback else latest_ver
        if (meta.latest_file_id, meta.latest_version, meta.latest_sha256) != (
                keep_id, keep_ver, keep_sha):
            meta.latest_file_id, meta.latest_version = keep_id, keep_ver
            meta.latest_sha256 = keep_sha
            try:
                write_modhold_meta(folder / "meta.ini", meta)
            except OSError as e:
                app_log(f"Modhold: could not update meta.ini for '{folder.name}': {e}")

    log(f"Modhold: {len(results)} mod(s) need attention.")
    return results

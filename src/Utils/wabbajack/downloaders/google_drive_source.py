"""Google Drive public-file archive downloads.

Covers ``Archive.State``'s ``GoogleDriveDownloader`` ``$type``: Wabbajack
stores just the Drive file id (``State.Id``) and expects a public ("anyone
with the link") share -- a private share would need an authenticated Drive
API session, which is out of scope (Wabbajack modlists reference public
shares almost universally; see the plan).

A file above Drive's virus-scan size threshold is not served directly:
the download endpoint answers with an interstitial "Google Drive - Virus
scan warning" page, and the real bytes come from repeating the request with
a confirm token. That token has taken three forms over the years, and this
handles all of them:

* a ``download_warning_*`` response cookie (oldest; checked first, it is the
  cheapest),
* a ``confirm=<token>`` value embedded in the interstitial's HTML,
* **the current one**: the interstitial is a small HTML ``<form>`` whose
  hidden inputs carry ``confirm=t`` plus a per-request ``uuid``, posted back
  to ``drive.usercontent.google.com/download``. Verified 2026-11-06 against
  a real 38 MB public share (the one LoreRim pulls "Vigilant Boss Moveset"
  from), which the two older forms both failed to unwrap.

Getting that wrong used to be silent: the warning page itself was written to
the destination and reported as a successful download, and only the
installer's hash check downstream noticed -- as "downloaded file doesn't
match the modlist's hash", which reads like a corrupt or moved upload rather
than a Drive handshake Mosaic didn't finish. An HTML response is now an
explicit failure, whatever the reason for it.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth_limit

from ..wabbajack_manifest import GoogleDriveState
from .http_source import DownloadCancelled, WabbajackDownloadResult

# docs.google.com/uc?export=download redirects here; asking directly saves a
# hop and is where the interstitial's own form posts back to.
_DOWNLOAD_URL = "https://drive.usercontent.google.com/download"
_CHUNK_SIZE = 256 * 1024
_CONFIRM_RE = re.compile(r"confirm=([0-9A-Za-z_-]+)")
_FORM_RE = re.compile(r"""<form\b[^>]*\baction\s*=\s*["']([^"']+)["']""", re.I)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([A-Za-z_][\w-]*)\s*=\s*["']([^"']*)["']""")


def _is_html(response: "requests.Response") -> bool:
    return "text/html" in (response.headers.get("Content-Type") or "")


def _hidden_fields(page: str) -> "dict[str, str]":
    """The ``<input type="hidden">`` name/value pairs in *page*."""
    fields: "dict[str, str]" = {}
    for tag in _INPUT_RE.findall(page):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        if attrs.get("type", "").lower() == "hidden" and attrs.get("name"):
            fields[attrs["name"]] = attrs.get("value", "")
    return fields


def _confirm_request(page: str, file_id: str) -> "tuple[str, dict[str, str]]":
    """Where to ask again for a file behind the virus-scan warning.

    Prefers the interstitial's own form (the current shape, which needs the
    page's one-off ``uuid``), falls back to a bare ``confirm=`` token in the
    HTML, and finally to ``confirm=t`` -- which Drive still honours, and is a
    better guess than giving up.
    """
    fields = _hidden_fields(page)
    if fields.get("confirm"):
        action = _FORM_RE.search(page)
        fields.setdefault("id", file_id)
        return (action.group(1) if action else _DOWNLOAD_URL), fields
    match = _CONFIRM_RE.search(page)
    token = match.group(1) if match else "t"
    return _DOWNLOAD_URL, {"id": file_id, "export": "download", "confirm": token}


def download_google_drive(state: GoogleDriveState, dest: Path, *, progress_cb=None,
                           cancel: "threading.Event | None" = None) -> WabbajackDownloadResult:
    """Download a public Google Drive file (``Archive.State`` ``$type``
    ``GoogleDriveDownloader``)."""
    if not state.file_id:
        return WabbajackDownloadResult(
            success=False, error="no file id in GoogleDriveDownloader state")

    verify = resolve_ca_bundle() or True
    session = requests.Session()
    try:
        probe = session.get(_DOWNLOAD_URL,
                            params={"id": state.file_id, "export": "download"},
                            stream=True, timeout=30, verify=verify)
        probe.raise_for_status()
        cookie_token = next((v for k, v in probe.cookies.items()
                             if k.startswith("download_warning")), None)
        if cookie_token:
            probe.close()
            resp_cm = session.get(
                _DOWNLOAD_URL,
                params={"id": state.file_id, "export": "download",
                        "confirm": cookie_token},
                stream=True, timeout=60, verify=verify)
        elif _is_html(probe):
            page = probe.text
            probe.close()
            url, params = _confirm_request(page, state.file_id)
            resp_cm = session.get(url, params=params, stream=True, timeout=60,
                                  verify=verify)
        else:
            resp_cm = probe

        with resp_cm as resp:
            resp.raise_for_status()
            if _is_html(resp):
                # Writing this would hand the installer a few KB of warning
                # page named like a 1.6 GB archive.
                return WabbajackDownloadResult(
                    success=False,
                    error="Google Drive served a web page instead of the file "
                          "-- the share may have been removed, or it is no "
                          "longer public")
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(_CHUNK_SIZE):
                    if cancel and cancel.is_set():
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise DownloadCancelled()
                    fh.write(chunk)
                    downloaded += len(chunk)
                    bandwidth_limit.throttle(len(chunk), cancel)
                    if progress_cb:
                        progress_cb(downloaded, total)
        return WabbajackDownloadResult(success=True, file_path=dest, bytes_downloaded=downloaded)
    except DownloadCancelled:
        return WabbajackDownloadResult(success=False, error="cancelled")
    except requests.RequestException as exc:
        return WabbajackDownloadResult(success=False, error=str(exc))
    finally:
        session.close()

"""Google Drive public-file archive downloads.

Covers ``Archive.State``'s ``GoogleDriveDownloader`` ``$type``: Wabbajack
stores just the Drive file id (``State.Id``) and expects a public ("anyone
with the link") share -- a private share would need an authenticated Drive
API session, which is out of scope (Wabbajack modlists reference public
shares almost universally; see the plan).

Google Drive serves small files directly from the ``uc?export=download``
endpoint, but a file above its virus-scan size threshold returns an
interstitial HTML page instead of the file, requiring a second request with
a "confirm" token. That token has shown up in two forms over the years: a
``download_warning_*`` response cookie, and (for very large files) a
``confirm=`` value embedded in the interstitial page's own HTML. This
handles both, preferring the cookie since it's cheaper to check. Neither
form is verified against a live Google Drive download in this sandbox --
cross-check against a real large-file share before relying on this for a
release.
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

_DOWNLOAD_URL = "https://docs.google.com/uc?export=download"
_CHUNK_SIZE = 256 * 1024
_CONFIRM_RE = re.compile(r"confirm=([0-9A-Za-z_-]+)")


def _find_confirm_token(response: "requests.Response") -> "str | None":
    for key, value in response.cookies.items():
        if key.startswith("download_warning"):
            return value
    if "text/html" in response.headers.get("Content-Type", ""):
        match = _CONFIRM_RE.search(response.text)
        if match:
            return match.group(1)
    return None


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
        probe = session.get(_DOWNLOAD_URL, params={"id": state.file_id},
                             stream=True, timeout=30, verify=verify)
        probe.raise_for_status()
        token = _find_confirm_token(probe)
        if token:
            probe.close()
            resp_cm = session.get(
                _DOWNLOAD_URL, params={"id": state.file_id, "confirm": token},
                stream=True, timeout=60, verify=verify)
        else:
            resp_cm = probe

        with resp_cm as resp:
            resp.raise_for_status()
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

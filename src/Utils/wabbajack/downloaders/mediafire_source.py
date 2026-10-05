"""MediaFire archive downloads.

Covers ``Archive.State``'s ``MediaFireDownloader`` ``$type``. MediaFire has
no public file-download API: the download page only embeds the real,
short-lived CDN URL in its rendered HTML (an anchor whose ``href`` points at
``download<n>.mediafire.com/.../<filename>``). This scrapes that URL out of
the page and streams from it -- fragile to MediaFire changing its page
markup, so it's kept isolated in its own module, separate from the other
downloaders, so a MediaFire breakage doesn't take them down too. Not
verified against a live MediaFire page in this sandbox.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth_limit

from ..wabbajack_manifest import MediaFireState
from .http_source import DownloadCancelled, WabbajackDownloadResult

_CHUNK_SIZE = 256 * 1024
_DIRECT_LINK_RE = re.compile(r'href="(https?://download\d*\.mediafire\.com/[^"]+)"')


def _resolve_direct_url(page_url: str, verify) -> "str | None":
    resp = requests.get(page_url, timeout=30, verify=verify)
    resp.raise_for_status()
    match = _DIRECT_LINK_RE.search(resp.text)
    return match.group(1) if match else None


def download_mediafire(state: MediaFireState, dest: Path, *, progress_cb=None,
                        cancel: "threading.Event | None" = None) -> WabbajackDownloadResult:
    """Download a MediaFire-hosted archive (``Archive.State`` ``$type``
    ``MediaFireDownloader``)."""
    if not state.url:
        return WabbajackDownloadResult(success=False, error="no URL in MediaFireDownloader state")

    verify = resolve_ca_bundle() or True
    try:
        direct_url = _resolve_direct_url(state.url, verify)
    except requests.RequestException as exc:
        return WabbajackDownloadResult(success=False, error=f"MediaFire page fetch failed: {exc}")
    if not direct_url:
        return WabbajackDownloadResult(
            success=False, error="could not find a direct download link on the MediaFire page")

    dest.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    try:
        with requests.get(direct_url, stream=True, timeout=60, verify=verify) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
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
    except DownloadCancelled:
        return WabbajackDownloadResult(success=False, error="cancelled")
    except requests.RequestException as exc:
        return WabbajackDownloadResult(success=False, error=str(exc))

    return WabbajackDownloadResult(success=True, file_path=dest, bytes_downloaded=downloaded)

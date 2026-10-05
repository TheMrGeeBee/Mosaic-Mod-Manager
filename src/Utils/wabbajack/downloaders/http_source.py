"""Direct-HTTP and Wabbajack-CDN archive downloads.

Covers two of ``Archive.State``'s ``$type`` variants:
  - ``HttpState`` -- a plain, already-resolved URL, optionally with extra
    request headers the compiler captured (e.g. an auth token for a gated
    host).
  - ``WabbajackCDNState`` -- Wabbajack's own hosted mirror. The URL doesn't
    point at the file itself: it's a "definition" endpoint returning a JSON
    manifest describing one or more independently-hosted parts, which this
    module fetches in order and concatenates. (The exact definition-JSON
    shape assumed below -- a ``Parts`` list of ``{"Url", "Size"}`` objects --
    should be cross-checked against a real modlist before this is trusted
    for a release; see the plan's "key open risks" note. Per-part integrity
    isn't verified here: the orchestrator verifies the whole concatenated
    file against ``Archive.hash`` once downloaded, which already catches a
    corrupt/truncated result.)

Streaming/cancel/bandwidth-throttle pattern mirrors
``Nexus.nexus_download.NexusDownloader._stream_download``, minus the
Nexus-specific file-id sidecar (a Wabbajack archive is identified by content
hash, not a Nexus file id).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth_limit

from ..wabbajack_manifest import HttpState, WabbajackCDNState

_CHUNK_SIZE = 256 * 1024
_REQUEST_TIMEOUT = 60


class DownloadCancelled(Exception):
    """Raised internally when a download is cancelled via the cancel event."""


@dataclass
class WabbajackDownloadResult:
    success: bool
    file_path: "Path | None" = None
    error: str = ""
    bytes_downloaded: int = 0


def _parse_headers(header_lines: "list[str]") -> dict:
    """Wabbajack stores extra request headers as ``"Key:Value"`` strings."""
    headers: dict = {}
    for line in header_lines or []:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()
    return headers


def _stream_to_file(url: str, dest: Path, *, headers: "dict | None" = None,
                     progress_cb=None, cancel: "threading.Event | None" = None) -> int:
    """Stream ``url`` to ``dest``, returning bytes written. Raises
    ``DownloadCancelled`` or a ``requests`` exception on failure; callers
    wrap this into a :class:`WabbajackDownloadResult`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    with requests.get(url, stream=True, timeout=_REQUEST_TIMEOUT, headers=headers or None,
                       verify=resolve_ca_bundle() or True) as resp:
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
    return downloaded


def download_http(state: HttpState, dest: Path, *, progress_cb=None,
                   cancel: "threading.Event | None" = None) -> WabbajackDownloadResult:
    """Download a direct-HTTP archive (``Archive.State`` ``$type``
    ``HttpDownloader``)."""
    if not state.url:
        return WabbajackDownloadResult(success=False, error="no URL in HttpDownloader state")
    try:
        n = _stream_to_file(state.url, dest, headers=_parse_headers(state.headers),
                             progress_cb=progress_cb, cancel=cancel)
        return WabbajackDownloadResult(success=True, file_path=dest, bytes_downloaded=n)
    except DownloadCancelled:
        return WabbajackDownloadResult(success=False, error="cancelled")
    except requests.RequestException as exc:
        return WabbajackDownloadResult(success=False, error=str(exc))


def download_wabbajack_cdn(state: WabbajackCDNState, dest: Path, *, progress_cb=None,
                            cancel: "threading.Event | None" = None) -> WabbajackDownloadResult:
    """Download a Wabbajack-CDN-hosted archive (``Archive.State`` ``$type``
    ``WabbajackCDNDownloader``). See the module docstring for the definition-
    JSON shape assumed and what is/isn't verified here."""
    if not state.url:
        return WabbajackDownloadResult(
            success=False, error="no URL in WabbajackCDNDownloader state")
    try:
        resp = requests.get(state.url, timeout=30, verify=resolve_ca_bundle() or True)
        resp.raise_for_status()
        definition = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return WabbajackDownloadResult(success=False, error=f"CDN definition fetch failed: {exc}")

    parts = definition.get("Parts") or []
    if not parts:
        return WabbajackDownloadResult(success=False, error="CDN definition has no parts")

    dest.parent.mkdir(parents=True, exist_ok=True)
    total_size = int(definition.get("Size") or sum(int(p.get("Size") or 0) for p in parts))
    downloaded = 0
    try:
        with open(dest, "wb") as out:
            for part in parts:
                if cancel and cancel.is_set():
                    raise DownloadCancelled()
                part_url = part.get("Url", "")
                if not part_url:
                    raise ValueError("CDN part is missing a Url")
                with requests.get(part_url, stream=True, timeout=_REQUEST_TIMEOUT,
                                  verify=resolve_ca_bundle() or True) as presp:
                    presp.raise_for_status()
                    for chunk in presp.iter_content(_CHUNK_SIZE):
                        if cancel and cancel.is_set():
                            raise DownloadCancelled()
                        out.write(chunk)
                        downloaded += len(chunk)
                        bandwidth_limit.throttle(len(chunk), cancel)
                        if progress_cb:
                            progress_cb(downloaded, total_size)
    except DownloadCancelled:
        dest.unlink(missing_ok=True)
        return WabbajackDownloadResult(success=False, error="cancelled")
    except (requests.RequestException, ValueError, OSError) as exc:
        dest.unlink(missing_ok=True)
        return WabbajackDownloadResult(success=False, error=str(exc))

    return WabbajackDownloadResult(success=True, file_path=dest, bytes_downloaded=downloaded)

"""Direct-HTTP and Wabbajack-CDN archive downloads.

Covers two of ``Archive.State``'s ``$type`` variants:
  - ``HttpState`` -- a plain, already-resolved URL, optionally with extra
    request headers the compiler captured (e.g. an auth token for a gated
    host).
  - ``WabbajackCDNState`` -- Wabbajack's own file host
    (``authored-files.wabbajack.org``). The URL names a folder, not the
    file (a plain GET returns an HTML page): ``<url>/definition.json.gz``
    is gzip-compressed JSON with the total ``Size``/``Hash`` and a ``Parts``
    list (``Index``, ``Offset``, ``Size``, ``Hash``; no URLs), and each part
    is served from ``<url>/parts/<Index>``. Parts are written in ``Index``
    order and each one, then the whole file, is checked against its hash.
    This layout was observed on the gallery's own modlist files; a
    ``WabbajackCDNDownloader`` state inside a modlist hasn't been seen yet,
    so it's assumed to point at the same kind of folder.

Streaming/cancel/bandwidth-throttle pattern mirrors
``Nexus.nexus_download.NexusDownloader._stream_download``, minus the
Nexus-specific file-id sidecar (a Wabbajack archive is identified by content
hash, not a Nexus file id).
"""
from __future__ import annotations

import gzip
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth_limit

from ..wabbajack_hash import StreamingHash, hashes_match
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
    # The source needs the user to (re-)log in before a retry can succeed.
    needs_auth: bool = False


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


def cdn_base_url(url: str) -> str:
    """``url`` with its path percent-encoded (real CDN names contain spaces
    and apostrophes, e.g. ``A Dragonborn's Fate.wabbajack_<uuid>``) and no
    trailing slash. Idempotent: an already-encoded URL is left as is."""
    parts = urlsplit(url.strip())
    path = quote(unquote(parts.path), safe="/").rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _cdn_definition(base: str, verify) -> dict:
    resp = requests.get(f"{base}/definition.json.gz", timeout=30, verify=verify)
    resp.raise_for_status()
    data = resp.content
    # The server may send it gzipped or let the transfer layer decompress it.
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    definition = json.loads(data)
    if not isinstance(definition, dict):
        raise ValueError("CDN definition isn't a JSON object")
    return definition


def download_wabbajack_cdn(state: WabbajackCDNState, dest: Path, *, progress_cb=None,
                            cancel: "threading.Event | None" = None) -> WabbajackDownloadResult:
    """Download a Wabbajack-CDN-hosted archive (``Archive.State`` ``$type``
    ``WabbajackCDNDownloader``); see the module docstring for the layout."""
    if not state.url:
        return WabbajackDownloadResult(
            success=False, error="no URL in WabbajackCDNDownloader state")
    verify = resolve_ca_bundle() or True
    base = cdn_base_url(state.url)
    try:
        definition = _cdn_definition(base, verify)
    except (requests.RequestException, ValueError, OSError, EOFError) as exc:
        return WabbajackDownloadResult(success=False, error=f"CDN definition fetch failed: {exc}")

    parts = sorted(definition.get("Parts") or [], key=lambda p: int(p.get("Index") or 0))
    if not parts:
        return WabbajackDownloadResult(success=False, error="CDN definition has no parts")

    dest.parent.mkdir(parents=True, exist_ok=True)
    total_size = int(definition.get("Size") or sum(int(p.get("Size") or 0) for p in parts))
    whole = StreamingHash()
    downloaded = 0
    try:
        with open(dest, "wb") as out:
            for part in parts:
                if cancel and cancel.is_set():
                    raise DownloadCancelled()
                index = int(part.get("Index") or 0)
                part_hash, part_size = StreamingHash(), 0
                with requests.get(f"{base}/parts/{index}", stream=True,
                                  timeout=_REQUEST_TIMEOUT, verify=verify) as presp:
                    presp.raise_for_status()
                    for chunk in presp.iter_content(_CHUNK_SIZE):
                        if cancel and cancel.is_set():
                            raise DownloadCancelled()
                        out.write(chunk)
                        part_hash.update(chunk)
                        whole.update(chunk)
                        part_size += len(chunk)
                        downloaded += len(chunk)
                        bandwidth_limit.throttle(len(chunk), cancel)
                        if progress_cb:
                            progress_cb(downloaded, total_size)
                if part.get("Size") is not None and part_size != int(part["Size"]):
                    raise ValueError(f"CDN part {index} is {part_size} bytes, "
                                     f"expected {part['Size']}")
                if part.get("Hash") and not hashes_match(part["Hash"], part_hash.b64()):
                    raise ValueError(f"CDN part {index} doesn't match its hash")
        if definition.get("Hash") and not hashes_match(definition["Hash"], whole.b64()):
            raise ValueError("downloaded file doesn't match the CDN's hash")
    except DownloadCancelled:
        dest.unlink(missing_ok=True)
        return WabbajackDownloadResult(success=False, error="cancelled")
    except (requests.RequestException, ValueError, OSError) as exc:
        dest.unlink(missing_ok=True)
        return WabbajackDownloadResult(success=False, error=str(exc))

    return WabbajackDownloadResult(success=True, file_path=dest, bytes_downloaded=downloaded)

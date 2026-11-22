"""
modhold_download.py
Download a Modhold file into the game's download cache and check it.

* The one-hour link is resolved right before the download, never earlier, so a
  paused or queued install never holds an expired link.
* The archive is streamed to ``<name>.part``, checked against the SHA-256 (and
  size) Modhold publishes, and only then renamed into place. A mismatch is
  retried once, then reported; a bad file is never left in the cache.
* A finished archive already in the folder is reused without a network call
  when its sidecar says it is this file and the file has not changed.
* The signed link is fetched without the player's key: only the API call that
  resolves the link carries it.

Modhold file ids are their own id space, so the sidecar is ``<archive>.modhold``
and never the Nexus ``.fileid``.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

from Modhold.modhold_api import (
    ModholdAPI,
    ModholdAuthError,
    ModholdError,
    ModholdGone,
    ModholdNotEntitled,
    ModholdNotReady,
)
from Utils.app_log import app_log
from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth_limit
from Utils.file_hash import file_digest

ProgressCallback = Callable[[int, int], None]

_CHUNK_SIZE = 256 * 1024
_ATTEMPTS = 2                 # one retry on a bad checksum or a dropped stream
_CONNECT_TIMEOUT = 15
_READ_TIMEOUT = 60
_SIDECAR_SUFFIX = ".modhold"
_PART_SUFFIX = ".part"


class DownloadCancelled(Exception):
    """Raised inside the stream loop when the cancel event is set."""


@dataclass
class ModholdDownloadResult:
    success: bool
    file_path: Optional[Path] = None
    file_name: str = ""
    error: str = ""
    file_id: int = 0
    bytes_downloaded: int = 0
    from_cache: bool = False
    # Why Modhold refused, when it did (a per-file outcome, never a program error).
    # ``error`` always holds Modhold's own player-readable message.
    not_entitled: bool = False      # paid, supporters-first or a beta this account lacks (402)
    not_connected: bool = False     # no key, or the key was removed (401)
    not_ready: bool = False         # still waiting on the virus scan; retry later (409)
    gone: bool = False              # an older version the author replaced (410)
    cancelled: bool = False


def failure_reason(res: ModholdDownloadResult) -> str:
    """The reason to show a player for a failed download. Modhold writes its own
    messages for players, so they are shown as sent; the fallbacks only cover an
    empty one."""
    msg = (res.error or "").strip()
    if res.not_entitled:
        return msg or "This account cannot get this file (it needs a purchase, Pass or beta access)."
    if res.not_connected:
        return msg or "Connect your Modhold account to download."
    if res.not_ready:
        return (msg or "Modhold is still scanning this file.") + " Try again in a few minutes."
    if res.gone:
        return msg or "This version has been replaced by the author."
    return msg or "The download failed."


def refusal_is_permanent(res: ModholdDownloadResult) -> bool:
    """True when asking again will get the same answer until something changes
    (no access to the file, or a version the author replaced)."""
    return bool(res.not_entitled or res.gone)


def sidecar_path(archive: Path) -> Path:
    return archive.with_name(archive.name + _SIDECAR_SUFFIX)


def _safe_name(name: str, file_id: int) -> str:
    """A bare, filesystem-safe archive name from whatever Modhold sent."""
    base = os.path.basename((name or "").replace("\\", "/")).strip().strip(".")
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base)
    return base or f"modhold_{file_id}.zip"


def _write_sidecar(archive: Path, file_id: int, sha256: str) -> None:
    try:
        st = archive.stat()
        sidecar_path(archive).write_text(json.dumps({
            "file_id": file_id, "sha256": sha256,
            "size": st.st_size, "mtime_ns": st.st_mtime_ns}), encoding="utf-8")
    except OSError:
        pass


def _read_sidecar(archive: Path) -> dict:
    try:
        data = json.loads(sidecar_path(archive).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def delete_archive_and_sidecar(archive: Path) -> None:
    """Remove an archive and its sidecar; safe when either is missing."""
    for p in (archive, sidecar_path(archive)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _candidates(dest_dir: Path, name: str) -> list[Path]:
    """The archive path and its ``Name (n).ext`` siblings, if any exist."""
    exact = dest_dir / name
    stem, suffix = exact.stem, exact.suffix
    out = [exact] if exact.is_file() else []
    try:
        out += sorted(p for p in dest_dir.glob(f"{glob_escape(stem)} (*){suffix}")
                      if p.is_file())
    except OSError:
        pass
    return out


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def find_cached(dest_dir: Path, name: str, file_id: int, sha256: str,
                size: int = 0) -> Optional[Path]:
    """A finished copy of this exact file in *dest_dir*, or None.

    A copy counts only when its checksum is the published one. The sidecar lets
    an unchanged file (same size and mtime as when it was verified) skip the
    re-hash; anything else is hashed, so a truncated or replaced archive never
    passes. Without a published checksum nothing is trusted.
    """
    sha256 = (sha256 or "").lower()
    if not sha256:
        return None
    for path in _candidates(dest_dir, name):
        try:
            st = path.stat()
        except OSError:
            continue
        if size and st.st_size != size:
            continue
        side = _read_sidecar(path)
        if (side.get("file_id") == file_id and side.get("sha256") == sha256
                and side.get("size") == st.st_size
                and side.get("mtime_ns") == st.st_mtime_ns):
            return path
        try:
            if file_digest(path, "sha256").lower() == sha256:
                _write_sidecar(path, file_id, sha256)
                return path
        except OSError:
            continue
    return None


class ModholdDownloader:
    """Downloads Modhold files. ``session`` is injectable for tests."""

    def __init__(self, api: ModholdAPI, download_dir: Path,
                 session: Optional[requests.Session] = None):
        self._api = api
        self._dir = Path(download_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._session = session or requests.Session()

    @property
    def download_dir(self) -> Path:
        return self._dir

    def download_file(self, file_id: int, *,
                      known_name: str = "", known_size: int = 0,
                      known_sha256: str = "",
                      progress_cb: Optional[ProgressCallback] = None,
                      cancel: Optional[threading.Event] = None
                      ) -> ModholdDownloadResult:
        """Fetch one file, or reuse the finished copy already on disk.

        Pass what the mod's page already told you (``known_*``, from
        ``files[]``) and a cached archive is found without touching the API.
        """
        if known_name and known_sha256:
            hit = find_cached(self._dir, _safe_name(known_name, file_id),
                              file_id, known_sha256, known_size)
            if hit:
                app_log(f"Modhold: reusing cached {hit.name}")
                return ModholdDownloadResult(True, hit, hit.name, file_id=file_id,
                                             bytes_downloaded=hit.stat().st_size,
                                             from_cache=True)

        if cancel is not None and cancel.is_set():
            return ModholdDownloadResult(False, error="Download cancelled",
                                         file_id=file_id, cancelled=True)
        try:
            link = self._api.file_download(file_id)
        except ModholdNotEntitled as e:
            return ModholdDownloadResult(False, error=str(e), file_id=file_id,
                                         not_entitled=True)
        except ModholdAuthError as e:
            return ModholdDownloadResult(False, error=str(e), file_id=file_id,
                                         not_connected=True)
        except ModholdNotReady as e:
            return ModholdDownloadResult(False, error=str(e), file_id=file_id, not_ready=True)
        except ModholdGone as e:
            return ModholdDownloadResult(False, error=str(e), file_id=file_id, gone=True)
        except ModholdError as e:
            return ModholdDownloadResult(False, error=str(e), file_id=file_id)

        name = _safe_name(link.name or known_name, file_id)
        size = link.size or known_size
        sha = link.sha256 or (known_sha256 or "").lower()
        # The link's own name/hash can differ from what the page showed.
        hit = find_cached(self._dir, name, file_id, sha, size)
        if hit:
            return ModholdDownloadResult(True, hit, hit.name, file_id=file_id,
                                         bytes_downloaded=hit.stat().st_size,
                                         from_cache=True)

        last_error = ""
        for attempt in range(_ATTEMPTS):
            try:
                path = self._stream(link.url, name, size, sha, file_id,
                                    progress_cb, cancel)
                return ModholdDownloadResult(True, path, path.name, file_id=file_id,
                                             bytes_downloaded=path.stat().st_size)
            except DownloadCancelled:
                return ModholdDownloadResult(False, error="Download cancelled",
                                             file_id=file_id, cancelled=True)
            except (requests.RequestException, OSError, ValueError) as e:
                last_error = str(e)
                app_log(f"Modhold: attempt {attempt + 1} for {name} failed: {e}")
        return ModholdDownloadResult(False, error=last_error or "Download failed",
                                     file_id=file_id)

    # ------------------------------------------------------------- internals

    def _stream(self, url: str, name: str, size: int, sha256: str, file_id: int,
                progress_cb: Optional[ProgressCallback],
                cancel: Optional[threading.Event]) -> Path:
        part = self._dir / (name + _PART_SUFFIX)
        try:
            with self._session.get(url, stream=True,
                                   timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                                   verify=resolve_ca_bundle() or True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0)) or size
                done = 0
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(_CHUNK_SIZE):
                        if cancel is not None and cancel.is_set():
                            raise DownloadCancelled()
                        fh.write(chunk)
                        done += len(chunk)
                        bandwidth_limit.throttle(len(chunk), cancel)
                        if progress_cb:
                            progress_cb(done, total)
            if size and done != size:
                raise ValueError(f"Size mismatch for {name}: got {done}, expected {size}")
            if sha256 and file_digest(part, "sha256").lower() != sha256:
                raise ValueError(f"Checksum mismatch for {name}: the download is "
                                 f"corrupt or was altered")
            final = self._free_name(name)
            os.replace(part, final)
            _write_sidecar(final, file_id, sha256)
            app_log(f"Modhold: downloaded {final.name} ({done} bytes)")
            return final
        except BaseException:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _free_name(self, name: str) -> Path:
        dest = self._dir / name
        n = 1
        while dest.exists():
            dest = self._dir / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        return dest

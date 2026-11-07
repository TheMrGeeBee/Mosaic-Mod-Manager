"""
runtime_swap_fix.py
The work behind the preflight's "switch the Skyrim runtime" fix: get the
collection's runtime-swap archive (from the cache, else a premium download),
verify it against the collection's md5, extract it, and load its patch manifest.

No Qt. Runs on a worker thread; the caller applies the transition afterwards
(after a Restore, if the game is deployed) with
``skyrim_runtime.apply_transition``.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from Utils.collections.collection_preflight import find_swap_archive
from Utils.modding_tools import skyrim_runtime as sr


def _noop(_msg: str) -> None:
    pass


@dataclass
class PreparedSwap:
    """A loaded transition plus the temp folder its patches live in."""
    transition: sr.Transition
    workdir: Path
    archive: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def _md5(path: Path) -> str:
    h = hashlib.md5()                                   # noqa: S324 — Nexus publishes md5, not a security use
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def acquire_swap_archive(
    mod,
    *,
    domain: str,
    search_dirs: Iterable["str | Path"],
    dest_dir: "str | Path",
    downloader,
    nexus_url: str = "",
    log_fn: Callable[[str], None] = _noop,
    cancel=None,
) -> Path:
    """The runtime-swap archive: an exact cached copy, else a premium download
    into *dest_dir* (Mosaic's per-game cache, so it is found next time).

    *downloader* is a ``NexusDownloader`` or None (non-premium / not logged in),
    in which case a missing archive is reported with where to get it.
    """
    found = find_swap_archive(mod, search_dirs)
    if found is not None:
        log_fn(f"Runtime swap: using the archive already downloaded: {found.name}")
        return found
    name = getattr(mod, "mod_name", "") or "the runtime swap mod"
    if downloader is None:
        raise sr.RuntimeSwapError(
            f"{name} isn't downloaded yet and Mosaic can't fetch it without a Nexus "
            "Premium login. Download its main file with MANUAL download"
            + (f" from {nexus_url}" if nexus_url else "")
            + " into your Downloads folder, then try again.")
    log_fn(f"Runtime swap: downloading {name} …")
    result = downloader.download_file(
        domain, int(mod.mod_id), int(mod.file_id), dest_dir=Path(dest_dir), cancel=cancel,
        known_file_name=getattr(mod, "file_name", "") or "",
        expected_size_bytes=int(getattr(mod, "size_bytes", 0) or 0))
    if not getattr(result, "success", False) or not getattr(result, "file_path", None):
        raise sr.RuntimeSwapError(
            f"Couldn't download {name}: {getattr(result, 'error', '') or 'unknown error'}.")
    path = Path(result.file_path)
    return path


def verify_archive_md5(archive: Path, expected_md5: str) -> None:
    """Refuse an archive whose md5 differs from the collection's (when known)."""
    expected = (expected_md5 or "").strip().lower()
    if expected and _md5(archive) != expected:
        raise sr.RuntimeSwapError(
            f"{archive.name} doesn't match the file the collection expects (checksum "
            "mismatch). Delete it and try again.")


def prepare_swap(
    mod,
    *,
    domain: str,
    search_dirs: Iterable["str | Path"],
    dest_dir: "str | Path",
    downloader,
    nexus_url: str = "",
    log_fn: Callable[[str], None] = _noop,
    cancel=None,
    extract: Callable[[Path, Path], None] | None = None,
) -> PreparedSwap:
    """Acquire, verify, extract and parse the swap archive. Caller must
    :meth:`PreparedSwap.cleanup` when done."""
    archive = acquire_swap_archive(
        mod, domain=domain, search_dirs=search_dirs, dest_dir=dest_dir,
        downloader=downloader, nexus_url=nexus_url, log_fn=log_fn, cancel=cancel)
    verify_archive_md5(archive, getattr(mod, "md5", ""))
    return prepare_swap_from_archive(archive, log_fn=log_fn, extract=extract)


def prepare_swap_from_archive(
    archive: "str | Path",
    *,
    log_fn: Callable[[str], None] = _noop,
    extract: Callable[[Path, Path], None] | None = None,
) -> PreparedSwap:
    """Extract and parse a swap archive that is already on disk.

    The route for a swap nothing told Mosaic about: a Wabbajack modlist that
    only says which runtime its script extender needs, or a file the user
    pointed at. There is no collection there to name the file or publish its
    md5, so what makes an archive the right one is what is inside it -- a
    ``RuntimeSwap/manifest.json``, whose own per-file hashes are then checked
    against the game folder before anything is patched. Caller must
    :meth:`PreparedSwap.cleanup` when done.
    """
    if extract is None:
        from Utils.wizard_support.wizard_archives import extract_to_dir as extract
    archive = Path(archive)
    workdir = Path(tempfile.mkdtemp(prefix="mosaic-runtime-swap-"))
    try:
        log_fn(f"Runtime swap: extracting {archive.name} …")
        extract(archive, workdir)
        swap_dir = sr.find_swap_dir(workdir)
        if swap_dir is None:
            raise sr.RuntimeSwapError(
                f"{archive.name} has no RuntimeSwap/manifest.json — it isn't a runtime swap mod.")
        transition = sr.load_transition(swap_dir)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return PreparedSwap(transition, workdir, archive)

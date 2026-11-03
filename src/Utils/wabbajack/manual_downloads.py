"""Finding archives the user downloaded by hand in their browser.

Real modlists ship many archives as ``ManualDownloader`` entries -- in a
2026-10-07 sample of 12 gallery lists, 279 of 303 manual entries pointed at
LoversLab (Wabbajack itself no longer logs in there), the rest at MediaFire,
Mega, GitHub and Yandex. The user downloads those in their own browser; this
module finds the result in their download folders (the system Downloads
folder, plus Mosaic's extra download locations) by exact size first and
Wabbajack hash second, so a file already downloaded earlier is picked up
without asking.
"""
from __future__ import annotations

from pathlib import Path

from .wabbajack_hash import hash_file, hashes_match

# Browsers' in-progress download names: never a finished file.
_PARTIAL_SUFFIXES = (".part", ".crdownload", ".download", ".partial", ".tmp")


def download_folders() -> "list[Path]":
    """Where the user's browser downloads land: the system Downloads folder
    (always -- the browser saves there whatever Mosaic's Downloads panel is
    set to show), plus Mosaic's extra download locations. Existing folders
    only, without duplicates."""
    from Utils.downloads.download_locations import (
        get_default_downloads_dir,
        load_extra_download_locations,
    )
    folders: "list[Path]" = []
    try:
        folders.append(Path(get_default_downloads_dir()))
    except Exception:
        pass
    try:
        folders += [Path(p) for p in load_extra_download_locations()]
    except Exception:
        pass
    out, seen = [], set()
    for f in folders:
        try:
            key = f.resolve()
        except OSError:
            continue
        if key not in seen and f.is_dir():
            seen.add(key)
            out.append(f)
    return out


class DownloadFinder:
    """Looks for one archive in some folders. Each candidate file (same size,
    not a partial download) is hashed once; a later :meth:`find` only looks
    at files that are new or changed since, so it's cheap to call every
    second while the user downloads."""

    def __init__(self, size: int, expected_hash: str, folders: "list[Path]"):
        self.size = int(size or 0)
        self.expected_hash = expected_hash
        self.folders = list(folders)
        self._checked: "dict[Path, tuple[int, float]]" = {}

    def find(self) -> "Path | None":
        if self.size <= 0 or not self.expected_hash:
            return None
        for folder in self.folders:
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for p in entries:
                if p.name.lower().endswith(_PARTIAL_SUFFIXES):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not p.is_file() or st.st_size != self.size:
                    continue
                stamp = (st.st_size, st.st_mtime)
                if self._checked.get(p) == stamp:
                    continue
                self._checked[p] = stamp
                try:
                    if hashes_match(self.expected_hash, hash_file(p)):
                        return p
                except OSError:
                    continue
        return None


def find_downloaded(archive, folders: "list[Path] | None" = None) -> "Path | None":
    """``archive`` (a modlist ``Archive``) if it's already in the download
    folders, verified by size and hash; else None."""
    return DownloadFinder(archive.size, archive.hash,
                          download_folders() if folders is None else folders).find()


class MultiDownloadFinder:
    """Watch one set of folders for *any* of several pending archives.

    :class:`DownloadFinder` is built for a single archive, which forces the
    manual-download flow to be strictly serial: archive 5 sitting in the
    Downloads folder is invisible until its turn comes, and one archive that
    never arrives blocks every other. A real list makes that costly -- the
    VtMB "Fashionista - Steam" list has 77 manual archives.

    Each candidate file is hashed at most once per (size, mtime), and only
    when its size matches something still pending, so watching 77 archives
    costs about the same as watching one.
    """

    def __init__(self, archives, folders: "list[Path]"):
        # hash -> archive, for everything still wanted
        self._pending = {a.hash: a for a in archives if a.hash and (a.size or 0) > 0}
        self._by_size: "dict[int, list]" = {}
        for a in self._pending.values():
            self._by_size.setdefault(int(a.size), []).append(a)
        self.folders = list(folders)
        self._checked: "dict[Path, tuple[int, float]]" = {}

    def pending(self) -> list:
        """Archives not yet found, in no particular order."""
        return list(self._pending.values())

    def resolve(self, archive, path: "Path | None" = None) -> None:
        """Drop *archive* from the watch set — found elsewhere, or skipped."""
        self._pending.pop(archive.hash, None)
        same = self._by_size.get(int(archive.size or 0))
        if same:
            remaining = [a for a in same if a.hash != archive.hash]
            if remaining:
                self._by_size[int(archive.size or 0)] = remaining
            else:
                self._by_size.pop(int(archive.size or 0), None)

    def find_any(self) -> "dict[str, Path]":
        """Scan once; return ``{archive.hash: path}`` for newly found files.

        Found archives are removed from the watch set, so a later call only
        looks for what is still outstanding.
        """
        hits: "dict[str, Path]" = {}
        if not self._pending:
            return hits
        for folder in self.folders:
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for p in entries:
                if not self._pending:
                    return hits
                if p.name.lower().endswith(_PARTIAL_SUFFIXES):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not p.is_file():
                    continue
                candidates = self._by_size.get(st.st_size)
                if not candidates:
                    continue
                stamp = (st.st_size, st.st_mtime)
                if self._checked.get(p) == stamp:
                    continue
                self._checked[p] = stamp
                try:
                    digest = hash_file(p)
                except OSError:
                    continue
                for archive in list(candidates):
                    if archive.hash in self._pending and hashes_match(archive.hash, digest):
                        hits[archive.hash] = p
                        self.resolve(archive)
                        break
        return hits

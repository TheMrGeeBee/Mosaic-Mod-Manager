"""Resolves a Wabbajack ``FromArchive``/``PatchedFromArchive`` directive's
``ArchiveHashPath`` to a real file on disk.

Wabbajack identifies files purely by content hash, and an ``ArchiveHashPath``
is a chain: ``[archive_hash, path_in_archive, path_in_nested_archive, ...]``.
The first element names one of the modlist's downloaded Archives (registered
by hash via :meth:`ArchiveIndex.add_archive`); each subsequent element is a
path inside the *previous* hop's container, which -- if more elements follow
-- must itself be an archive (the classic case is a BSA bundled inside a
7z).

Resolution extracts only the specific member needed at each hop (not the
whole container), and caches each hop's result on disk keyed by
``(container, member path)`` so the same nested file referenced by multiple
directives is only extracted once.

Containers can be zip, 7z, or a Bethesda BSA (v104/v105) / BA2 (read with
``Utils.archives.bsa_file_reader.BsaFile``). A DX10 texture read out of a
BA2 comes back with a reconstructed DDS header, which may not match the
bytes Wabbajack hashed -- the directive's hash check catches that rather
than installing a different file. Any other container format raises
:class:`VfsResolutionError`.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import py7zr

from .wabbajack_hash import hash_file

_ZIP_EXTS = (".zip",)
_SEVENZ_EXTS = (".7z",)
_BETHESDA_EXTS = (".bsa", ".ba2")


class VfsResolutionError(Exception):
    """A directive's ArchiveHashPath couldn't be resolved to a real file."""


class ArchiveIndex:
    """Maps a top-level Archive's content hash to its on-disk path, and
    caches each resolved nested-path hop so repeated directives referencing
    the same nested file only extract it once."""

    def __init__(self, scratch_dir: "str | Path"):
        self.scratch_dir = Path(scratch_dir)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self._roots: "dict[str, Path]" = {}
        self._hop_cache: "dict[tuple[str, str], Path]" = {}
        self._next_id = 0

    def add_archive(self, content_hash: str, path: "str | Path") -> None:
        """Register a downloaded Archive's on-disk location by its content
        hash (``Archive.hash`` from the modlist)."""
        self._roots[content_hash] = Path(path)

    def resolve(self, archive_hash_path: "list[str]") -> Path:
        """Resolve a directive's ``ArchiveHashPath`` to a real file on disk,
        extracting and caching each nested hop as needed.

        Raises :class:`VfsResolutionError` if any hop can't be found or
        extracted (missing archive, missing member, or an unsupported
        nested-container format).
        """
        if not archive_hash_path:
            raise VfsResolutionError("empty ArchiveHashPath")
        root_hash = archive_hash_path[0]
        current = self._roots.get(root_hash)
        if current is None:
            raise VfsResolutionError(
                f"archive {root_hash!r} was not downloaded/registered")

        current_hash = root_hash
        for member_path in archive_hash_path[1:]:
            cache_key = (current_hash, member_path)
            cached = self._hop_cache.get(cache_key)
            if cached is not None and cached.exists():
                current = cached
            else:
                current = self._extract_member(current, member_path)
                self._hop_cache[cache_key] = current
            current_hash = hash_file(current)
        return current

    def _extract_member(self, container: Path, member_path: str) -> Path:
        self._next_id += 1
        dest = self.scratch_dir / f"{self._next_id}_{Path(member_path).name}"
        ext = container.suffix.lower()
        if ext in _ZIP_EXTS:
            self._extract_from_zip(container, member_path, dest)
        elif ext in _SEVENZ_EXTS:
            self._extract_from_7z(container, member_path, dest)
        elif ext in _BETHESDA_EXTS:
            self._extract_from_bethesda(container, member_path, dest)
        else:
            raise VfsResolutionError(
                f"don't know how to look inside {container.name!r} to find "
                f"{member_path!r} (extension {ext!r} is not a supported "
                "nested-container format yet)")
        return dest

    @staticmethod
    def _extract_from_zip(container: Path, member_path: str, dest: Path) -> None:
        try:
            with zipfile.ZipFile(container, "r") as zf:
                names = set(zf.namelist())
                target = member_path if member_path in names else member_path.lstrip("/")
                if target not in names:
                    raise VfsResolutionError(f"{member_path!r} not found in {container.name}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(target) as src, open(dest, "wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
        except zipfile.BadZipFile as exc:
            raise VfsResolutionError(f"{container}: not a valid zip ({exc})") from exc

    @staticmethod
    def _extract_from_7z(container: Path, member_path: str, dest: Path) -> None:
        import tempfile
        try:
            with tempfile.TemporaryDirectory() as td:
                with py7zr.SevenZipFile(container, mode="r") as arc:
                    names = arc.getnames()
                    target = next(
                        (n for n in names if n == member_path or n.lstrip("/") == member_path),
                        None)
                    if target is None:
                        raise VfsResolutionError(f"{member_path!r} not found in {container.name}")
                    arc.extract(path=td, targets=[target])
                extracted = Path(td) / target.lstrip("/")
                if not extracted.is_file():
                    raise VfsResolutionError(
                        f"{member_path!r} extracted from {container.name} but not "
                        "found at the expected path")
                dest.parent.mkdir(parents=True, exist_ok=True)
                extracted.replace(dest)
        except py7zr.exceptions.ArchiveError as exc:
            raise VfsResolutionError(f"{container}: 7z read failed ({exc})") from exc

    @staticmethod
    def _extract_from_bethesda(container: Path, member_path: str, dest: Path) -> None:
        from Utils.archives.bsa_file_reader import BsaFile, BsaReadError
        try:
            with BsaFile(container) as archive:
                if member_path not in archive:
                    raise VfsResolutionError(f"{member_path!r} not found in {container.name}")
                data = archive.read(member_path)
        except BsaReadError as exc:
            raise VfsResolutionError(f"{container}: BSA/BA2 read failed ({exc})") from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

"""Resolves a Wabbajack ``FromArchive``/``PatchedFromArchive`` directive's
``ArchiveHashPath`` to a real file on disk.

Wabbajack identifies files purely by content hash, and an ``ArchiveHashPath``
is a chain: ``[archive_hash, path_in_archive, path_in_nested_archive, ...]``.
The first element names one of the modlist's downloaded Archives (registered
by hash via :meth:`ArchiveIndex.add_archive`); each subsequent element is a
path inside the *previous* hop's container, which -- if more elements follow
-- must itself be an archive (the classic case is a BSA bundled inside a
7z).

A zip or BSA/BA2 hop extracts only the member needed. A 7z or rar container,
or a zip using a method Python's zipfile can't decompress (PPMd, Deflate64),
is extracted whole, once, with Mosaic's regular extractor chain (native 7z →
bsdtar → py7zr, ``Utils.mods.mod_install._extract_archive``) and every later
member is served from that copy: py7zr alone can't read BCJ2-filtered 7z
archives (real case: F4SE 0.6.23), and per-member extraction re-decompresses
a solid archive for every file. Each hop's result is cached keyed by
``(container, member path)`` so the same nested file referenced by multiple
directives is only extracted once. Member paths are matched the way Windows
does: ``\\`` or ``/`` separators, any casing.

Containers can be zip, 7z, rar, or a Bethesda BSA (v104/v105) / BA2 (read with
``Utils.archives.bsa_file_reader.BsaFile``). A DX10 texture read out of a
BA2 gets the DDS header Wabbajack itself writes (``wabbajack_dds``), since
Wabbajack hashed and built its patches against that exact file. Any other container format raises
:class:`VfsResolutionError`.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from .wabbajack_hash import hash_file

_ZIP_EXTS = (".zip",)
_EXTRACT_WHOLE_EXTS = (".7z", ".rar")
_BETHESDA_EXTS = (".bsa", ".ba2")
# ba2_extract._make_dds_header: magic + DDS_HEADER + DDS_HEADER_DXT10.
_MOSAIC_DDS_HEADER_LEN = 4 + 124 + 20


def _norm_member(path: str) -> str:
    return path.replace("\\", "/").strip("/").lower()


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
        # Whole-extracted 7z/rar containers: container path -> {normalised
        # member path -> extracted file}.
        self._extracted: "dict[str, dict[str, Path]]" = {}
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
        ext = container.suffix.lower()
        if ext in _EXTRACT_WHOLE_EXTS:
            return self._member_of_whole_extract(container, member_path)
        self._next_id += 1
        dest = self.scratch_dir / f"{self._next_id}_{Path(member_path).name}"
        if ext in _ZIP_EXTS:
            try:
                self._extract_from_zip(container, member_path, dest)
            except NotImplementedError:
                # zipfile can't decompress PPMd (98) or Deflate64 (9) members
                # (real cases: VRAMr, Goodneighbor View); native 7z can.
                dest.unlink(missing_ok=True)
                return self._member_of_whole_extract(container, member_path)
        elif ext in _BETHESDA_EXTS:
            self._extract_from_bethesda(container, member_path, dest)
        else:
            raise VfsResolutionError(
                f"don't know how to look inside {container.name!r} to find "
                f"{member_path!r} (extension {ext!r} is not a supported "
                "nested-container format yet)")
        return dest

    @staticmethod
    def _find_member(names: "list[str]", member_path: str, container: Path) -> str:
        """The archive's own spelling of ``member_path``. Modlists record
        Windows paths (``Data\\Scripts\\Math.pex``, any casing); zip and 7z
        store ``Data/Scripts/Math.pex``."""
        wanted = _norm_member(member_path)
        exact = next((n for n in names if n.replace("\\", "/").strip("/") ==
                      member_path.replace("\\", "/").strip("/")), None)
        found = exact or next((n for n in names if _norm_member(n) == wanted), None)
        if found is None:
            raise VfsResolutionError(f"{member_path!r} not found in {container.name}")
        return found

    @staticmethod
    def _extract_from_zip(container: Path, member_path: str, dest: Path) -> None:
        try:
            with zipfile.ZipFile(container, "r") as zf:
                target = ArchiveIndex._find_member(zf.namelist(), member_path, container)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(target) as src, open(dest, "wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
        except zipfile.BadZipFile as exc:
            raise VfsResolutionError(f"{container}: not a valid zip ({exc})") from exc

    def _member_of_whole_extract(self, container: Path, member_path: str) -> Path:
        """``member_path`` from a 7z/rar (or PPMd/Deflate64 zip) container
        extracted whole, once, into
        the scratch dir (so on the install's own filesystem, never /tmp)."""
        key = str(container)
        members = self._extracted.get(key)
        if members is None:
            from Utils.mods.mod_install import _extract_archive
            self._next_id += 1
            out = self.scratch_dir / f"{self._next_id}_{container.stem}.d"
            out.mkdir(parents=True, exist_ok=True)
            errors: "list[str]" = []
            if not _extract_archive(str(container), str(out), lambda _m: None,
                                    error_sink=errors):
                detail = errors[-1].strip() if errors else "no extractor could read it"
                raise VfsResolutionError(f"{container}: extraction failed ({detail})")
            members = {_norm_member(p.relative_to(out).as_posix()): p
                       for p in out.rglob("*") if p.is_file()}
            self._extracted[key] = members
        found = members.get(_norm_member(member_path))
        if found is None:
            raise VfsResolutionError(f"{member_path!r} not found in {container.name}")
        return found

    @staticmethod
    def _extract_from_bethesda(container: Path, member_path: str, dest: Path) -> None:
        from Utils.archives.bsa_file_reader import BsaFile, BsaReadError
        try:
            with BsaFile(container) as archive:
                if member_path not in archive:
                    raise VfsResolutionError(f"{member_path!r} not found in {container.name}")
                data = archive.read(member_path)
                info = archive.dx10_info(member_path)
                if info is not None:
                    # Swap Mosaic's DDS header for the one Wabbajack hashed.
                    from .wabbajack_dds import wabbajack_dds_header
                    data = wabbajack_dds_header(
                        height=info["height"], width=info["width"],
                        mip_count=info["num_mips"], dxgi_format=info["dxgi_format"],
                        is_cubemap=info["is_cubemap"]) + data[_MOSAIC_DDS_HEADER_LEN:]
        except BsaReadError as exc:
            raise VfsResolutionError(f"{container}: BSA/BA2 read failed ({exc})") from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

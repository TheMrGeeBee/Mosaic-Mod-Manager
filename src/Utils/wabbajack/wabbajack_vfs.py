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

Containers can be zip, 7z, rar, an Inno Setup installer (``.exe``, extracted
with ``innoextract``; see :func:`_extract_inno`), or a Bethesda BSA
(v104/v105) / BA2 (read with ``Utils.archives.bsa_file_reader.BsaFile``). A DX10 texture read out of a
BA2 gets the DDS header Wabbajack itself writes (``wabbajack_dds``), since
Wabbajack hashed and built its patches against that exact file. Any other container format raises
:class:`VfsResolutionError`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

_ZIP_EXTS = (".zip",)
# .fomod: an old FOMM package, really a 7z or a zip (native 7z sniffs which).
# Real case: Nuclear Sunset's The Mod Configuration Menu.fomod.
_EXTRACT_WHOLE_EXTS = (".7z", ".rar", ".fomod")
_BETHESDA_EXTS = (".bsa", ".ba2")
# Inno Setup installers. Wabbajack extracts these with innoextract and records
# the member paths innoextract produces, so Mosaic has to use it too -- see
# _extract_inno.
_INNO_EXTS = (".exe",)
# ba2_extract._make_dds_header: magic + DDS_HEADER + DDS_HEADER_DXT10.
_MOSAIC_DDS_HEADER_LEN = 4 + 124 + 20


def _norm_member(path: str) -> str:
    return path.replace("\\", "/").strip("/").lower()



def _chain_key(chain) -> "tuple[str, ...]":
    """An ArchiveHashPath as Windows matches it: separators and casing of the
    member paths don't matter (the root is a content hash)."""
    return (chain[0], *(_norm_member(m) for m in chain[1:]))


class VfsResolutionError(Exception):
    """A directive's ArchiveHashPath couldn't be resolved to a real file."""


class _BadZipContainer(VfsResolutionError):
    """A ``.zip``-named container Python's zipfile can't read."""


def _extract_inno(container: Path, out: Path) -> None:
    """Extract an Inno Setup installer whole with ``innoextract``.

    ``--collisions=rename-all`` is what makes the member paths line up with
    the modlist: an Inno installer can ship several variants of one file
    chosen by component at install time (real case: VtMB's Unofficial Patch
    ships ``vdata/system/stats.txt`` three times, for its basic/plus/standard
    components), and Wabbajack records those as ``stats.txt#basic`` /
    ``stats.txt#plus`` -- innoextract's own collision-suffix spelling. The
    default mode leaves one variant unsuffixed and would silently resolve the
    wrong bytes for it, so every colliding variant has to be suffixed. No
    ``--default-language``: a multi-language installer's variants are
    suffixed the same way, and forcing one language would drop the others.
    """
    exe = shutil.which("innoextract")
    if exe is None:
        raise VfsResolutionError(
            f"{container.name} is an Inno Setup installer and innoextract is "
            "not installed; install it (Debian/Ubuntu: 'apt install "
            "innoextract', Arch: 'pacman -S innoextract', Fedora: 'dnf "
            "install innoextract') and run Repair to finish these files")
    try:
        proc = subprocess.run(
            [exe, "--extract", "--collisions=rename-all", "--silent",
             "--output-dir", str(out), str(container)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace", check=False)
    except OSError as exc:
        raise VfsResolutionError(f"{container}: innoextract failed to run ({exc})") from exc
    if proc.returncode != 0:
        detail = (proc.stdout or "").strip().splitlines()
        raise VfsResolutionError(
            f"{container}: innoextract failed "
            f"({detail[-1] if detail else f'exit {proc.returncode}'})")


def _extract_7z_members(container: Path, out: Path, wanted: "set[str]", *,
                        threads: int = 0, low_priority: bool = False) -> bool:
    """Extract only the members in *wanted* (normalised paths) from a 7z/rar
    with native 7z. True when every wanted member was found and extracted;
    False -- leaving the caller to extract the whole archive, as before --
    when 7z is missing, the listing fails, or a member isn't matched."""
    from Utils.archive_tools import find_7z
    exe = find_7z()
    if not exe:
        return False
    try:
        listing = subprocess.run(
            [exe, "l", "-slt", "-ba", "-scsUTF-8", str(container)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return False
    if listing.returncode != 0:
        return False
    # One block per entry. A folder is "Folder = +" (rar, zip) or has D in
    # its Attributes (7z, which has no Folder line).
    names: "dict[str, str]" = {}
    text = listing.stdout.decode("utf-8", "surrogateescape")
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        path = fields.get("Path")
        if not path:
            continue
        attrs = fields.get("Attributes", "").split(" ", 1)[0]
        if fields.get("Folder", "-").strip() == "+" or "D" in attrs:
            continue
        names.setdefault(_norm_member(path), path)
    pick = [names[w] for w in wanted if w in names]
    if not pick or len(pick) != len(wanted):
        return False
    listfile = out.parent / (out.name + ".list")
    listfile.write_text("".join(f"{n}\n" for n in pick), encoding="utf-8",
                        errors="surrogateescape")
    cmd = [exe, "x", str(container), f"-o{out}", "-y", "-spd", "-scsUTF-8",
           f"-mmt={threads}" if threads > 0 else "-mmt=on", f"@{listfile}"]
    try:
        from Utils.mods.mod_install import _run_extractor_cancellable
        rc, _err, _killed = _run_extractor_cancellable(cmd, None, low_priority=low_priority)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        listfile.unlink(missing_ok=True)
    return rc == 0


class ArchiveIndex:
    """Maps a top-level Archive's content hash to its on-disk path and
    resolves ``ArchiveHashPath`` chains to files, extracting what's needed.

    Built for a build that works through the modlist one archive at a time:

    * :meth:`plan` says, before anything is extracted, which members of each
      downloaded archive the install uses, so a 7z/rar archive gets only those
      extracted -- a mod archive often ships several variants, of which a list
      uses one (on LoreRim, 35 GB of whole extractions served 2.9 GB of files);
    * each nested hop is cached by its path chain -- never by hashing the
      intermediate file, which for a BSA inside a 7z meant re-reading the
      whole BSA for every file taken out of it;
    * a zip or BSA is opened once and its member table kept;
    * :meth:`take` puts a file in place, moving the extracted copy on its last
      use rather than copying it;
    * :meth:`release` deletes an archive's scratch files once the build is
      done with it, so the install never holds every extraction at once.
    """

    def __init__(self, scratch_dir: "str | Path"):
        self.scratch_dir = Path(scratch_dir)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self._roots: "dict[str, Path]" = {}
        # path chain (root hash, member, ...) -> extracted file
        self._hop_cache: "dict[tuple[str, ...], Path]" = {}
        # Extracted 7z/rar containers: container path -> {normalised member
        # path -> extracted file}.
        self._extracted: "dict[str, dict[str, Path]]" = {}
        # Members the install will ask for, per root archive hash (normalised).
        self._wanted: "dict[str, set[str]]" = {}
        # Uses left per (normalised) path chain, for move-on-last-use, and
        # chains that are containers of others, which are never moved.
        self._uses: "dict[tuple[str, ...], int]" = {}
        self._pinned: "set[tuple[str, ...]]" = set()
        self._zips: "dict[str, tuple[zipfile.ZipFile, dict[str, str]]]" = {}
        self._bsas: "dict[str, object]" = {}
        self._next_id = 0
        self._lock = threading.RLock()                 # the index's own tables
        self._container_locks: "dict[str, threading.Lock]" = {}
        # Set by a parallel build: how many archives extract at once (7z's
        # threads are shared out), and whether extractors run at low priority.
        self.concurrency = 1
        self.low_priority = False

    def _lock_for(self, container: Path) -> "threading.Lock":
        """One lock per container: different archives extract in parallel,
        two jobs on the same archive wait for each other."""
        with self._lock:
            return self._container_locks.setdefault(str(container), threading.Lock())

    def add_archive(self, content_hash: str, path: "str | Path") -> None:
        """Register a downloaded Archive's on-disk location by its content
        hash (``Archive.hash`` from the modlist)."""
        self._roots[content_hash] = Path(path)

    def plan(self, hash_paths) -> None:
        """Declare every ``ArchiveHashPath`` the build will resolve, so each
        downloaded archive extracts only what's used and an extracted file can
        be moved into place on its last use. Optional: without it every
        archive is extracted whole and every file copied, as before."""
        for chain in hash_paths:
            chain = tuple(chain or ())
            if len(chain) < 2:
                continue
            self._wanted.setdefault(chain[0], set()).add(_norm_member(chain[1]))
            key = _chain_key(chain)
            self._uses[key] = self._uses.get(key, 0) + 1
            # A container the build reads from (Requiem.bsa inside its 7z)
            # can also be installed as a file itself: never move it then.
            for n in range(2, len(chain)):
                self._pinned.add(_chain_key(chain[:n]))

    def _root_dir(self, root_hash: str) -> Path:
        """Scratch space for one downloaded archive and everything inside it."""
        safe = "".join(c if c.isalnum() else "_" for c in root_hash)
        return self.scratch_dir / safe

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
        chain = (root_hash,)
        for member_path in archive_hash_path[1:]:
            chain = chain + (member_path,)
            with self._lock:
                cached = self._hop_cache.get(chain)
            if cached is not None and cached.exists():
                current = cached
                continue
            wanted = self._wanted.get(root_hash) if len(chain) == 2 else None
            current = self._extract_member(current, member_path, root_hash, wanted)
            with self._lock:
                self._hop_cache[chain] = current
        return current

    def take(self, archive_hash_path: "list[str]", dest: Path) -> None:
        """Put the file ``archive_hash_path`` names at *dest*: moved out of
        the scratch space on its last planned use, copied otherwise."""
        source = self.resolve(archive_hash_path)
        chain = tuple(archive_hash_path)
        key = _chain_key(chain)
        with self._lock:
            left = self._uses.get(key, 0) - 1
            if left > 0:
                self._uses[key] = left
            else:
                self._uses.pop(key, None)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if (left <= 0 and len(chain) >= 2 and key not in self._pinned
                and source.is_relative_to(self.scratch_dir)):
            try:
                os.replace(source, dest)
                with self._lock:
                    self._hop_cache.pop(chain, None)
                    self._forget_extracted(source)
                return
            except OSError:
                pass                       # another filesystem: copy instead
        shutil.copyfile(source, dest)

    def _forget_extracted(self, path: Path) -> None:
        for members in self._extracted.values():
            for k, v in list(members.items()):
                if v == path:
                    del members[k]

    def release(self, root_hash: str) -> None:
        """The build is done with this downloaded archive: close it and
        delete everything extracted from it."""
        root_dir = self._root_dir(root_hash)
        root_path = self._roots.get(root_hash)
        with self._lock:
            for chain in [c for c in self._hop_cache if c[0] == root_hash]:
                del self._hop_cache[chain]
            for key in [k for k in self._extracted
                        if Path(k).is_relative_to(root_dir) or Path(k) == root_path]:
                del self._extracted[key]
            for key in [k for k in self._zips
                        if Path(k).is_relative_to(root_dir) or Path(k) == root_path]:
                self._zips.pop(key)[0].close()
            for key in [k for k in self._bsas
                        if Path(k).is_relative_to(root_dir) or Path(k) == root_path]:
                self._bsas.pop(key).close()
            self._wanted.pop(root_hash, None)
            self._pinned = {c for c in self._pinned if c[0] != root_hash}
        shutil.rmtree(root_dir, ignore_errors=True)

    def close(self) -> None:
        with self._lock:
            for zf, _names in self._zips.values():
                zf.close()
            for bsa in self._bsas.values():
                bsa.close()
            self._zips.clear()
            self._bsas.clear()

    def _new_dest(self, root_hash: str, member_path: str) -> Path:
        with self._lock:
            self._next_id += 1
            n = self._next_id
        d = self._root_dir(root_hash)
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{n}_{Path(member_path.replace(chr(92), '/')).name}"

    def _extract_member(self, container: Path, member_path: str, root_hash: str,
                        wanted: "set[str] | None" = None) -> Path:
        ext = container.suffix.lower()
        if ext in _EXTRACT_WHOLE_EXTS or ext in _INNO_EXTS:
            return self._member_of_whole_extract(container, member_path, root_hash, wanted)
        dest = self._new_dest(root_hash, member_path)
        if ext in _ZIP_EXTS:
            try:
                self._extract_from_zip(container, member_path, dest)
            except NotImplementedError:
                # zipfile can't decompress PPMd (98) or Deflate64 (9) members
                # (real cases: VRAMr, Goodneighbor View); native 7z can.
                dest.unlink(missing_ok=True)
                return self._member_of_whole_extract(container, member_path, root_hash, wanted)
            except _BadZipContainer as bad:
                # Not a readable zip to Python, but the hash matched: a RAR/7z
                # saved as ".zip" (KittyVFX), or a zip with a broken central
                # directory (Bow of Shadows). Native 7z sniffs content and
                # tolerates both, as Wabbajack's own extractor does.
                dest.unlink(missing_ok=True)
                try:
                    return self._member_of_whole_extract(container, member_path,
                                                         root_hash, wanted)
                except VfsResolutionError:
                    raise bad
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

    def _zip(self, container: Path) -> "tuple[zipfile.ZipFile, dict[str, str]]":
        """The container opened once, with an index from exact and Windows-
        normalised member names to the zip's own spelling."""
        key = str(container)
        with self._lock:
            got = self._zips.get(key)
        if got is not None:
            return got
        with self._lock_for(container):
            with self._lock:
                got = self._zips.get(key)
            if got is None:
                try:
                    zf = zipfile.ZipFile(container, "r")
                except zipfile.BadZipFile as exc:
                    raise _BadZipContainer(f"{container}: not a valid zip ({exc})") from exc
                names: "dict[str, str]" = {}
                for n in zf.namelist():
                    names.setdefault(n.replace("\\", "/").strip("/"), n)
                for n in zf.namelist():
                    names.setdefault("\0" + _norm_member(n), n)
                got = (zf, names)
                with self._lock:
                    self._zips[key] = got
            return got

    def _extract_from_zip(self, container: Path, member_path: str, dest: Path) -> None:
        zf, names = self._zip(container)
        target = (names.get(member_path.replace("\\", "/").strip("/"))
                  or names.get("\0" + _norm_member(member_path)))
        if target is None:
            raise VfsResolutionError(f"{member_path!r} not found in {container.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock_for(container):     # one ZipFile, one reader at a time
                with zf.open(target) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
        except zipfile.BadZipFile as exc:
            raise _BadZipContainer(f"{container}: not a valid zip ({exc})") from exc

    def _member_of_whole_extract(self, container: Path, member_path: str,
                                 root_hash: str, wanted: "set[str] | None" = None) -> Path:
        """``member_path`` from a 7z/rar/Inno-installer (or PPMd/Deflate64
        zip) container, extracted once into the scratch dir (so on the
        install's own filesystem, never /tmp). With *wanted* -- the members
        the install will use -- only those are extracted."""
        key = str(container)
        with self._lock_for(container):
            with self._lock:
                members = self._extracted.get(key)
            if members is None:
                out = self._new_dest(root_hash, container.stem + ".d")
                out.mkdir(parents=True, exist_ok=True)
                if container.suffix.lower() in _INNO_EXTS:
                    _extract_inno(container, out)
                elif not (wanted and _extract_7z_members(
                        container, out, wanted, threads=self._threads(),
                        low_priority=self.low_priority)):
                    from Utils.mods.mod_install import _extract_archive
                    errors: "list[str]" = []
                    if not _extract_archive(str(container), str(out), lambda _m: None,
                                            error_sink=errors,
                                            concurrent_workers=self.concurrency):
                        detail = (errors[-1].strip() if errors
                                  else "no extractor could read it")
                        raise VfsResolutionError(
                            f"{container}: extraction failed ({detail})")
                members = {_norm_member(p.relative_to(out).as_posix()): p
                           for p in out.rglob("*") if p.is_file()}
                with self._lock:
                    self._extracted[key] = members
        found = members.get(_norm_member(member_path))
        if found is None:
            raise VfsResolutionError(f"{member_path!r} not found in {container.name}")
        return found

    def _bsa(self, container: Path):
        from Utils.archives.bsa_file_reader import BsaFile
        key = str(container)
        with self._lock:
            got = self._bsas.get(key)
            if got is None:
                got = self._bsas[key] = BsaFile(container).__enter__()
            return got

    def _threads(self) -> int:
        """7z threads per extraction: the user's setting, else the cores
        shared between the archives extracting at once."""
        try:
            from Utils.ui_config import load_extraction_settings
            chosen = int(load_extraction_settings().get("cpu_threads", 0) or 0)
        except Exception:
            chosen = 0
        if chosen > 0:
            return chosen
        return max(1, (os.cpu_count() or 2) // max(1, self.concurrency))

    def _extract_from_bethesda(self, container: Path, member_path: str, dest: Path) -> None:
        from Utils.archives.bsa_file_reader import BsaReadError
        try:
            with self._lock_for(container):     # one open archive, one reader at a time
                archive = self._bsa(container)
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

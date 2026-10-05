"""Builds the BSA/BA2 archive a Wabbajack ``CreateBSA`` directive describes.

Wabbajack records the curator's original archive instead of shipping it:
the directive's ``State`` carries the header (BSA version, archive and file
flags; or BA2 version, type and name-table flag) and ``FileStates`` lists
every file inside it with its per-file settings (a BSA file's
``FlipCompression``; a BA2 file's flags, hashes and compression, or a
texture's dimensions, format and chunk layout). The loose files themselves
are produced by earlier directives into ``TEMP_BSA_FILES/<TempID>/``; this
module packs them back up with :func:`bsa_writer.write_bsa_entries` /
:func:`ba2_writer.write_ba2_entries`.

The result has the same structure and contents as the original, but is
generally not byte-identical: Wabbajack compresses with .NET libraries whose
zlib/LZ4 output differs from Python's. The install pipeline therefore checks
each packed file (every input was hash-verified when it was built) rather
than requiring the rebuilt archive's own hash to match.

Field names follow Wabbajack's serialised ``BSAState``/``BSAFileState`` and
``BA2State``/``BA2FileEntryState``/``BA2DX10EntryState`` models; like the
rest of this package they haven't been checked against a real modlist from
this sandbox. Morrowind (TES3) BSAs, PS4 (GNMF) BA2s and Starfield's BA2
versions aren't supported -- :func:`support_problem` names them so preflight
can refuse the modlist up front.
"""
from __future__ import annotations

from pathlib import Path

from Utils.archives.ba2_writer import (
    Ba2Chunk,
    Ba2GeneralEntry,
    Ba2TextureEntry,
    Ba2WriteError,
    write_ba2_entries,
)
from Utils.archives.bsa_writer import BsaEntry, BsaWriteError, write_bsa_entries

from .wabbajack_manifest import CreateBSADirective

_BSA_VERSIONS = (103, 104, 105)
_BA2_VERSIONS = (1, 7, 8)
_BA2_TYPES = {0: "GNRL", 1: "DX10", 2: "GNMF", "GNRL": "GNRL", "DX10": "DX10", "GNMF": "GNMF"}


class ArchiveBuildError(Exception):
    """The archive couldn't be built; the message is safe to show."""


def _kind(d: dict) -> str:
    """Bare lowercase ``$type`` name: ``"BSAState, Compression.BSA"`` -> ``"bsastate"``."""
    return str(d.get("$type", "")).split(",", 1)[0].split(".")[-1].strip().lower()


def _state(directive: CreateBSADirective) -> dict:
    state = directive.raw.get("State")
    return state if isinstance(state, dict) else {}


def _ba2_type(state: dict) -> str:
    raw = state.get("Type", 0)
    return _BA2_TYPES.get(raw if isinstance(raw, int) else str(raw).upper(), str(raw))


def support_problem(directive: CreateBSADirective) -> "str | None":
    """Why this archive can't be built, or ``None`` if it can."""
    state = _state(directive)
    kind = _kind(state)
    if kind == "bsastate":
        version = int(state.get("Version") or 0)
        return None if version in _BSA_VERSIONS else f"BSA version {version}"
    if kind == "ba2state":
        version, ba2_type = int(state.get("Version") or 0), _ba2_type(state)
        if ba2_type not in ("GNRL", "DX10"):
            return f"{ba2_type} BA2 (console format)"
        return None if version in _BA2_VERSIONS else f"BA2 version {version} (Starfield)"
    if kind == "tes3state":
        return "Morrowind-format BSA"
    return f"unknown archive format ({state.get('$type') or 'no type'})"


def _file_states(directive: CreateBSADirective) -> "list[dict]":
    states = [s for s in (directive.raw.get("FileStates") or []) if isinstance(s, dict)]
    return sorted(states, key=lambda s: int(s.get("Index") or 0))


def _source(temp_root: Path, directive: CreateBSADirective, fs: dict) -> "tuple[str, Path]":
    rel = str(fs.get("Path") or "").replace("\\", "/").strip("/")
    if not rel:
        raise ArchiveBuildError("an archive entry has no path")
    src = temp_root / directive.temp_id / rel
    if not src.is_file():
        raise ArchiveBuildError(f"missing file for the archive: {rel}")
    return rel, src


def _opt_int(fs: dict, key: str) -> "int | None":
    value = fs.get(key)
    return None if value is None else int(value)


def build_archive(directive: CreateBSADirective, temp_root: Path, dest: Path) -> None:
    """Pack ``temp_root/<TempID>/`` into ``dest`` as the directive's
    ``State``/``FileStates`` describe. Raises :class:`ArchiveBuildError`."""
    problem = support_problem(directive)
    if problem is not None:
        raise ArchiveBuildError(f"can't build a {problem}")
    if not directive.temp_id:
        raise ArchiveBuildError("the directive has no TempID")
    state = _state(directive)
    file_states = _file_states(directive)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        if _kind(state) == "bsastate":
            archive_flags = int(state.get("ArchiveFlags") or 0)
            default_compress = bool(archive_flags & 0x4)
            entries = []
            for fs in file_states:
                rel, src = _source(temp_root, directive, fs)
                entries.append(BsaEntry(rel, src, default_compress != bool(fs.get("FlipCompression"))))
            write_bsa_entries(dest, entries, version=int(state["Version"]),
                              archive_flags=archive_flags,
                              file_flags=int(state.get("FileFlags") or 0))
            return

        ba2_type = _ba2_type(state)
        entries = []
        for fs in file_states:
            rel, src = _source(temp_root, directive, fs)
            if ba2_type == "GNRL":
                entries.append(Ba2GeneralEntry(
                    rel, src, compress=bool(fs.get("Compressed")),
                    flags=int(fs.get("Flags", 0x00100100) or 0),
                    name_hash=_opt_int(fs, "NameHash"), dir_hash=_opt_int(fs, "DirHash"),
                    ext=fs.get("Extension")))
            else:
                chunks = tuple(
                    Ba2Chunk(int(c.get("FullSz") or 0), int(c.get("StartMip") or 0),
                             int(c.get("EndMip") or 0), bool(c.get("Compressed")))
                    for c in (fs.get("Chunks") or []))
                entries.append(Ba2TextureEntry(
                    rel, src, height=int(fs.get("Height") or 0), width=int(fs.get("Width") or 0),
                    num_mips=int(fs.get("NumMips") or 0),
                    dxgi_format=int(fs.get("PixelFormat") or 0), chunks=chunks,
                    unk8=int(fs.get("Unk8") or 0),
                    chunk_header_len=int(fs.get("ChunkHdrLen") or 24),
                    tile_mode=int(fs.get("TileMode", 2048) or 0),
                    name_hash=_opt_int(fs, "NameHash"), dir_hash=_opt_int(fs, "DirHash")))
        write_ba2_entries(dest, entries, archive_type=ba2_type,
                          version=int(state.get("Version") or 1),
                          name_table=bool(state.get("HasNameTable", True)))
    except (BsaWriteError, Ba2WriteError) as exc:
        raise ArchiveBuildError(str(exc)) from exc

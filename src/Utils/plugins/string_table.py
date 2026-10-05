"""
string_table.py
Minimal ``.STRINGS`` file reader for localized plugin FULL (name) subrecords.

Skyrim's Skyrim.esm/Update.esm/DLC masters and Fallout 4's Fallout4.esm/DLC
masters set the TES4 header's localization flag (bit 0x80 of the header
flags field, byte offset 8). When set, every FULL subrecord is a 4-byte
string ID requiring a lookup in a paired Strings/<Plugin>_<Lang>.STRINGS
file, rather than inline text. Without this, every vanilla armor's Name
would resolve blank.

Only the plain ``.STRINGS`` variant is needed here: ARMO/KYWD's FULL/EDID
subrecords are never descriptions (.DLSTRINGS) or dialogue (.ILSTRINGS), so
those two variants (which use length-prefixed strings, a different format)
are intentionally not implemented.

On a real install the vanilla masters' .STRINGS files are normally packed
inside one of the game's own base BSAs/BA2s (verified: Skyrim SE ships
``Strings/Skyrim_English.STRINGS`` inside ``Skyrim - Interface.bsa``, not
loose) — this is the common case, not an edge case, so both locations are
checked: loose ``Strings/`` next to the plugin first (cheap, no archive
I/O), then every BSA/BA2 sitting alongside the plugin in its own directory
(the plugin and its own base-game archives always live in the same Data
directory). The archive scan is cached per directory so it only runs once
per catalog build, not once per localized plugin.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

_TES4_LOCALIZED_FLAG = 0x80
_HDR = struct.Struct("<II")        # count, data_size
_DIR_ENTRY = struct.Struct("<II")  # string id, offset

# dir path -> {"strings/<name>.strings": archive Path}, built once per directory.
_BSA_STRINGS_CACHE: dict = {}


def is_localized(plugin_path: Path) -> bool:
    """True if the plugin's TES4 header has the localization bit set."""
    try:
        with open(plugin_path, "rb") as f:
            hdr = f.read(12)
    except OSError:
        return False
    if len(hdr) < 12 or hdr[0:4] != b"TES4":
        return False
    flags = struct.unpack_from("<I", hdr, 8)[0]
    return bool(flags & _TES4_LOCALIZED_FLAG)


# Language-token naming is per-game, not just per-plugin: Skyrim/Oblivion/
# FO3/NV use the full name ("Skyrim_English.STRINGS"); Fallout 4 uses a
# short code instead ("Fallout4_en.STRINGS" -- confirmed against a real
# install, no "Fallout4_English.STRINGS" exists). Neither this module nor
# its only caller (armor_record_details) otherwise needs to know which game
# it's reading, so rather than plumb a game_id through, just try every
# convention and use whichever one actually resolves.
_ENGLISH_TOKENS = ("English", "en")


def load_strings_table(plugin_path: Path, language: "str | None" = None) -> dict:
    """{string_id: text} for *plugin_path*'s English .STRINGS table, loose or
    BSA/BA2-packed. Returns {} on any missing file / I/O / format error.
    *language* overrides the default English-token search with one exact
    token, for callers that already know the game's convention."""
    tokens = (language,) if language is not None else _ENGLISH_TOKENS
    for token in tokens:
        name = f"{plugin_path.stem}_{token}.strings".lower()
        data = _read_loose(plugin_path.parent, name)
        if data is None:
            data = _read_from_archives(plugin_path.parent, name)
        if data is not None:
            return _parse_table(data)
    return {}


def _read_loose(data_dir: Path, name_lower: str) -> "bytes | None":
    strings_dir = data_dir / "Strings"
    if not strings_dir.is_dir():
        return None
    try:
        for entry in os.scandir(strings_dir):
            if entry.is_file() and entry.name.lower() == name_lower:
                try:
                    return Path(entry.path).read_bytes()
                except OSError:
                    return None
    except OSError:
        return None
    return None


def _read_from_archives(data_dir: Path, name_lower: str) -> "bytes | None":
    archive_map = _scan_archives(data_dir)
    archive_path = archive_map.get(f"strings/{name_lower}")
    if archive_path is None:
        return None
    try:
        from Utils.archives.bsa_file_reader import BsaFile, BsaReadError
        bsa = BsaFile(archive_path)
        try:
            return bsa.read(f"strings/{name_lower}")
        finally:
            bsa.close()
    except (BsaReadError, ImportError, OSError):
        return None


def _scan_archives(data_dir: Path) -> dict:
    cached = _BSA_STRINGS_CACHE.get(data_dir)
    if cached is not None:
        return cached
    out: dict = {}
    try:
        from Utils.archives.bsa_file_reader import BsaFile, BsaReadError
        candidates = list(data_dir.glob("*.bsa")) + list(data_dir.glob("*.ba2"))
        for archive_path in candidates:
            try:
                bsa = BsaFile(archive_path)
            except BsaReadError:
                continue
            try:
                for path in bsa.paths():
                    if path.startswith("strings/") and path.endswith(".strings"):
                        out.setdefault(path, archive_path)
            finally:
                bsa.close()
    except ImportError:
        pass
    _BSA_STRINGS_CACHE[data_dir] = out
    return out


def _parse_table(data: bytes) -> dict:
    """Format: 8-byte header (count:u32, data_size:u32), then `count` x 8-byte
    directory entries (id:u32, offset:u32), then a blob of null-terminated
    strings (offsets are relative to the start of that blob)."""
    if len(data) < _HDR.size:
        return {}
    try:
        count, data_size = _HDR.unpack_from(data, 0)
        dir_start = _HDR.size
        blob_start = dir_start + count * _DIR_ENTRY.size
        blob = data[blob_start:blob_start + data_size]
        out: dict = {}
        for i in range(count):
            sid, offset = _DIR_ENTRY.unpack_from(data, dir_start + i * _DIR_ENTRY.size)
            end = blob.find(b"\x00", offset)
            if end == -1:
                continue
            out[sid] = blob[offset:end].decode("utf-8", errors="replace")
        return out
    except struct.error:
        return {}

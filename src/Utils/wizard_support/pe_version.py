"""
pe_version.py
Read the four-part file version (e.g. Fallout4.exe 1.11.240.0) from a Windows
PE image's RT_VERSION resource.

Mosaic has no PE library dependency and only needs this one value, so this
walks just far enough through the PE structure to reach the
VS_FIXEDFILEINFO. Returns None for anything it can't positively read — an
unknown version is safer to report than a guessed one.
"""

from __future__ import annotations

import mmap
import os
import struct
from pathlib import Path

Version = tuple[int, int, int, int]

_RT_VERSION = 16
_FIXED_FILE_INFO_SIGNATURE = 0xFEEF04BD
_SUBDIR_FLAG = 0x80000000
_MAX_KEY_CHARS = 64          # "VS_VERSION_INFO" is 15; bound the NUL scan


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def read_file_version(path: "str | Path") -> Version | None:
    """The file version of the PE at *path*, or None if it can't be read."""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size < 64:
                return None
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as image:
                return _parse(image)
    except (OSError, ValueError, struct.error, IndexError):
        return None


def _parse(b) -> Version | None:
    if b[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    if b[pe:pe + 4] != b"PE\0\0":
        return None

    n_sections = struct.unpack_from("<H", b, pe + 6)[0]
    opt_size = struct.unpack_from("<H", b, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", b, opt)[0]
    if magic == 0x10B:          # PE32
        dirs = opt + 96
    elif magic == 0x20B:        # PE32+
        dirs = opt + 112
    else:
        return None
    if struct.unpack_from("<I", b, dirs - 4)[0] <= 2:      # no resource directory slot
        return None
    res_rva = struct.unpack_from("<I", b, dirs + 2 * 8)[0]
    if not res_rva:
        return None

    sections = [
        struct.unpack_from("<8sIIII", b, opt + opt_size + i * 40)[1:]
        for i in range(n_sections)
    ]  # (virtual size, virtual address, raw size, raw pointer)

    def rva_to_offset(rva: int) -> int | None:
        for vsize, vaddr, rawsize, rawptr in sections:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return rawptr + (rva - vaddr)
        return None

    res = rva_to_offset(res_rva)
    if res is None:
        return None

    def first_entry(directory: int, want_id: int | None = None) -> int | None:
        """Offset value of the first (or the *want_id*) entry of a resource
        directory table, or None."""
        n_named, n_id = struct.unpack_from("<HH", b, directory + 12)
        for i in range(n_named + n_id):
            entry_id, offset = struct.unpack_from("<II", b, directory + 16 + i * 8)
            if want_id is None or entry_id == want_id:
                return offset
        return None

    # type (RT_VERSION) → name/id → language → data entry
    level1 = first_entry(res, _RT_VERSION)
    if level1 is None or not level1 & _SUBDIR_FLAG:
        return None
    level2 = first_entry(res + (level1 & ~_SUBDIR_FLAG))
    if level2 is None or not level2 & _SUBDIR_FLAG:
        return None
    level3 = first_entry(res + (level2 & ~_SUBDIR_FLAG))
    if level3 is None or level3 & _SUBDIR_FLAG:
        return None

    data_rva = struct.unpack_from("<I", b, res + level3)[0]
    data = rva_to_offset(data_rva)
    if data is None:
        return None

    # VS_VERSIONINFO: wLength, wValueLength, wType, then the UTF-16 key, then
    # the VS_FIXEDFILEINFO on the next DWORD boundary.
    cursor = data + 6
    for _ in range(_MAX_KEY_CHARS):
        if b[cursor:cursor + 2] == b"\0\0":
            break
        cursor += 2
    else:
        return None
    cursor += 2
    fixed = data + (((cursor - data) + 3) & ~3)

    signature, _struc_version, ms, ls = struct.unpack_from("<IIII", b, fixed)
    if signature != _FIXED_FILE_INFO_SIGNATURE:
        return None
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)

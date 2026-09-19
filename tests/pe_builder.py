"""Shared test helper: build a minimal, valid PE image with a given file version.

Used by the PE-version reader tests and the Fallout 4 downgrade tests, which
need real-enough exes/dlls for ``read_file_version`` without depending on any
game being installed.
"""
from __future__ import annotations

import struct

_RES_RVA = 0x1000
_RES_RAW = 0x400


def build_pe(version: tuple[int, int, int, int], *, pe32_plus: bool = True,
              with_resources: bool = True) -> bytes:
    """A minimal PE whose only resource is RT_VERSION → VS_FIXEDFILEINFO."""
    opt_size = 240 if pe32_plus else 224
    dd_off = 112 if pe32_plus else 96           # data directories, from opt start
    pe_off = 0x40
    opt_off = pe_off + 24

    img = bytearray(_RES_RAW + 0x200)
    img[0:2] = b"MZ"
    struct.pack_into("<I", img, 0x3C, pe_off)
    img[pe_off:pe_off + 4] = b"PE\0\0"
    # COFF: machine, nsections, timestamp, symtab, nsyms, opt size, chars
    struct.pack_into("<HHIIIHH", img, pe_off + 4,
                     0x8664 if pe32_plus else 0x14C, 1, 0, 0, 0, opt_size, 0x22)
    struct.pack_into("<H", img, opt_off, 0x20B if pe32_plus else 0x10B)
    struct.pack_into("<I", img, opt_off + dd_off - 4, 16)      # NumberOfRvaAndSizes
    if with_resources:
        struct.pack_into("<II", img, opt_off + dd_off + 2 * 8, _RES_RVA, 0x200)
    # one section: .rsrc
    struct.pack_into("<8sIIII", img, opt_off + opt_size,
                     b".rsrc", 0x200, _RES_RVA, 0x200, _RES_RAW)

    base = _RES_RAW

    def _dir(at: int, entry_id: int, target: int, subdir: bool) -> None:
        struct.pack_into("<IIHHHH", img, at, 0, 0, 0, 0, 0, 1)   # 1 id entry
        struct.pack_into("<II", img, at + 16, entry_id,
                         (0x80000000 | target) if subdir else target)

    _dir(base + 0x00, 16, 0x18, True)          # type RT_VERSION
    _dir(base + 0x18, 1, 0x30, True)           # name/id 1
    _dir(base + 0x30, 0x409, 0x48, False)      # language → data entry
    data_off = 0x58
    struct.pack_into("<IIII", img, base + 0x48, _RES_RVA + data_off, 92, 0, 0)

    d = base + data_off
    struct.pack_into("<HHH", img, d, 92, 52, 0)
    key = "VS_VERSION_INFO".encode("utf-16-le") + b"\0\0"      # 32 bytes
    img[d + 6:d + 6 + len(key)] = key
    fixed = d + 40                                             # 38 → DWORD-aligned
    ms = (version[0] << 16) | version[1]
    ls = (version[2] << 16) | version[3]
    struct.pack_into("<IIIIII", img, fixed, 0xFEEF04BD, 0x00010000, ms, ls, ms, ls)
    return bytes(img)

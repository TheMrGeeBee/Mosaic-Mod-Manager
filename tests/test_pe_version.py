"""read_file_version() must read a Windows exe's four-part file version.

Real-world motivation: the Fallout 4 downgrade wizard has to know whether the
installed Fallout4.exe is 1.11.240.0 (Anniversary Edition, patchable) or
1.10.163.0 (Old-Gen, already downgraded) before touching anything, and to
verify the patched result. The repo had no PE reader, so a minimal one walks
just far enough to reach the VS_FIXEDFILEINFO in the RT_VERSION resource.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from Utils.wizard_support.pe_version import format_version, read_file_version

_RES_RVA = 0x1000
_RES_RAW = 0x400


def _build_pe(version: tuple[int, int, int, int], *, pe32_plus: bool = True,
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


@pytest.mark.parametrize("pe32_plus", [True, False])
@pytest.mark.parametrize("version", [(1, 11, 240, 0), (1, 10, 163, 0), (0, 7, 9, 65535)])
def test_reads_version_from_pe32_and_pe32_plus(tmp_path, version, pe32_plus):
    exe = tmp_path / "a.exe"
    exe.write_bytes(_build_pe(version, pe32_plus=pe32_plus))
    assert read_file_version(exe) == version


def test_format_version():
    assert format_version((1, 11, 240, 0)) == "1.11.240.0"


def test_pe_without_a_resource_directory_is_none(tmp_path):
    exe = tmp_path / "a.exe"
    exe.write_bytes(_build_pe((1, 2, 3, 4), with_resources=False))
    assert read_file_version(exe) is None


def test_not_a_pe_is_none(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello " * 50)
    assert read_file_version(f) is None


def test_empty_and_truncated_and_missing_are_none(tmp_path):
    empty = tmp_path / "empty.exe"
    empty.write_bytes(b"")
    truncated = tmp_path / "trunc.exe"
    truncated.write_bytes(_build_pe((1, 2, 3, 4))[:0x120])
    assert read_file_version(empty) is None
    assert read_file_version(truncated) is None
    assert read_file_version(tmp_path / "missing.exe") is None


def test_corrupt_signature_is_none(tmp_path):
    raw = bytearray(_build_pe((1, 2, 3, 4)))
    i = raw.index(struct.pack("<I", 0xFEEF04BD))
    raw[i] ^= 0xFF
    exe = tmp_path / "a.exe"
    exe.write_bytes(bytes(raw))
    assert read_file_version(exe) is None


_REAL_FO4 = Path("/home/mrgeebee/games/steamapps/common/Fallout 4/Fallout4.exe")


@pytest.mark.skipif(not _REAL_FO4.is_file(), reason="Fallout 4 not installed here")
def test_real_fallout4_exe_reads_as_a_1_x_version():
    v = read_file_version(_REAL_FO4)
    assert v is not None and v[:2] in {(1, 10), (1, 11)}

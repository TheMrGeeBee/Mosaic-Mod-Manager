"""read_dds_info parses a DDS header without decoding pixels."""
from __future__ import annotations

import struct

from Utils.dds_info import format_size, read_dds_info

_CAPS_MIP = 0x1 | 0x8 | 0x400000
_FLAGS = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000     # caps|h|w|pf|mipcount


def _dds(tmp_path, *, w=256, h=128, mips=9, fourcc=b"DXT5", pf_flags=0x4,
         bits=0, a_mask=0, caps2=0, dxgi=None, name="t.dds", flags=_FLAGS,
         pad=0):
    head = bytearray(128)
    head[0:4] = b"DDS "
    struct.pack_into("<7I", head, 4, 124, flags, h, w, 0, 0, mips)
    struct.pack_into("<2I4s", head, 76, 32, pf_flags, fourcc)
    struct.pack_into("<5I", head, 88, bits, 0, 0, 0, a_mask)
    struct.pack_into("<2I", head, 108, _CAPS_MIP, caps2)
    data = bytes(head)
    if dxgi is not None:
        data += struct.pack("<5I", dxgi, 3, 0, 1, 0)
    data += b"\0" * pad
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_dxt1_dxt5_and_dimensions(tmp_path):
    info = read_dds_info(_dds(tmp_path, fourcc=b"DXT1"))
    assert (info.width, info.height, info.mip_count) == (256, 128, 9)
    assert info.format == "BC1 (DXT1)"
    assert read_dds_info(_dds(tmp_path, fourcc=b"DXT5")).format == "BC3 (DXT5)"


def test_dx10_bc7_and_srgb(tmp_path):
    assert read_dds_info(_dds(tmp_path, fourcc=b"DX10", dxgi=98)).format == "BC7"
    assert read_dds_info(_dds(tmp_path, fourcc=b"DX10", dxgi=99)).format == "BC7 (sRGB)"
    assert read_dds_info(_dds(tmp_path, fourcc=b"DX10", dxgi=999)).format == "DXGI 999"


def test_dx10_header_missing_is_none(tmp_path):
    p = _dds(tmp_path, fourcc=b"DX10")          # says DX10 but no extension follows
    assert read_dds_info(p) is None


def test_uncompressed(tmp_path):
    rgba = _dds(tmp_path, pf_flags=0x40 | 0x1, bits=32, a_mask=0xFF000000, fourcc=b"\0\0\0\0")
    assert read_dds_info(rgba).format == "32-bit RGBA"
    rgb = _dds(tmp_path, pf_flags=0x40, bits=24, fourcc=b"\0\0\0\0", name="b.dds")
    assert read_dds_info(rgb).format == "24-bit RGB"


def test_mip_count_ignored_without_flag(tmp_path):
    info = read_dds_info(_dds(tmp_path, mips=7, flags=_FLAGS & ~0x20000))
    assert info.mip_count == 1


def test_cubemap_and_summary(tmp_path):
    info = read_dds_info(_dds(tmp_path, caps2=0x200, pad=2048))
    assert info.is_cubemap
    assert info.summary().startswith("256×128 cubemap · BC3 (DXT5) · 9 mips · ")


def test_rejects_garbage(tmp_path):
    assert read_dds_info(tmp_path / "missing.dds") is None
    bad = tmp_path / "bad.dds"
    bad.write_bytes(b"PNG\0" + b"\0" * 200)
    assert read_dds_info(bad) is None
    short = tmp_path / "short.dds"
    short.write_bytes(b"DDS " + b"\0" * 20)
    assert read_dds_info(short) is None


def test_format_size():
    assert format_size(500) == "500 B"
    assert format_size(2048) == "2 KB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"

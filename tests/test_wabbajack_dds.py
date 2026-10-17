"""The DDS header the Wabbajack installer writes for a BA2 texture must be
Wabbajack's own (DirectXTexUtil.GenerateDDSHeader): patches are built
against it. Verified 2026-10-08 on Wasteland Genesis AE: with these rules
all 374 patch sources read from Fallout 4's Textures BA2s hash exactly as
the modlist records (BC1, BC3, BC5, B8G8R8A8), and 230 failed patches
built."""
from __future__ import annotations

import struct

from Utils.wabbajack.wabbajack_dds import wabbajack_dds_header


def _fields(h: bytes) -> dict:
    v = struct.unpack("<4s7I11I8I5I", h[:128])
    return {"magic": v[0], "size": v[1], "flags": v[2], "height": v[3], "width": v[4],
            "pitch": v[5], "depth": v[6], "mips": v[7], "pf_size": v[19],
            "pf_flags": v[20], "fourcc": struct.pack("<I", v[21]), "bits": v[22],
            "masks": v[23:27], "caps": v[27], "caps2": v[28]}


def test_bc5_gets_a_legacy_header_with_the_top_mip_size():
    h = wabbajack_dds_header(height=1024, width=2048, mip_count=12, dxgi_format=83)
    f = _fields(h)
    assert len(h) == 128                               # no DX10 extension
    assert (f["magic"], f["size"], f["pf_size"]) == (b"DDS ", 124, 32)
    assert f["fourcc"] == b"BC5U" and f["pf_flags"] == 0x4
    assert f["flags"] == 0x1007 | 0x20000 | 0x80000
    assert f["pitch"] == (2048 // 4) * (1024 // 4) * 16
    assert (f["depth"], f["mips"]) == (1, 12)
    assert f["caps"] == 0x1000 | 0x400008


def test_bc1_and_bc3_are_dxt1_and_dxt5():
    bc1 = _fields(wabbajack_dds_header(height=6, width=6, mip_count=3, dxgi_format=71))
    bc3 = _fields(wabbajack_dds_header(height=4, width=4, mip_count=1, dxgi_format=77))
    assert bc1["fourcc"] == b"DXT1" and bc1["pitch"] == 2 * 2 * 8   # partial blocks round up
    assert bc3["fourcc"] == b"DXT5" and bc3["pitch"] == 16
    assert bc3["caps"] == 0x1000 and bc3["flags"] & 0x20000          # one mip: no COMPLEX


def test_bc7_and_srgb_formats_get_the_dx10_extension():
    h = wabbajack_dds_header(height=256, width=256, mip_count=9, dxgi_format=98)
    assert len(h) == 148 and _fields(h)["fourcc"] == b"DX10"
    assert struct.unpack("<5I", h[128:]) == (98, 3, 0, 1, 0)
    cube = wabbajack_dds_header(height=64, width=64, mip_count=7, dxgi_format=72,
                                is_cubemap=True)
    assert struct.unpack("<5I", cube[128:]) == (72, 3, 4, 1, 0)
    assert _fields(cube)["caps2"] == 0


def test_uncompressed_bgra_uses_a_row_pitch_and_rgba_masks():
    f = _fields(wabbajack_dds_header(height=32, width=50, mip_count=1, dxgi_format=87))
    assert f["flags"] == 0x1007 | 0x20000 | 0x8
    assert f["pitch"] == 50 * 4
    assert (f["pf_flags"], f["bits"]) == (0x41, 32)
    assert f["masks"] == (0x00ff0000, 0x0000ff00, 0x000000ff, 0xff000000)


def test_installer_reads_ba2_textures_with_the_wabbajack_header(tmp_path):
    from Utils.archives._ba2_writer_selftest import _make_synthetic_dds
    from Utils.archives.ba2_writer import write_ba2_textures
    from Utils.archives.bsa_file_reader import BsaFile
    from Utils.wabbajack.wabbajack_vfs import ArchiveIndex

    src = tmp_path / "src"
    (src / "textures/actors").mkdir(parents=True)
    (src / "textures/actors/face_n.dds").write_bytes(
        _make_synthetic_dds(128, 64, mip_count=4, dxgi_format=83))
    ba2 = tmp_path / "Mod - Textures.ba2"
    write_ba2_textures(ba2, src)
    with BsaFile(ba2) as arc:
        info = arc.dx10_info("Textures\\Actors\\Face_n.dds")
        payload = arc.read("textures/actors/face_n.dds")[148:]
    assert info == {"height": 64, "width": 128, "num_mips": 4, "dxgi_format": 83,
                    "is_cubemap": False}

    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("h", ba2)
    out = idx.resolve(["h", "Textures\\Actors\\Face_n.dds"]).read_bytes()
    assert out == wabbajack_dds_header(height=64, width=128, mip_count=4,
                                       dxgi_format=83) + payload

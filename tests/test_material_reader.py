"""Bethesda material files (.bgsm/.bgem) — layout verified byte-for-byte
against 9,732 real files (89 from a real mod, 9,643 from the base Fallout 4
install across both real versions, 1 and 2) while building this reader."""
from __future__ import annotations

import struct

from Utils.nif.material_reader import read_material

_HEADER_LEN = 63


def _pstr(s: str) -> bytes:
    """A material file's own pascal-string encoding: a uint32 length that
    INCLUDES a trailing NUL, then that many bytes."""
    raw = s.encode("utf-8") + b"\x00"
    return struct.pack("<I", len(raw)) + raw


def _bgsm(version=1, diffuse="CROSS\\coa\\coa_02_d.dds", normal="CROSS\\coa\\coa_02_n.dds",
         specular="CROSS\\coa\\coa_02_s.dds", magic=b"BGSM"):
    header = magic + struct.pack("<I", version) + b"\x00" * (_HEADER_LEN - 8)
    return header + _pstr(diffuse) + _pstr(normal) + _pstr(specular)


def _bgem(version=1, diffuse="CROSS\\CROSS_Tex_OFF_d.DDS", second=""):
    header = b"BGEM" + struct.pack("<I", version) + b"\x00" * (_HEADER_LEN - 8)
    return header + _pstr(diffuse) + _pstr(second)


def test_reads_diffuse_normal_specular_from_a_real_layout_bgsm():
    mt = read_material(_bgsm())
    assert mt.diffuse == "textures/cross/coa/coa_02_d.dds"
    assert mt.normal == "textures/cross/coa/coa_02_n.dds"
    assert mt.specular == "textures/cross/coa/coa_02_s.dds"


def test_version_2_is_also_supported_same_header_length():
    # Verified on the real base game (9,643 files, all version 2) — the
    # fixed header before the first texture string is the same 63 bytes.
    mt = read_material(_bgsm(version=2))
    assert mt.diffuse == "textures/cross/coa/coa_02_d.dds"


def test_bgem_only_reads_the_diffuse_slot():
    # A BGEM's second slot isn't diffuse/normal/specular in the same sense a
    # BGSM's is (verified: a real one was an empty "Greyscale" slot) — not
    # read into normal/specular to avoid mislabeling it.
    mt = read_material(_bgem())
    assert mt.diffuse == "textures/cross/cross_tex_off_d.dds"
    assert mt.normal == "" and mt.specular == ""


def test_a_legitimately_empty_diffuse_is_not_an_error():
    # Real decal/neon materials have an empty diffuse slot on purpose.
    mt = read_material(_bgsm(diffuse=""))
    assert mt is not None and mt.diffuse == ""
    assert mt.normal == "textures/cross/coa/coa_02_n.dds"


def test_unrecognised_version_returns_none_rather_than_guess():
    assert read_material(_bgsm(version=99)) is None


def test_wrong_magic_returns_none():
    assert read_material(_bgsm(magic=b"NOPE")) is None


def test_truncated_file_returns_none_not_a_crash():
    data = _bgsm()
    assert read_material(data[:_HEADER_LEN + 2]) is None
    assert read_material(b"BGSM") is None
    assert read_material(b"") is None


def test_a_corrupt_length_prefix_is_handled_gracefully():
    data = bytearray(_bgsm())
    struct.pack_into("<I", data, _HEADER_LEN, 0xFFFFFFF0)   # absurd string length
    assert read_material(bytes(data)) is None

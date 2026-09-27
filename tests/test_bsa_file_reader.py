"""BsaFile: single-file reads from a BSA, checked against the project's own
BSA writer (v104 zlib and v105 LZ4) so no game files are needed."""
from __future__ import annotations

import os

import pytest

from Utils.archives.bsa_file_reader import BsaFile, BsaReadError
from Utils.archives.bsa_writer import write_bsa

FILES = {
    "meshes/armor/iron/m/cuirass_0.nif": b"NIF" * 5000,          # compressible
    "meshes/armor/iron/m/helmet.nif": os.urandom(300),
    "textures/armor/iron/iron_d.dds": b"DDS " + os.urandom(2000),
    "meshes/top.nif": b"x",
}


def _pack(tmp_path, version, compress=True):
    src = tmp_path / "src"
    for rel, data in FILES.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    out = tmp_path / "t.bsa"
    write_bsa(out, src, version=version, compress=compress)
    return out


@pytest.mark.parametrize("version", [104, 105])
@pytest.mark.parametrize("compress", [True, False])
def test_reads_every_file_back(tmp_path, version, compress):
    with BsaFile(_pack(tmp_path, version, compress)) as bsa:
        assert bsa.version == version
        assert sorted(bsa.paths()) == sorted(FILES)
        for rel, data in FILES.items():
            assert bsa.read(rel) == data


def test_lookup_is_case_and_slash_insensitive(tmp_path):
    with BsaFile(_pack(tmp_path, 105)) as bsa:
        assert "Meshes\\Armor\\Iron\\M\\Cuirass_0.NIF" in bsa
        assert bsa.read("MESHES/top.nif") == b"x"
        assert "meshes/nope.nif" not in bsa


def test_missing_file_and_bad_archives(tmp_path):
    with BsaFile(_pack(tmp_path, 105)) as bsa:
        with pytest.raises(BsaReadError):
            bsa.read("meshes/nope.nif")
    junk = tmp_path / "junk.bsa"
    junk.write_bytes(b"NOTABSA" + b"\0" * 100)
    with pytest.raises(BsaReadError):
        BsaFile(junk)
    with pytest.raises(BsaReadError):
        BsaFile(tmp_path / "missing.bsa")
    cut = tmp_path / "cut.bsa"
    cut.write_bytes(_pack(tmp_path, 105).read_bytes()[:50])
    with pytest.raises(BsaReadError):
        BsaFile(cut)


@pytest.mark.parametrize("version", [104, 105])
@pytest.mark.parametrize("compress", [True, False])
def test_read_head_returns_only_the_first_bytes(tmp_path, version, compress):
    with BsaFile(_pack(tmp_path, version, compress)) as bsa:
        rel = "meshes/armor/iron/m/cuirass_0.nif"
        assert bsa.read_head(rel, 16) == FILES[rel][:16]
        assert bsa.read_head("meshes/top.nif", 128) == b"x"          # shorter than n
        assert bsa.read_head(rel, 10**6) == FILES[rel]               # longer than the file
        with pytest.raises(BsaReadError):
            bsa.read_head("meshes/nope.nif")


# -- BA2 (Fallout 4+) ------------------------------------------------------------------
BA2_FILES = {
    "meshes/armor/vault/vaultsuit_0.nif": b"NIF" * 5000,   # compressible
    "meshes/armor/vault/helmet.nif": os.urandom(300),
    "meshes/top.nif": b"x",
}


def _pack_ba2_gnrl(tmp_path, compress=True):
    from Utils.archives.ba2_writer import write_ba2
    src = tmp_path / "src_gnrl"
    for rel, data in BA2_FILES.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    out = tmp_path / "t - Main.ba2"
    write_ba2(out, src, compress=compress)
    return out


@pytest.mark.parametrize("compress", [True, False])
def test_ba2_gnrl_reads_every_file_back(tmp_path, compress):
    with BsaFile(_pack_ba2_gnrl(tmp_path, compress)) as ba2:
        assert ba2._kind == "ba2" and ba2._ba2_type == "GNRL"
        assert sorted(ba2.paths()) == sorted(BA2_FILES)
        for rel, data in BA2_FILES.items():
            assert ba2.read(rel) == data
            assert ba2.read_head(rel, 10) == data[:10]


def test_ba2_lookup_is_case_and_slash_insensitive(tmp_path):
    with BsaFile(_pack_ba2_gnrl(tmp_path)) as ba2:
        assert "Meshes\\Armor\\Vault\\VaultSuit_0.NIF" in ba2
        assert ba2.read("MESHES/top.nif") == b"x"
        assert "meshes/nope.nif" not in ba2
        with pytest.raises(BsaReadError):
            ba2.read("meshes/nope.nif")


def test_ba2_dx10_textures_reassemble_into_valid_dds(tmp_path):
    from Utils.archives._ba2_writer_selftest import _make_synthetic_dds
    from Utils.archives.ba2_writer import write_ba2_textures
    from Utils.dds_info import read_dds_info

    src = tmp_path / "src_dx10"
    single = _make_synthetic_dds(64, 64, mip_count=1, dxgi_format=71)     # BC1
    multi = _make_synthetic_dds(128, 128, mip_count=4, dxgi_format=98)    # BC7
    (src / "textures/armor").mkdir(parents=True)
    (src / "textures/armor/vaultsuit_d.dds").write_bytes(single)
    (src / "textures/armor/vaultsuit_n.dds").write_bytes(multi)
    out = tmp_path / "t - Textures.ba2"
    write_ba2_textures(out, src)

    with BsaFile(out) as ba2:
        assert ba2._ba2_type == "DX10"
        assert sorted(ba2.paths()) == ["textures/armor/vaultsuit_d.dds",
                                       "textures/armor/vaultsuit_n.dds"]
        got_single = ba2.read("textures/armor/vaultsuit_d.dds")
        got_multi = ba2.read("textures/armor/vaultsuit_n.dds")
        # Reassembly is byte-for-byte only from the synthesised header onward;
        # what actually matters to a caller is that it re-parses correctly.
        p1, p2 = tmp_path / "s.dds", tmp_path / "m.dds"
        p1.write_bytes(got_single)
        p2.write_bytes(got_multi)
        i1, i2 = read_dds_info(p1), read_dds_info(p2)
        assert (i1.width, i1.height, i1.mip_count) == (64, 64, 1)
        assert (i2.width, i2.height, i2.mip_count) == (128, 128, 4)
        # read_head on a DX10 record still returns a prefix of the same bytes.
        assert ba2.read_head("textures/armor/vaultsuit_d.dds", 16) == got_single[:16]


def test_ba2_bad_archives(tmp_path):
    junk = tmp_path / "junk.ba2"
    junk.write_bytes(b"NOTABA2" + b"\0" * 100)
    with pytest.raises(BsaReadError):
        BsaFile(junk)
    cut = tmp_path / "cut.ba2"
    cut.write_bytes(_pack_ba2_gnrl(tmp_path).read_bytes()[:30])
    with pytest.raises(BsaReadError):
        BsaFile(cut)

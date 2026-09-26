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

"""Tests for Utils.wabbajack.wabbajack_bsa: which archive formats can be
rebuilt, and rebuilding BSA / BA2 (GNRL and DX10) archives from
Wabbajack-shaped State/FileStates -- each checked by extracting the result
with Mosaic's existing extractors."""
from __future__ import annotations

import os
import struct

import pytest

from Utils.archives.ba2_extract import extract_ba2
from Utils.archives.bsa_extract import extract_bsa
from Utils.wabbajack.wabbajack_bsa import ArchiveBuildError, build_archive, support_problem
from Utils.wabbajack.wabbajack_manifest import parse_directive


def _directive(state, file_states, temp_id="t1"):
    return parse_directive({"$type": "CreateBSA, Wabbajack.Lib", "To": "mods/A/A.bsa",
                            "TempID": temp_id, "State": state, "FileStates": file_states})


def _inputs(tmp_path, files: dict, temp_id="t1"):
    root = tmp_path / "temp"
    for rel, data in files.items():
        p = root / temp_id / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


@pytest.mark.parametrize("state, problem", [
    ({"$type": "BSAState, Compression.BSA", "Version": 105}, None),
    ({"$type": "BSAState, Compression.BSA", "Version": 103}, None),
    ({"$type": "BSAState, Compression.BSA", "Version": 0x100}, "BSA version 256"),
    ({"$type": "BA2State, Compression.BSA", "Version": 1, "Type": 0}, None),
    ({"$type": "BA2State, Compression.BSA", "Version": 8, "Type": "DX10"}, None),
    ({"$type": "BA2State, Compression.BSA", "Version": 1, "Type": 2}, "GNMF BA2 (console format)"),
    ({"$type": "BA2State, Compression.BSA", "Version": 2, "Type": 0}, "BA2 version 2 (Starfield)"),
    ({"$type": "TES3State, Compression.BSA"}, "Morrowind-format BSA"),
    ({}, "unknown archive format (no type)"),
])
def test_support_problem(state, problem):
    assert support_problem(_directive(state, [])) == problem


def test_build_bsa_honours_flags_and_flip_compression(tmp_path):
    files = {"meshes/a.nif": b"N" * 3000, "sound/b.wav": os.urandom(800)}
    root = _inputs(tmp_path, files)
    d = _directive(
        {"$type": "BSAState, Compression.BSA", "Version": 104, "ArchiveFlags": 0x107,
         "FileFlags": 0x3},
        [{"Path": "meshes\\a.nif", "Index": 0, "FlipCompression": False},
         {"Path": "sound\\b.wav", "Index": 1, "FlipCompression": True}])
    dest = tmp_path / "A.bsa"
    build_archive(d, root, dest)

    header = struct.unpack("<4sIIIIIIII", dest.read_bytes()[:36])
    assert header[1] == 104 and header[3] == 0x107 and header[8] == 0x3
    extract_bsa(dest, tmp_path / "out")
    for rel, data in files.items():
        assert (tmp_path / "out" / rel).read_bytes() == data


def test_build_ba2_general(tmp_path):
    files = {"materials/x.bgsm": b"BGSM" * 100, "sound/y.xwm": os.urandom(400)}
    root = _inputs(tmp_path, files)
    d = _directive(
        {"$type": "BA2State, Compression.BSA", "Version": 8, "Type": 0, "HasNameTable": True,
         "HeaderMagic": "BTDX"},
        [{"$type": "BA2FileEntryState", "Path": "materials\\x.bgsm", "Index": 0,
          "Compressed": True, "Flags": 0x00100100, "Extension": "bgsm"},
         {"$type": "BA2FileEntryState", "Path": "sound\\y.xwm", "Index": 1,
          "Compressed": False, "Flags": 0x00100100, "Extension": "xwm"}])
    dest = tmp_path / "A.ba2"
    build_archive(d, root, dest)
    assert struct.unpack("<4sI4s", dest.read_bytes()[:12]) == (b"BTDX", 8, b"GNRL")
    extract_ba2(dest, tmp_path / "out")
    for rel, data in files.items():
        assert (tmp_path / "out" / rel).read_bytes() == data


def test_build_ba2_textures(tmp_path):
    mip0, mip1 = os.urandom(1024), os.urandom(256)
    header = bytearray(128)
    header[:4], header[84:88] = b"DDS ", b"DXT5"
    root = _inputs(tmp_path, {"textures/t.dds": bytes(header) + mip0 + mip1})
    d = _directive(
        {"$type": "BA2State, Compression.BSA", "Version": 1, "Type": 1, "HasNameTable": True},
        [{"$type": "BA2DX10EntryState", "Path": "textures\\t.dds", "Index": 0, "Height": 32,
          "Width": 32, "NumMips": 2, "PixelFormat": 77, "ChunkHdrLen": 24, "Unk8": 0,
          "Chunks": [{"FullSz": len(mip0), "StartMip": 0, "EndMip": 0, "Compressed": True},
                     {"FullSz": len(mip1), "StartMip": 1, "EndMip": 1, "Compressed": False}]}])
    dest = tmp_path / "A - Textures.ba2"
    build_archive(d, root, dest)
    extract_ba2(dest, tmp_path / "out")
    assert (tmp_path / "out" / "textures" / "t.dds").read_bytes()[148:] == mip0 + mip1


def test_build_reports_missing_input(tmp_path):
    d = _directive({"$type": "BSAState, Compression.BSA", "Version": 105, "ArchiveFlags": 3},
                   [{"Path": "meshes\\gone.nif", "Index": 0}])
    with pytest.raises(ArchiveBuildError, match="missing file for the archive: meshes/gone.nif"):
        build_archive(d, tmp_path / "temp", tmp_path / "A.bsa")


def test_build_refuses_unsupported_format(tmp_path):
    d = _directive({"$type": "TES3State, Compression.BSA"}, [])
    with pytest.raises(ArchiveBuildError, match="Morrowind"):
        build_archive(d, tmp_path, tmp_path / "A.bsa")


def test_build_requires_temp_id(tmp_path):
    d = _directive({"$type": "BSAState, Compression.BSA", "Version": 105}, [], temp_id="")
    with pytest.raises(ArchiveBuildError, match="TempID"):
        build_archive(d, tmp_path, tmp_path / "A.bsa")

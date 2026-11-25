"""BSA v103 (Oblivion's format) and FOMM .fomod packages.

Real case (Nuclear Sunset, 2026-11-25): 1,119 files failed to build with
"unsupported BSA version 103" -- older Fallout NV mods (Niv's Spice of Life,
JIP Companions Command and Control, RaceToTheBottom) ship BSAs packed with
Oblivion-era tools. v103 has v104's layout. Another 9 failed because the
Mod Configuration Menu ships as a .fomod, which is a 7z archive."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from Utils.archives.bsa_extract import extract_bsa
from Utils.archives.bsa_file_reader import BsaFile
from Utils.archives.bsa_reader import read_bsa_file_list
from Utils.archives.bsa_writer import write_bsa
from Utils.wabbajack import wabbajack_vfs as vfs

_FILES = {"meshes/armor/hat.nif": b"Gamebryo File Format" + b"x" * 300,
          "textures/armor/hat.dds": b"DDS " + b"y" * 500}


def _v103(tmp_path, compress: bool, flag_0x100: bool = False) -> Path:
    """A v104 BSA relabelled v103 -- the two share one layout. Optionally set
    flag 0x100, which in v103 does *not* mean embedded file names."""
    src = tmp_path / "src"
    for rel, data in _FILES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    bsa = tmp_path / "old.bsa"
    write_bsa(bsa, src, version=104, compress=compress)
    raw = bytearray(bsa.read_bytes())
    flags = struct.unpack_from("<I", raw, 12)[0]
    assert not flags & 0x100
    struct.pack_into("<I", raw, 4, 103)
    if flag_0x100:
        struct.pack_into("<I", raw, 12, flags | 0x100)
    bsa.write_bytes(bytes(raw))
    return bsa


@pytest.mark.parametrize("compress", [True, False])
@pytest.mark.parametrize("flag_0x100", [False, True])
def test_v103_reads_lists_and_extracts(tmp_path, compress, flag_0x100):
    bsa = _v103(tmp_path, compress, flag_0x100)
    with BsaFile(bsa) as b:
        assert b.version == 103
        for rel, data in _FILES.items():
            assert b.read(rel) == data
    assert sorted(p.replace("\\", "/").lower() for p in read_bsa_file_list(bsa)) == sorted(_FILES)
    out = tmp_path / "out"
    extract_bsa(bsa, out)
    for rel, data in _FILES.items():
        assert (out / rel).read_bytes() == data


def test_a_fomod_is_extracted_with_7z(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(vfs.ArchiveIndex, "_member_of_whole_extract",
                        lambda self, c, m, h, w=None: seen.append(c.name) or tmp_path / "x")
    index = vfs.ArchiveIndex(tmp_path / "vfs")
    index._extract_member(tmp_path / "The Mod Configuration Menu.fomod", "MCM.esp", "h")
    assert seen == ["The Mod Configuration Menu.fomod"]

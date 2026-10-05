"""Tests for the explicit-entry archive writers (bsa_writer.write_bsa_entries,
ba2_writer.write_ba2_entries), checked through Mosaic's existing, separately
written extractors: what goes in must come back out byte-for-byte, under the
exact header values the caller asked for."""
from __future__ import annotations

import os
import struct

import pytest

from Utils.archives.ba2_extract import extract_ba2
from Utils.archives.ba2_writer import (
    Ba2Chunk,
    Ba2GeneralEntry,
    Ba2TextureEntry,
    Ba2WriteError,
    ba2_hash,
    write_ba2_entries,
)
from Utils.archives.bsa_extract import extract_bsa
from Utils.archives.bsa_reader import read_bsa_file_list
from Utils.archives.bsa_writer import BsaEntry, BsaWriteError, write_bsa_entries

COMPRESSED_DEFAULT = 0x4
EMBED_NAMES = 0x100
BASE_FLAGS = 0x1 | 0x2


def _files(tmp_path, contents: dict):
    out = {}
    for rel, data in contents.items():
        p = tmp_path / "src" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        out[rel] = p
    return out


CONTENTS = {
    "Meshes/Armor/cuirass.nif": b"NIF" * 2000,
    "textures/armor/cuirass.dds": os.urandom(3000),
    "sound/fx/hit.wav": os.urandom(1500),
}


# ---------------------------------------------------------------------------
# BSA
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version", [104, 105])
@pytest.mark.parametrize("embed", [False, True])
def test_bsa_entries_round_trip_with_flipped_compression(tmp_path, version, embed):
    srcs = _files(tmp_path, CONTENTS)
    flags = BASE_FLAGS | COMPRESSED_DEFAULT | (EMBED_NAMES if embed else 0)
    # Archive default is compressed; the .wav is stored uncompressed (flipped).
    entries = [BsaEntry(rel, p, compress=not rel.endswith(".wav")) for rel, p in srcs.items()]
    bsa = tmp_path / "out.bsa"
    count, size = write_bsa_entries(bsa, entries, version=version, archive_flags=flags,
                                    file_flags=0x1A3)
    assert count == 3 and size == bsa.stat().st_size

    header = struct.unpack("<4sIIIIIIII", bsa.read_bytes()[:36])
    assert header[1] == version and header[3] == flags and header[8] == 0x1A3

    assert sorted(read_bsa_file_list(bsa)) == sorted(r.lower() for r in CONTENTS)
    out = tmp_path / "out"
    extract_bsa(bsa, out)
    for rel, data in CONTENTS.items():
        assert (out / rel.lower()).read_bytes() == data


def test_bsa_entries_uncompressed_default_with_one_compressed_file(tmp_path):
    srcs = _files(tmp_path, CONTENTS)
    entries = [BsaEntry(rel, p, compress=rel.endswith(".nif")) for rel, p in srcs.items()]
    bsa = tmp_path / "out.bsa"
    write_bsa_entries(bsa, entries, version=104, archive_flags=BASE_FLAGS, file_flags=0)
    out = tmp_path / "out"
    extract_bsa(bsa, out)
    for rel, data in CONTENTS.items():
        assert (out / rel.lower()).read_bytes() == data


def test_bsa_entries_v103_header_shares_v104_layout(tmp_path):
    srcs = _files(tmp_path, CONTENTS)
    bsa = tmp_path / "out.bsa"
    write_bsa_entries(bsa, [BsaEntry(r, p, False) for r, p in srcs.items()],
                      version=103, archive_flags=BASE_FLAGS, file_flags=0)
    assert struct.unpack("<I", bsa.read_bytes()[4:8])[0] == 103


@pytest.mark.parametrize("entries, match", [
    ([("root.txt", b"x")], "inside a folder"),
    ([("a/b.txt", b"x"), ("A\\B.TXT", b"y")], "duplicate"),
    ([], "no files"),
])
def test_bsa_entries_rejects_bad_input(tmp_path, entries, match):
    srcs = _files(tmp_path, {f"f{i}": d for i, (_, d) in enumerate(entries)})
    built = [BsaEntry(path, srcs[f"f{i}"], False) for i, (path, _) in enumerate(entries)]
    with pytest.raises(BsaWriteError, match=match):
        write_bsa_entries(tmp_path / "out.bsa", built, version=105,
                          archive_flags=BASE_FLAGS, file_flags=0)


def test_bsa_entries_rejects_unsupported_version(tmp_path):
    with pytest.raises(BsaWriteError, match="unsupported BSA version"):
        write_bsa_entries(tmp_path / "out.bsa", [], version=100, archive_flags=0, file_flags=0)


# ---------------------------------------------------------------------------
# BA2 GNRL
# ---------------------------------------------------------------------------

def test_ba2_general_round_trip_with_version_and_flags(tmp_path):
    srcs = _files(tmp_path, CONTENTS)
    entries = [Ba2GeneralEntry(rel, p, compress=not rel.endswith(".wav"), flags=0x00100100)
               for rel, p in srcs.items()]
    ba2 = tmp_path / "out.ba2"
    count, _size = write_ba2_entries(ba2, entries, archive_type="GNRL", version=8)
    assert count == 3
    magic, version, kind, n, name_off = struct.unpack("<4sI4sIQ", ba2.read_bytes()[:24])
    assert (magic, version, kind, n) == (b"BTDX", 8, b"GNRL", 3) and name_off > 0

    first = struct.unpack("<I4sIIQIII", ba2.read_bytes()[24:60])
    assert first[0] == ba2_hash("cuirass") and first[2] == ba2_hash("meshes\\armor")
    assert first[1] == b"nif\x00"

    out = tmp_path / "out"
    extract_ba2(ba2, out)
    for rel, data in CONTENTS.items():
        assert (out / rel.lower()).read_bytes() == data


def test_ba2_general_uses_given_hashes_and_can_omit_name_table(tmp_path):
    srcs = _files(tmp_path, {"a/b.txt": b"hello"})
    ba2 = tmp_path / "out.ba2"
    write_ba2_entries(ba2, [Ba2GeneralEntry("a/b.txt", srcs["a/b.txt"], False,
                                            name_hash=111, dir_hash=222, ext="txt")],
                      archive_type="GNRL", name_table=False)
    data = ba2.read_bytes()
    assert struct.unpack("<Q", data[16:24])[0] == 0
    rec = struct.unpack("<I4sIIQIII", data[24:60])
    assert (rec[0], rec[2]) == (111, 222)
    assert data[rec[4]:rec[4] + 5] == b"hello"


def test_ba2_rejects_starfield_versions(tmp_path):
    with pytest.raises(Ba2WriteError, match="unsupported BA2 version"):
        write_ba2_entries(tmp_path / "x.ba2", [], archive_type="GNRL", version=2)


# ---------------------------------------------------------------------------
# BA2 DX10
# ---------------------------------------------------------------------------

def _dds(path, pixels: bytes, fourcc: bytes):
    header = bytearray(128)
    header[:4] = b"DDS "
    header[84:88] = fourcc
    extra = bytes(20) if fourcc == b"DX10" else b""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + extra + pixels)


@pytest.mark.parametrize("fourcc", [b"DX10", b"DXT1"])
def test_ba2_dx10_chunks_round_trip(tmp_path, fourcc):
    mip0, mip1 = os.urandom(2048), os.urandom(512)
    src = tmp_path / "src" / "tex.dds"
    _dds(src, mip0 + mip1, fourcc)
    entry = Ba2TextureEntry(
        "Textures/Armor/Tex.dds", src, height=32, width=32, num_mips=2, dxgi_format=71,
        chunks=(Ba2Chunk(len(mip0), 0, 0, True), Ba2Chunk(len(mip1), 1, 1, False)))
    ba2 = tmp_path / "out.ba2"
    write_ba2_entries(ba2, [entry], archive_type="DX10", version=1)

    rec = struct.unpack("<I4sIBBHHHBBH", ba2.read_bytes()[24:48])
    assert rec[4] == 2 and (rec[6], rec[7], rec[8], rec[9], rec[10]) == (32, 32, 2, 71, 2048)

    out = tmp_path / "out"
    extract_ba2(ba2, out)
    rebuilt = (out / "textures/armor/tex.dds").read_bytes()
    assert rebuilt[148:] == mip0 + mip1  # extractor synthesises a 148-byte DX10 header


def test_ba2_dx10_rejects_chunk_layout_mismatch(tmp_path):
    src = tmp_path / "src" / "tex.dds"
    _dds(src, os.urandom(100), b"DX10")
    entry = Ba2TextureEntry("t/tex.dds", src, height=4, width=4, num_mips=1, dxgi_format=71,
                            chunks=(Ba2Chunk(64, 0, 0, False),))
    with pytest.raises(Ba2WriteError, match="chunk layout"):
        write_ba2_entries(tmp_path / "out.ba2", [entry], archive_type="DX10")


def test_ba2_rejects_mixed_entry_kinds(tmp_path):
    srcs = _files(tmp_path, {"a/b.txt": b"x"})
    with pytest.raises(Ba2WriteError, match="must all be"):
        write_ba2_entries(tmp_path / "out.ba2", [Ba2GeneralEntry("a/b.txt", srcs["a/b.txt"], False)],
                          archive_type="DX10")

"""Tests for Utils.wabbajack.wabbajack_vfs's ArchiveHashPath resolution
(single- and multi-hop, zip and 7z containers, caching, and the documented
failure cases)."""
from __future__ import annotations

import zipfile

import py7zr
import pytest

from Utils.wabbajack.wabbajack_vfs import ArchiveIndex, VfsResolutionError


def _make_zip(path, members: dict):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _make_7z(path, members: dict):
    with py7zr.SevenZipFile(path, "w") as arc:
        for name, data in members.items():
            import io
            arc.writef(io.BytesIO(data), name)


def test_resolve_single_hop_zip(tmp_path):
    archive = tmp_path / "a.zip"
    _make_zip(archive, {"mod/file.esp": b"plugin bytes"})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)

    resolved = idx.resolve(["hash1", "mod/file.esp"])
    assert resolved.read_bytes() == b"plugin bytes"


def test_resolve_single_hop_7z(tmp_path):
    archive = tmp_path / "a.7z"
    _make_7z(archive, {"mod/file.esp": b"7z plugin bytes"})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)

    resolved = idx.resolve(["hash1", "mod/file.esp"])
    assert resolved.read_bytes() == b"7z plugin bytes"


def test_resolve_nested_zip_in_zip(tmp_path):
    inner = tmp_path / "inner_payload.zip"
    _make_zip(inner, {"deep/data.bin": b"deeply nested bytes"})
    outer = tmp_path / "outer.zip"
    _make_zip(outer, {"nested/inner.zip": inner.read_bytes()})

    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("outer-hash", outer)

    resolved = idx.resolve(["outer-hash", "nested/inner.zip", "deep/data.bin"])
    assert resolved.read_bytes() == b"deeply nested bytes"


def test_resolve_caches_hop_and_does_not_re_extract(tmp_path, monkeypatch):
    archive = tmp_path / "a.zip"
    _make_zip(archive, {"inner.7z": b"placeholder"})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)

    calls = []
    orig = ArchiveIndex._extract_from_zip

    def counting_extract(container, member_path, dest):
        calls.append(member_path)
        return orig(container, member_path, dest)

    monkeypatch.setattr(ArchiveIndex, "_extract_from_zip", staticmethod(counting_extract))

    # Resolving the same one-hop path twice should only extract once.
    first = idx.resolve(["hash1", "inner.7z"])
    second = idx.resolve(["hash1", "inner.7z"])
    assert first == second
    assert calls == ["inner.7z"]


def test_resolve_empty_path_raises():
    idx = ArchiveIndex("/tmp/doesnotneedtoexistyet")
    with pytest.raises(VfsResolutionError):
        idx.resolve([])


def test_resolve_unregistered_archive_raises(tmp_path):
    idx = ArchiveIndex(tmp_path / "scratch")
    with pytest.raises(VfsResolutionError, match="not downloaded/registered"):
        idx.resolve(["missing-hash", "file.esp"])


def test_resolve_missing_member_in_zip_raises(tmp_path):
    archive = tmp_path / "a.zip"
    _make_zip(archive, {"present.esp": b"x"})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)
    with pytest.raises(VfsResolutionError, match="not found"):
        idx.resolve(["hash1", "absent.esp"])


def test_resolve_unsupported_container_extension_raises(tmp_path):
    archive = tmp_path / "a.rar"
    archive.write_bytes(b"not actually a rar, just needs to exist")
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)
    with pytest.raises(VfsResolutionError, match="not a supported nested-container format"):
        idx.resolve(["hash1", "some/inner/path"])


def test_resolve_corrupt_zip_raises_vfs_error(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"this is not a zip file")
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)
    with pytest.raises(VfsResolutionError, match="not a valid zip"):
        idx.resolve(["hash1", "whatever"])


def test_resolve_file_inside_bsa_inside_zip(tmp_path):
    """The classic Wabbajack chain: a downloaded archive that ships a BSA,
    with the directive reaching a file inside that BSA."""
    from Utils.archives.bsa_writer import BsaEntry, write_bsa_entries
    loose = tmp_path / "loose" / "a.nif"
    loose.parent.mkdir()
    loose.write_bytes(b"NIF IN A BSA" * 50)
    bsa = tmp_path / "Mod.bsa"
    write_bsa_entries(bsa, [BsaEntry("meshes/a.nif", loose, True)], version=105,
                      archive_flags=0x7, file_flags=0)
    outer = tmp_path / "download.zip"
    _make_zip(outer, {"Data/Mod.bsa": bsa.read_bytes()})

    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("dl-hash", outer)
    resolved = idx.resolve(["dl-hash", "Data/Mod.bsa", "meshes\\a.nif"])
    assert resolved.read_bytes() == b"NIF IN A BSA" * 50


def test_resolve_missing_member_in_bsa_raises(tmp_path):
    from Utils.archives.bsa_writer import BsaEntry, write_bsa_entries
    loose = tmp_path / "a.nif"
    loose.write_bytes(b"x")
    bsa = tmp_path / "Mod.bsa"
    write_bsa_entries(bsa, [BsaEntry("meshes/a.nif", loose, False)], version=104,
                      archive_flags=0x3, file_flags=0)
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("h", bsa)
    with pytest.raises(VfsResolutionError, match="not found"):
        idx.resolve(["h", "meshes/missing.nif"])

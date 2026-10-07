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


@pytest.mark.parametrize("make, ext", [(_make_zip, "zip"), (_make_7z, "7z")])
def test_resolve_windows_member_path(tmp_path, make, ext):
    """Real modlists (Horizon, 2026-10-07) name members with backslashes and
    Windows casing (``f4se_0_06_23\\Data\\Scripts\\Math.pex``); zip and 7z
    store ``f4se_0_06_23/Data/Scripts/Math.pex``."""
    archive = tmp_path / f"a.{ext}"
    make(archive, {"f4se_0_06_23/Data/Scripts/Math.pex": b"pex bytes",
                   "Interface/BarterMenu.swf": b"swf bytes"})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)

    assert idx.resolve(["hash1", "f4se_0_06_23\\Data\\Scripts\\Math.pex"]).read_bytes() == b"pex bytes"
    assert idx.resolve(["hash1", "interface\\bartermenu.swf"]).read_bytes() == b"swf bytes"


def test_resolve_7z_when_system_temp_is_another_filesystem(tmp_path, monkeypatch):
    """Real install (Horizon, 2026-10-07): /tmp is a tmpfs, the profile is on
    /home, and moving a 7z member extracted into the system temp dir failed
    with EXDEV. Extraction must stay on the scratch dir's filesystem."""
    import errno
    import os
    import tempfile
    from pathlib import Path

    archive = tmp_path / "a.7z"
    _make_7z(archive, {"mod/file.esp": b"7z plugin bytes"})
    scratch = tmp_path / "scratch"
    other_fs = tmp_path / "system-tmp"
    other_fs.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(other_fs))

    real_replace, real_rename = Path.replace, os.rename

    def _cross_device(src, dst):
        if Path(src).resolve().is_relative_to(other_fs.resolve()) and \
                not Path(dst).resolve().is_relative_to(other_fs.resolve()):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))

    def fake_replace(self, target):
        _cross_device(self, target)
        return real_replace(self, target)

    def fake_rename(src, dst, *a, **k):
        _cross_device(src, dst)
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(Path, "replace", fake_replace)
    monkeypatch.setattr(os, "rename", fake_rename)

    idx = ArchiveIndex(scratch)
    idx.add_archive("hash1", archive)
    resolved = idx.resolve(["hash1", "mod/file.esp"])
    assert resolved.read_bytes() == b"7z plugin bytes"
    assert resolved.resolve().is_relative_to(scratch.resolve())
    assert list(other_fs.iterdir()) == []


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
    archive = tmp_path / "a.exe"
    archive.write_bytes(b"not an archive format the VFS looks inside")
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)
    with pytest.raises(VfsResolutionError, match="not a supported nested-container format"):
        idx.resolve(["hash1", "some/inner/path"])


def test_resolve_unreadable_rar_raises_vfs_error(tmp_path):
    archive = tmp_path / "a.rar"
    archive.write_bytes(b"not actually a rar")
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)
    with pytest.raises(VfsResolutionError, match="extraction failed"):
        idx.resolve(["hash1", "some/inner/path"])


def test_resolve_7z_extracts_container_once(tmp_path, monkeypatch):
    """Every member of a 7z comes from one whole extraction, not one
    decompression pass per member."""
    from Utils.mods import mod_install
    archive = tmp_path / "a.7z"
    _make_7z(archive, {"Data/a.esp": b"a", "Data/b.esp": b"b", "Root/x.exe": b"x"})
    calls = []
    real = mod_install._extract_archive
    monkeypatch.setattr(mod_install, "_extract_archive",
                        lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)
    assert [idx.resolve(["hash1", p]).read_bytes()
            for p in ("Data\\a.esp", "data\\B.ESP", "Root\\x.exe")] == [b"a", b"b", b"x"]
    assert calls == [str(archive)]


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

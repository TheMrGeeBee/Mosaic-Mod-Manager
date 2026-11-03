"""Tests for Utils.wabbajack.wabbajack_vfs's ArchiveHashPath resolution
(single- and multi-hop, zip and 7z containers, caching, and the documented
failure cases)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    # Not ".exe": that is an Inno Setup installer, which the VFS does look
    # inside (see the innoextract tests below).
    archive = tmp_path / "a.dmg"
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


@pytest.mark.skipif(shutil.which("7z") is None, reason="needs native 7z")
def test_resolve_7z_saved_with_zip_extension(tmp_path):
    """Nexus authors sometimes ship a RAR/7z named ".zip"; the modlist hash
    still matches, so it must resolve via the content-sniffing extractor."""
    real = tmp_path / "real.7z"
    _make_7z(real, {"Meshes/a.nif": b"nif bytes"})
    archive = tmp_path / "misnamed.zip"
    real.rename(archive)
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)
    assert idx.resolve(["hash1", "Meshes\\a.nif"]).read_bytes() == b"nif bytes"


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


@pytest.mark.skipif(shutil.which("7z") is None, reason="needs the native 7z")
def test_resolve_ppmd_zip_falls_back_to_7z(tmp_path):
    """Python's zipfile can't decompress PPMd (method 98) members; real
    Nexus zips use it (Goodneighbor View, Wasteland Gardening)."""
    src = tmp_path / "src" / "Data"
    src.mkdir(parents=True)
    (src / "plugin.esp").write_bytes(b"ppmd plugin bytes " * 50)
    archive = tmp_path / "ppmd.zip"
    subprocess.run(["7z", "a", "-tzip", "-mm=PPMd", str(archive), "Data"],
                   cwd=tmp_path / "src", check=True, capture_output=True)
    with zipfile.ZipFile(archive) as zf:
        assert {i.compress_type for i in zf.infolist() if not i.is_dir()} == {98}
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", archive)
    assert idx.resolve(["hash1", "Data\\Plugin.esp"]).read_bytes() == b"ppmd plugin bytes " * 50


# --- Inno Setup installers (".exe") -----------------------------------------
#
# Wabbajack extracts these with innoextract and records the member paths
# innoextract produces, including its "#component" collision suffixes. These
# tests stub innoextract on PATH so the real code path runs without shipping a
# 572 MB installer; see test_wabbajack_vfs_inno_real for the live check.

def _fake_innoextract(bin_dir, tree: dict, *, exit_code: int = 0, log=None):
    """A stub `innoextract` that records its argv and writes ``tree``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "innoextract"
    argv_log = log if log is not None else bin_dir / "argv.txt"
    payload = repr({k: v.decode() for k, v in tree.items()})
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(argv_log)!r}).write_text(repr(sys.argv[1:]))\n"
        f"if {exit_code} != 0:\n"
        "    print('Done with 1 error.'); sys.exit(%d)\n" % exit_code +
        "args = sys.argv[1:]\n"
        "out = pathlib.Path(args[args.index('--output-dir') + 1])\n"
        f"for name, data in {payload}.items():\n"
        "    f = out / name\n"
        "    f.parent.mkdir(parents=True, exist_ok=True)\n"
        "    f.write_text(data)\n")
    script.chmod(0o755)
    return argv_log


def test_resolve_file_inside_inno_installer(tmp_path, monkeypatch):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ... an Inno Setup installer")
    bin_dir = tmp_path / "bin"
    _fake_innoextract(bin_dir, {"app/Unofficial_Patch/vdata/system/stats.txt": b"patched"})
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", installer)
    # Modlists record the Windows spelling of the inner path.
    out = idx.resolve(["hash1", r"app\Unofficial_Patch\vdata\system\stats.txt"])
    assert out.read_text() == "patched"


def test_inno_extraction_asks_for_every_collision_to_be_suffixed(tmp_path, monkeypatch):
    """``--collisions=rename-all`` is what lines the member paths up with the
    modlist: an Inno installer ships several component variants of one file,
    and Wabbajack refers to them as ``stats.txt#basic``/``#plus``. The default
    mode leaves one variant unsuffixed, which would resolve the wrong bytes."""
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ")
    bin_dir = tmp_path / "bin"
    argv_log = _fake_innoextract(bin_dir, {
        "app/vdata/stats.txt#basic": b"basic variant",
        "app/vdata/stats.txt#plus": b"plus variant",
    })
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", installer)

    assert idx.resolve(["hash1", r"app\vdata\stats.txt#basic"]).read_text() == "basic variant"
    assert idx.resolve(["hash1", r"app\vdata\stats.txt#plus"]).read_text() == "plus variant"

    argv = eval(argv_log.read_text())
    assert "--collisions=rename-all" in argv
    # Forcing a language would drop a multi-language installer's other variants.
    assert not any(a.startswith("--default-language") for a in argv)


def test_inno_installer_is_extracted_only_once(tmp_path, monkeypatch):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ")
    bin_dir = tmp_path / "bin"
    _fake_innoextract(bin_dir, {"app/a.txt": b"A", "app/b.txt": b"B"})
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", installer)

    calls = []
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (calls.append(a), real_run(*a, **k))[1])
    assert idx.resolve(["hash1", r"app\a.txt"]).read_text() == "A"
    assert idx.resolve(["hash1", r"app\b.txt"]).read_text() == "B"
    assert len(calls) == 1, "a 572 MB installer must not be re-extracted per member"


def test_inno_installer_without_innoextract_says_how_to_fix_it(tmp_path, monkeypatch):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", installer)
    with pytest.raises(VfsResolutionError, match="innoextract is not installed"):
        idx.resolve(["hash1", r"app\a.txt"])


def test_inno_extraction_failure_raises_vfs_error(tmp_path, monkeypatch):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ not really an Inno installer")
    bin_dir = tmp_path / "bin"
    _fake_innoextract(bin_dir, {}, exit_code=1)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("hash1", installer)
    with pytest.raises(VfsResolutionError, match="innoextract failed"):
        idx.resolve(["hash1", r"app\a.txt"])

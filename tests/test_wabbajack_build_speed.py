"""The Wabbajack build's archive handling: same files, much less work.

Measured on 120 real LoreRim archives (2026-11-25): the build took a 7z or
rar apart whole even when the list used a few files of it (35 GB of
extractions for 2.9 GB of files), re-hashed a BSA inside a 7z for every file
read out of it, re-read a zip's table for every member, copied every
extracted file, and re-hashed all 280 GB of downloads on every run."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import py7zr
import pytest

from Utils.wabbajack import wabbajack_hash as wh
from Utils.wabbajack.wabbajack_vfs import ArchiveIndex

needs_7z = pytest.mark.skipif(not (shutil.which("7z") or shutil.which("7zz")),
                              reason="native 7z not installed")


def _7z(path: Path, files: dict) -> Path:
    with py7zr.SevenZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(data, name)
    return path


def _zip(path: Path, files: dict) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def _files(d: Path) -> "set[str]":
    return {p.name for p in d.rglob("*") if p.is_file()}


@needs_7z
def test_a_planned_7z_extracts_only_the_files_the_install_uses(tmp_path):
    arc = _7z(tmp_path / "mod.7z", {"Variant A/a.esp": b"A" * 10,
                                    "Variant B/b.esp": b"B" * 10,
                                    "Docs/readme.txt": b"r"})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("h", arc)
    idx.plan([["h", "Variant A\\A.ESP"]])           # Windows path, any case
    got = idx.resolve(["h", "Variant A\\A.ESP"])
    assert got.read_bytes() == b"A" * 10
    assert _files(tmp_path / "scratch") == {"a.esp"}


def test_without_a_plan_a_7z_is_still_extracted_whole(tmp_path):
    arc = _7z(tmp_path / "mod.7z", {"a.esp": b"A", "b.esp": b"B"})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("h", arc)
    assert idx.resolve(["h", "a.esp"]).read_bytes() == b"A"
    assert _files(tmp_path / "scratch") == {"a.esp", "b.esp"}


@needs_7z
def test_the_last_use_moves_the_file_and_earlier_uses_copy_it(tmp_path):
    arc = _7z(tmp_path / "mod.7z", {"shared.dds": b"S" * 100})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("h", arc)
    idx.plan([["h", "shared.dds"], ["h", "Shared.dds"]])   # two mods, one file
    first, second = tmp_path / "out" / "1.dds", tmp_path / "out" / "2.dds"
    idx.take(["h", "shared.dds"], first)
    assert first.read_bytes() == b"S" * 100
    assert _files(tmp_path / "scratch") == {"shared.dds"}   # still needed
    idx.take(["h", "Shared.dds"], second)
    assert second.read_bytes() == b"S" * 100
    assert _files(tmp_path / "scratch") == set()             # moved, not copied


@needs_7z
def test_a_container_the_build_reads_from_is_never_moved(tmp_path):
    # Requiem's 7z: core/Requiem.bsa is installed as a file, and other files
    # are read out of it.
    inner = _zip(tmp_path / "inner.zip", {"meshes/a.nif": b"NIF"})
    arc = _7z(tmp_path / "mod.7z", {"core/inner.zip": inner.read_bytes()})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("h", arc)
    idx.plan([["h", "core\\inner.zip"], ["h", "core\\inner.zip", "meshes\\a.nif"]])
    idx.take(["h", "core\\inner.zip"], tmp_path / "out" / "inner.zip")
    nif = tmp_path / "out" / "a.nif"
    idx.take(["h", "core\\inner.zip", "meshes\\a.nif"], nif)
    assert nif.read_bytes() == b"NIF"


def test_a_nested_hop_is_not_hashed_for_every_file(tmp_path, monkeypatch):
    inner = _zip(tmp_path / "inner.zip", {f"f{i}.txt": b"x" for i in range(5)})
    outer = _zip(tmp_path / "outer.zip", {"inner.zip": inner.read_bytes()})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("h", outer)
    import Utils.wabbajack.wabbajack_hash as hmod
    calls = []
    monkeypatch.setattr(hmod, "hash_file", lambda p, *a: calls.append(p) or "x")
    for i in range(5):
        assert idx.resolve(["h", "inner.zip", f"f{i}.txt"]).read_bytes() == b"x"
    assert calls == []


def test_a_zip_is_opened_once_for_all_its_members(tmp_path, monkeypatch):
    arc = _zip(tmp_path / "big.zip", {f"Textures/T{i}.dds": b"d" for i in range(50)})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("h", arc)
    opened = []
    real = zipfile.ZipFile
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: opened.append(a) or real(*a, **k))
    for i in range(50):
        idx.resolve(["h", f"textures\\t{i}.DDS"])
    assert len(opened) == 1


def test_release_deletes_an_archives_scratch_files(tmp_path):
    arc = _7z(tmp_path / "mod.7z", {"a.esp": b"A"})
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive("h", arc)
    idx.resolve(["h", "a.esp"])
    assert _files(tmp_path / "scratch")
    idx.release("h")
    assert _files(tmp_path / "scratch") == set()
    # and it still works if asked again
    assert idx.resolve(["h", "a.esp"]).read_bytes() == b"A"


def test_downloads_are_hashed_once_until_they_change(tmp_path, monkeypatch):
    f = tmp_path / "dl" / "mod.7z"
    f.parent.mkdir()
    f.write_bytes(b"archive bytes")
    calls = []
    real = wh.hash_file
    monkeypatch.setattr(wh, "hash_file", lambda p, *a: calls.append(p) or real(p))
    monkeypatch.setattr(wh, "_hash_caches", {})
    first = wh.cached_hash_file(f)
    wh.flush_hash_cache()
    monkeypatch.setattr(wh, "_hash_caches", {})          # a new run reads the file
    assert wh.cached_hash_file(f) == first
    assert len(calls) == 1
    f.write_bytes(b"different bytes!")                    # changed: hashed again
    assert wh.cached_hash_file(f) != first
    assert len(calls) == 2
    assert (f.parent / wh.HASH_CACHE_NAME).is_file()

"""Tests for reproducing a collection author's FOMOD install from the
manifest's ``hashes`` list.

Real files and real md5s in ``tmp_path`` — no mocks. The resolver only walks a
directory and hashes files, so nothing needs faking.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from Utils.mods.mod_install import _resolve_files_from_hashes


def write(root, rel, data: bytes):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def entry(path, data: bytes):
    return {"path": path, "md5": md5(data)}


def test_exact_path_match_resolves(tmp_path):
    root = str(tmp_path)
    data = b"plugin-bytes"
    write(root, "Foo.esp", data)
    got = _resolve_files_from_hashes(root, [entry("Foo.esp", data)], lambda m: None)
    assert got == [("Foo.esp", "Foo.esp", False)]


def test_renamed_file_resolves_by_md5(tmp_path):
    """The Thwack case: the FOMOD installs
    ``Patches/Vigil Enforcer Distribution/X.esp`` as plain ``X.esp``, so the
    manifest's destination path does not exist in the archive at all."""
    root = str(tmp_path)
    data = b"vigil-enforcer"
    write(root, "Patches/Vigil Enforcer Distribution/Thwack.esp", data)
    got = _resolve_files_from_hashes(root, [entry("Thwack.esp", data)], lambda m: None)
    assert got is not None
    src, dst, is_folder = got[0]
    assert src == os.path.join("Patches", "Vigil Enforcer Distribution", "Thwack.esp")
    assert dst == "Thwack.esp"
    assert is_folder is False


def test_unselected_alternative_is_not_staged(tmp_path):
    """Two mutually exclusive FOMOD options share a basename; only the one the
    author's md5 names may be picked."""
    root = str(tmp_path)
    chosen, other = b"the-author-picked-this", b"the-other-option"
    write(root, "Patches/A/Patch.esp", chosen)
    write(root, "Patches/B/Patch.esp", other)
    got = _resolve_files_from_hashes(root, [entry("Patch.esp", chosen)], lambda m: None)
    assert got is not None
    assert got[0][0] == os.path.join("Patches", "A", "Patch.esp")


def test_same_basename_wrong_md5_is_not_matched(tmp_path):
    root = str(tmp_path)
    write(root, "Foo.esp", b"actual-content")
    got = _resolve_files_from_hashes(
        root, [{"path": "Foo.esp", "md5": md5(b"different-content")}], lambda m: None)
    assert got is None


def test_backslash_paths_from_the_manifest_are_normalised(tmp_path):
    """Manifests are Windows-authored: paths arrive with backslashes."""
    root = str(tmp_path)
    data = b"translation"
    write(root, "interface/translations/X_english.txt", data)
    got = _resolve_files_from_hashes(
        root, [entry("interface\\translations\\X_english.txt", data)], lambda m: None)
    assert got is not None
    assert got[0][1] == os.path.join("interface", "translations", "X_english.txt")


def test_one_unresolvable_entry_fails_the_whole_set(tmp_path):
    """All-or-nothing: a partial match would silently produce a broken mod."""
    root = str(tmp_path)
    a, b = b"file-a", b"file-b"
    write(root, "A.esp", a)
    got = _resolve_files_from_hashes(
        root, [entry("A.esp", a), entry("B.esp", b)], lambda m: None)
    assert got is None


def test_failure_is_logged_with_the_offending_path(tmp_path):
    root = str(tmp_path)
    write(root, "A.esp", b"a")
    logs: list = []
    _resolve_files_from_hashes(root, [entry("Missing.esp", b"nope")], logs.append)
    assert logs and "Missing.esp" in logs[0]


@pytest.mark.parametrize("bad", [
    {"path": "", "md5": "abc"},
    {"path": "A.esp", "md5": ""},
    {"path": "A.esp"},
    {"md5": "abc"},
])
def test_malformed_entries_abort_rather_than_being_skipped(tmp_path, bad):
    root = str(tmp_path)
    write(root, "A.esp", b"a")
    assert _resolve_files_from_hashes(root, [bad], lambda m: None) is None


def test_empty_tree_returns_none(tmp_path):
    assert _resolve_files_from_hashes(
        str(tmp_path), [entry("A.esp", b"a")], lambda m: None) is None


def test_duplicate_content_at_two_paths_both_resolve(tmp_path):
    """Two destinations sharing one md5 (a file installed twice) must both
    resolve rather than collapsing to a single entry."""
    root = str(tmp_path)
    data = b"shared"
    write(root, "shared.dds", data)
    got = _resolve_files_from_hashes(
        root, [entry("textures/a.dds", data), entry("textures/b.dds", data)],
        lambda m: None)
    assert got is not None and len(got) == 2
    assert {d for _s, d, _f in got} == {
        os.path.join("textures", "a.dds"), os.path.join("textures", "b.dds")}


def test_md5_is_matched_case_insensitively(tmp_path):
    """Manifests are inconsistent about hex case."""
    root = str(tmp_path)
    data = b"content"
    write(root, "A.esp", data)
    got = _resolve_files_from_hashes(
        root, [{"path": "A.esp", "md5": md5(data).upper()}], lambda m: None)
    assert got is not None


def test_every_entry_is_reported_as_a_file_never_a_folder(tmp_path):
    root = str(tmp_path)
    d1, d2 = b"one", b"two"
    write(root, "meshes/a.nif", d1)
    write(root, "textures/b.dds", d2)
    got = _resolve_files_from_hashes(
        root, [entry("meshes/a.nif", d1), entry("textures/b.dds", d2)], lambda m: None)
    assert got is not None
    assert all(is_folder is False for _s, _d, is_folder in got)

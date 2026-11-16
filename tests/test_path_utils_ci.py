"""Utils.path_utils.find_child_ci / resolve_ci -- the one shared
case-insensitive walk that replaced a dozen private copies."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from Utils import path_utils
from Utils.path_utils import find_child_ci, has_path_traversal, resolve_ci


def _touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _fs_is_case_sensitive(tmp_path: Path) -> bool:
    (tmp_path / "CaseProbe").write_bytes(b"")
    return not (tmp_path / "caseprobe").exists()


@pytest.fixture
def tree(tmp_path):
    """tmp/Game/Data/Textures/Sky.DDS, tmp/Game/Data/Skyrim.ESM and tmp/Game/readme.txt"""
    game = tmp_path / "Game"
    _touch(game / "Data" / "Textures" / "Sky.DDS")
    _touch(game / "Data" / "Skyrim.ESM")
    _touch(game / "readme.txt")
    return game


# -- find_child_ci -----------------------------------------------------------

def test_find_child_exact_match(tree):
    assert find_child_ci(tree, "Data") == tree / "Data"


def test_find_child_different_case(tree):
    assert find_child_ci(tree, "DATA") == tree / "Data"
    assert find_child_ci(tree / "Data", "skyrim.esm") == tree / "Data" / "Skyrim.ESM"


def test_find_child_missing_returns_none(tree):
    assert find_child_ci(tree, "Saves") is None


def test_find_child_unreadable_or_missing_directory_returns_none(tmp_path):
    assert find_child_ci(tmp_path / "nope", "x") is None


@pytest.mark.parametrize("name", ["..", "."])
def test_find_child_never_returns_dot_entries(tree, name):
    assert find_child_ci(tree / "Data", name) is None
    assert find_child_ci(tree / "Data", name, listings={}) is None


def test_find_child_in_a_file_returns_none(tree):
    assert find_child_ci(tree / "readme.txt", "x") is None


def test_find_child_accepts_str_directory(tree):
    assert find_child_ci(str(tree), "data") == tree / "Data"


def test_exact_case_wins_over_other_spellings(tmp_path):
    if not _fs_is_case_sensitive(tmp_path):
        pytest.skip("case-insensitive filesystem")
    _touch(tmp_path / "foo")
    _touch(tmp_path / "Foo")
    _touch(tmp_path / "FOO")

    assert find_child_ci(tmp_path, "Foo") == tmp_path / "Foo"
    assert find_child_ci(tmp_path, "foo") == tmp_path / "foo"
    assert find_child_ci(tmp_path, "FOO") == tmp_path / "FOO"


def test_ambiguous_spellings_pick_deterministically(tmp_path):
    if not _fs_is_case_sensitive(tmp_path):
        pytest.skip("case-insensitive filesystem")
    for name in ("fOo", "FOO", "Foo"):
        _touch(tmp_path / name)

    picks = {find_child_ci(tmp_path, "foo", listings={}) for _ in range(5)}
    picks.add(find_child_ci(tmp_path, "foo"))      # no-cache path agrees too

    assert picks == {tmp_path / "FOO"}             # sorts first


def test_listings_cache_reads_each_directory_once(tree, monkeypatch):
    calls = []
    real = path_utils.os.scandir
    monkeypatch.setattr(path_utils.os, "scandir", lambda p: calls.append(Path(p)) or real(p))
    listings: dict = {}

    for name in ("data", "DATA", "Data"):
        find_child_ci(tree, name, listings=listings)
    for name in ("skyrim.esm", "SKYRIM.ESM", "missing.esm"):
        find_child_ci(tree / "Data", name, listings=listings)

    assert calls == [tree, tree / "Data"]


# -- resolve_ci --------------------------------------------------------------

def test_resolve_walks_every_component_ignoring_case(tree):
    assert resolve_ci(tree, "data/textures/sky.dds") == tree / "Data" / "Textures" / "Sky.DDS"


def test_resolve_accepts_windows_separators(tree):
    assert resolve_ci(tree, "DATA\\Skyrim.esm") == tree / "Data" / "Skyrim.ESM"


@pytest.mark.parametrize("rel", ["data/skyrim.esm", "/data//skyrim.esm", "./data/./skyrim.esm"])
def test_resolve_ignores_empty_and_dot_components(tree, rel):
    assert resolve_ci(tree, rel) == tree / "Data" / "Skyrim.ESM"


def test_resolve_accepts_path_and_component_list(tree):
    expected = tree / "Data" / "Skyrim.ESM"
    assert resolve_ci(tree, Path("data") / "skyrim.esm") == expected
    assert resolve_ci(tree, PurePosixPath("data/skyrim.esm")) == expected
    assert resolve_ci(tree, ["data", "skyrim.esm"]) == expected


def test_resolve_missing_component_returns_none(tree):
    assert resolve_ci(tree, "data/missing/sky.dds") is None
    assert resolve_ci(tree, "data/textures/missing.dds") is None


def test_resolve_cannot_escape_the_root(tree):
    assert resolve_ci(tree / "Data", "../readme.txt") is None


def test_resolve_through_a_file_returns_none(tree):
    assert resolve_ci(tree, "readme.txt/anything") is None


def test_resolve_empty_rel_returns_root(tree):
    assert resolve_ci(tree, "") == tree
    assert resolve_ci(tree, "", kind="dir") == tree
    assert resolve_ci(tree, "", kind="file") is None


def test_resolve_kind_checks_the_final_component_only(tree):
    assert resolve_ci(tree, "DATA", kind="dir") == tree / "Data"
    assert resolve_ci(tree, "DATA", kind="file") is None
    assert resolve_ci(tree, "data/skyrim.esm", kind="file") == tree / "Data" / "Skyrim.ESM"
    assert resolve_ci(tree, "data/skyrim.esm", kind="dir") is None
    assert resolve_ci(tree, "data", kind="any") == tree / "Data"


def test_resolve_rejects_unknown_kind(tree):
    with pytest.raises(ValueError):
        resolve_ci(tree, "data", kind="folder")


def test_resolve_shares_listings_across_calls(tree, monkeypatch):
    calls = []
    real = path_utils.os.scandir
    monkeypatch.setattr(path_utils.os, "scandir", lambda p: calls.append(Path(p)) or real(p))
    listings: dict = {}

    for _ in range(10):
        resolve_ci(tree, "DATA/SKYRIM.ESM", listings=listings)
        resolve_ci(tree, "data/textures/sky.dds", listings=listings)

    assert sorted(calls) == sorted([tree, tree / "Data", tree / "Data" / "Textures"])


def test_resolve_exact_path_does_not_list_directories(tree, monkeypatch):
    def boom(_path):
        raise AssertionError("scandir called although the exact path exists")
    monkeypatch.setattr(path_utils.os, "scandir", boom)

    assert resolve_ci(tree, "Data/Textures/Sky.DDS") == tree / "Data" / "Textures" / "Sky.DDS"


# -- has_path_traversal (unchanged) -------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("foo/bar", False), ("file..name.ext", False), ("foo/../bar", True),
    ("..", True), ("/etc/passwd", True), ("\\windows", True), ("a\\..\\b", True),
])
def test_has_path_traversal(path, expected):
    assert has_path_traversal(path) is expected

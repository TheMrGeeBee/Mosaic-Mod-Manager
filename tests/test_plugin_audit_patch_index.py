"""Plugin Audit's patch index and SkyGen-INI lookup.

``_build_patch_index`` walks every mod folder once (it used to rglob each one
five times) and ``_has_skygen_ini`` takes a prebuilt index so a scan does not
re-walk the SkyGen output trees for every plugin.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Utils.plugins import plugin_audit_core as core  # noqa: E402


def _w(p: Path, text: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def mods(tmp_path):
    m = tmp_path / "ModA"
    _w(m / "Foo_swap.ini")                                  # bare swap ini at the root
    _w(m / "x" / "y" / "Bar_swap.ini")                      # ... or deep in the mod
    _w(m / "Baz_SWAP.ini")                                  # case-sensitive: ignored
    _w(m / "q" / "Qux_distr.ini")                           # SPID
    _w(m / "Quux_KID.ini")                                  # KID
    _w(m / "data" / "Zed_DSD.json")                         # DSD
    _w(m / "Zed2_DSD.yaml")
    _w(m / "Other.json")                                    # not a patch file
    _w(m / "SKSE/Plugins/Data/Base Object Swapper/Thing_SkyGen_SWAP.ini", "; for Named.esp")
    _w(m / "SKSE/Plugins/SkyPatcher/1F-Other_SkyGen.ini")
    return tmp_path


def test_patch_index_finds_every_kind_of_patch(mods):
    bos, sp = core._build_patch_index(mods)
    assert {"foo.esp", "foo.esm", "foo.esl", "bar.esp", "named.esp"} <= bos
    assert not any(n.startswith("baz") for n in bos)        # *_swap.ini is case-sensitive
    assert {"qux.esp", "quux.esl", "zed.esp", "zed2.esm", "other.esp"} <= sp
    assert "thing.esp" not in bos                           # header says "for Named.esp"


def test_walk_is_case_sensitive_and_skips_symlinked_folders(tmp_path):
    m = tmp_path / "M"
    _w(m / "real" / "A_swap.ini")
    os.symlink(m / "real", m / "link")                      # not followed
    os.symlink(tmp_path / "nowhere", m / "broken_distr.ini")
    swaps, files = core._walk_patch_candidates(m)
    assert swaps == ["A_swap.ini"]                          # once, not via the link
    bos, sp = core._build_patch_index(tmp_path)
    assert "broken.esp" not in sp                           # broken symlink is not a file


def _skygen_tree(root: Path) -> None:
    bos = root / "SkyGen BOS"
    _w(bos / "SKSE/Plugins/Data/Base Object Swapper/Alpha_SkyGen_SWAP.ini")
    _w(bos / "deep" / "Beta_SkyGen_SWAP.ini")               # legacy naming, anywhere
    sp = root / "SkyGen SkyPatcher/SKSE/Plugins/SkyPatcher"
    _w(sp / "03-Delta_SkyGen.ini")                          # load-order prefix
    _w(sp / "Eps.ini")
    _w(sp / "sub" / "0A-Zeta_SkyGen.ini")


@pytest.mark.parametrize("plugin,expected", [
    ("Alpha.esp", True), ("Beta.esp", True), ("Delta.esp", True),
    ("Eps.esp", True), ("Zeta.esp", True), ("ALPHA.ESM", True),
    ("Nope.esp", False),
])
def test_has_skygen_ini_with_and_without_a_prebuilt_index(tmp_path, plugin, expected):
    _skygen_tree(tmp_path)
    idx = core._SkygenIniIndex(tmp_path)
    assert core._has_skygen_ini(plugin, tmp_path) is expected
    assert core._has_skygen_ini(plugin, tmp_path, idx) is expected


def test_prebuilt_index_does_not_walk_the_tree_again(tmp_path, monkeypatch):
    _skygen_tree(tmp_path)
    idx = core._SkygenIniIndex(tmp_path)

    def boom(self, *a, **k):
        raise AssertionError("tree was walked again")
    monkeypatch.setattr(Path, "rglob", boom)
    assert core._has_skygen_ini("Beta.esp", tmp_path, idx)
    assert core._has_skygen_ini("Zeta.esp", tmp_path, idx)
    assert not core._has_skygen_ini("Nope.esp", tmp_path, idx)


def test_missing_mods_folder_is_empty(tmp_path):
    assert core._build_patch_index(tmp_path / "absent") == (set(), set())
    assert core._has_skygen_ini("A.esp", tmp_path / "absent") is False

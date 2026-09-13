"""finish_install() must recognize an existing mod folder even when its
casing differs from the freshly-resolved mod_name.

A plain ``(staging_root / mod_name).exists()`` (the previous behaviour) misses
a same-mod folder that differs only in case — on a case-sensitive filesystem
this makes an update look like a brand-new install: the Replace/Rename/Cancel
prompt never fires, and the mod falls through to modlist.py's prepend_mod
(top-of-list) instead of ensure_mod_preserving_position, so it visibly jumps
to the opposite end of the list under reverse-priority sort. Reported by a
user who downloads archives manually from the Nexus website (not through the
built-in updater/Collections path that a prior case-sensitivity fix already
covered in modlist.py).
"""
from __future__ import annotations

from Utils.mods.mod_install import _find_existing_mod_folder


def test_finds_a_differently_cased_existing_folder(tmp_path):
    (tmp_path / "Engine Fixes - Main File").mkdir()
    found = _find_existing_mod_folder(tmp_path, "engine fixes - main file")
    assert found == tmp_path / "Engine Fixes - Main File"


def test_finds_an_exact_case_match_too(tmp_path):
    (tmp_path / "Some Mod").mkdir()
    found = _find_existing_mod_folder(tmp_path, "Some Mod")
    assert found == tmp_path / "Some Mod"


def test_returns_none_when_nothing_matches(tmp_path):
    (tmp_path / "Unrelated Mod").mkdir()
    assert _find_existing_mod_folder(tmp_path, "Some Mod") is None


def test_ignores_files_only_matches_directories(tmp_path):
    (tmp_path / "notadir").write_text("stub")
    assert _find_existing_mod_folder(tmp_path, "notadir") is None


def test_empty_staging_root_returns_none(tmp_path):
    assert _find_existing_mod_folder(tmp_path, "Anything") is None

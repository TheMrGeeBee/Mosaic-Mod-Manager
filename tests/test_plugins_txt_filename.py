"""Fallout 4 must deploy its load order to the engine's real filename, "Plugins.txt".

Real-world motivation: Fallout 4 inherited Fallout_3's lowercase ``plugins.txt``
default. On a case-sensitive host the game (under Wine) reads its own exact-case
``Plugins.txt`` — the two-line "downloaded content" stub it creates on first
launch — so every deploy wrote a correct 713-plugin list to a file the game
never opened. The game then loaded only the official masters, and every F4SE
plugin that resolves a form by plugin name logged it as "not found"
(RustyFaceFix: ``Rusty Face Fix.esp|800 not found!``, RobCo Patcher: 76 plugins
"not found or is not a valid plugin file"). Same bug, same fix as SkyrimSE.
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from Games.Bethesda.fallout_3 import Fallout_3
from Games.Bethesda.fallout_4 import Fallout_4
from Games.Bethesda.fallout_nv import Fallout_NV

_STUB = (
    "# This file is used by Fallout 4 to keep track of your downloaded content.\n"
    "# Please do not modify this file.\n"
)
_LOAD_ORDER = "*Unofficial Fallout 4 Patch.esp\n*Rusty Face Fix.esp\n"


def test_fallout4_uses_the_engines_capital_p_filename():
    assert Fallout_4().plugins_txt_filename == "Plugins.txt"


def test_deploy_targets_use_the_engine_casing(tmp_path):
    appdata = tmp_path / Fallout_4._APPDATA_SUBPATH
    appdata.mkdir(parents=True)
    assert Fallout_4()._plugins_txt_targets(tmp_path) == [appdata / "Plugins.txt"]


def test_a_lowercase_value_persisted_by_the_old_default_is_not_an_override():
    """paths.json always stores the effective filename, so every existing install
    has ``"plugins.txt"`` saved. It must not resurrect the wrong name."""
    game = Fallout_4()
    game._load_paths_extra({"plugins_txt_filename": "plugins.txt"})
    assert game.plugins_txt_filename == "Plugins.txt"


def test_a_genuinely_different_user_override_still_wins():
    game = Fallout_4()
    game._load_paths_extra({"plugins_txt_filename": "Other.txt"})
    assert game.plugins_txt_filename == "Other.txt"


def test_deploy_replaces_the_game_made_stub_instead_of_writing_a_sibling(tmp_path, monkeypatch):
    """The real regression: a game-created ``Plugins.txt`` stub is already there.
    The deploy must overwrite *that* file — not drop a lowercase twin beside it
    for Wine to ignore."""
    prefix = tmp_path / "pfx"
    appdata = prefix / Fallout_4._APPDATA_SUBPATH
    appdata.mkdir(parents=True)
    (appdata / "Plugins.txt").write_text(_STUB)

    profile_root = tmp_path / "profile_root"
    (profile_root / "profiles" / "prof").mkdir(parents=True)
    (profile_root / "profiles" / "prof" / "plugins.txt").write_text(_LOAD_ORDER)

    game = Fallout_4()
    game._prefix_path = prefix
    monkeypatch.setattr(game, "get_profile_root", lambda: profile_root)
    game._symlink_plugins_txt("prof", lambda _m: None)

    names = sorted(p.name for p in appdata.iterdir())
    assert names == ["Plugins.txt"], f"expected a single file, got {names}"
    deployed = appdata / "Plugins.txt"
    assert deployed.read_text() == _LOAD_ORDER
    # Fallout 4's launcher would rewrite it, so the deploy still locks it.
    assert not deployed.stat().st_mode & stat.S_IWUSR


@pytest.mark.parametrize("cls", [Fallout_3, Fallout_NV])
def test_the_fix_is_scoped_to_fallout4(cls):
    """Fallout 3 / New Vegas keep the inherited default — this change is not a
    blanket casing flip."""
    assert cls().plugins_txt_filename == "plugins.txt"

"""Comparing the installed game version with the one a Modhold loadout targets."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from Modhold.game_versions import (
    MISMATCH,
    OK,
    UNKNOWN,
    WORKS_ON,
    check,
    installed_game_version,
    normalize,
    same_version,
)


@pytest.mark.parametrize("text,expected", [
    ("1.7.104", (1, 7, 104)), ("1.7.104.0", (1, 7, 104, 0)), ("v1.6.1170", (1, 6, 1170)),
    (" 2.1 ", (2, 1)), ("1.6.1170a", None), ("latest", None), ("1", None), ("", None), (None, None),
    (1.5, None),
])
def test_normalize(text, expected):
    assert normalize(text) == expected


def test_a_trailing_zero_part_is_the_same_build():
    assert same_version("1.7.104.0", "1.7.104") and same_version("1.7.104", "1.7.104.0")
    assert not same_version("1.7.104", "1.6.1170")
    assert not same_version("1.7.104", "1.7.1040")
    assert not same_version("x", "x")                    # unparseable never matches


@pytest.mark.parametrize("installed,target,works,status", [
    ("1.7.104", "1.6.1170", (), MISMATCH),
    ("1.6.1170", "1.6.1170", (), OK),
    ("1.6.1170.0", "1.6.1170", (), OK),
    ("1.7.104", "1.6.1170", ("1.6.1179", "1.7.104.0"), WORKS_ON),
    ("1.7.104", "1.6.1170", ("1.6.1179",), MISMATCH),
    ("", "1.6.1170", (), UNKNOWN),                       # game version can't be read
    ("1.7.104", "", (), UNKNOWN),                        # loadout doesn't say
    ("1.7.104", "latest", (), UNKNOWN),                  # not a version number
    ("Build 24914197", "1.6.1170", (), UNKNOWN),
])
def test_check_statuses(installed, target, works, status):
    vc = check(installed, target, works)
    assert vc.status == status
    assert vc.mismatch == (status == MISMATCH)
    assert (vc.installed, vc.target, vc.works_on) == (installed, target, tuple(works))


def _game(tmp_path, **kw):
    return SimpleNamespace(**{"name": "Skyrim SE", "steam_id": "489830", "exe_name": "other.exe",
                              "get_game_path": lambda: tmp_path, **kw})


def test_installed_game_version_uses_the_wizard_reading(monkeypatch, tmp_path):
    from Utils.steam import depot_versions as dv
    seen = {}
    monkeypatch.setattr(dv, "load_table",
                        lambda app_id, user_file=None: SimpleNamespace(exe_file="SkyrimSE.exe"))
    monkeypatch.setattr(dv, "installed_version",
                        lambda root, state, exe: seen.update(root=root, exe=exe) or "1.7.104")
    monkeypatch.setattr("Utils.config_paths.get_game_config_dir", lambda name: tmp_path)
    assert installed_game_version(_game(tmp_path)) == "1.7.104"
    assert seen == {"root": tmp_path, "exe": "SkyrimSE.exe"}          # the table's exe, not exe_name


def test_a_game_without_a_version_table_is_unknown_not_its_build_number(monkeypatch, tmp_path):
    """Cyberpunk's exe says 3.0.88.46235 where mods say 2.3: comparing would raise false warnings."""
    from Utils.steam import depot_versions as dv
    monkeypatch.setattr(dv, "load_table", lambda app_id, user_file=None: SimpleNamespace(exe_file=""))
    monkeypatch.setattr(dv, "installed_version", lambda *a: "3.0.88.46235")
    monkeypatch.setattr("Utils.config_paths.get_game_config_dir", lambda name: tmp_path)
    assert installed_game_version(_game(tmp_path, name="Cyberpunk 2077", steam_id="1091500")) == ""


def test_a_game_with_no_steam_id_is_unknown(monkeypatch, tmp_path):
    assert installed_game_version(_game(tmp_path, steam_id="")) == ""


def test_the_shipped_tables_are_what_make_a_game_comparable():
    """Skyrim SE and Fallout 4 ship a table; a game without one must not be compared."""
    from Utils.steam import depot_versions as dv
    assert dv.load_table("489830").exe_file == "SkyrimSE.exe"
    assert dv.load_table("377160").exe_file == "Fallout4.exe"
    assert dv.load_table("1091500").exe_file == ""                      # Cyberpunk 2077


@pytest.mark.parametrize("game", [
    object(), SimpleNamespace(), SimpleNamespace(get_game_path=lambda: None),
    SimpleNamespace(name="G", get_game_path=lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
])
def test_an_unreadable_game_gives_an_empty_version_and_never_raises(game):
    assert installed_game_version(game) == ""

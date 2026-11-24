"""A profile can run on its own game copy (a "Stock Game" folder next to a
modlist, like MO2's) through the existing per-profile game_path override. Code
that means *Steam's* install (Proton prefix lookup, Steam builds, runtime swaps,
Wabbajack game files, store launchers) must keep using the Steam install."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import Games.base_game as base_game
from Games.Bethesda.skyrim_se import SkyrimSE
from Utils.exe_launch import exe_launch
from Utils.profile.profile_state import merge_profile_settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    steam = tmp_path / "lib" / "steamapps" / "common" / "Skyrim Special Edition"
    steam.mkdir(parents=True)
    copy = tmp_path / "Modlists" / "LoreRim" / "Stock Game"
    copy.mkdir(parents=True)
    paths_json = tmp_path / "cfg" / "paths.json"
    paths_json.parent.mkdir(parents=True)
    paths_json.write_text(json.dumps({"game_path": str(steam)}), encoding="utf-8")
    monkeypatch.setattr(base_game, "get_game_config_path", lambda _n: paths_json)
    monkeypatch.setattr(base_game, "_find_steam_prefix", lambda *_a: None)
    root = tmp_path / "staging"
    profiles = root / "profiles"
    for name in ("default", "LoreRim", "OtherLibrary"):
        (profiles / name).mkdir(parents=True)
    g = SkyrimSE()
    monkeypatch.setattr(g, "get_profile_root", lambda: root)
    return g, steam, copy, profiles


def _use(g, profile_dir):
    g.set_active_profile_dir(profile_dir)
    g.load_paths()


def test_default_profile_is_the_steam_install(env):
    g, steam, _copy, profiles = env
    _use(g, profiles / "default")
    assert g.get_game_path() == steam == g.get_steam_install_path()
    assert not g.uses_game_copy()


def test_profile_on_a_copy_keeps_the_steam_install_separate(env):
    g, steam, copy, profiles = env
    merge_profile_settings(profiles / "LoreRim", {"game_path": str(copy)})
    _use(g, profiles / "LoreRim")
    assert g.get_game_path() == copy
    assert g.get_steam_install_path() == steam
    assert g.uses_game_copy()


def test_profile_on_another_steam_library_is_not_a_copy(env, tmp_path):
    g, _steam, _copy, profiles = env
    other = tmp_path / "lib2" / "steamapps" / "common" / "Skyrim Special Edition"
    other.mkdir(parents=True)
    merge_profile_settings(profiles / "OtherLibrary", {"game_path": str(other)})
    _use(g, profiles / "OtherLibrary")
    assert g.get_steam_install_path() == other
    assert not g.uses_game_copy()


def test_prefix_lookup_uses_the_steam_install(env, monkeypatch):
    """A copy is in no Steam library: looking the prefix up through it falls back
    to the first Steam root's compatdata, a prefix the game may never use."""
    g, steam, copy, profiles = env
    seen = []
    monkeypatch.setattr(base_game, "_find_steam_prefix",
                        lambda sid, path: seen.append(path) or None)
    merge_profile_settings(profiles / "LoreRim", {"game_path": str(copy)})
    _use(g, profiles / "LoreRim")
    assert seen and set(seen) == {steam}


@pytest.mark.parametrize("mode", ["steam", "heroic", "lutris", "auto"])
def test_a_copy_is_launched_from_its_own_exe(env, monkeypatch, mode):
    g, _steam, copy, profiles = env
    merge_profile_settings(profiles / "LoreRim", {"game_path": str(copy)})
    _use(g, profiles / "LoreRim")
    exe = copy / "SkyrimSE.exe"
    exe.write_bytes(b"MZ")
    calls = []
    monkeypatch.setattr(exe_launch, "load_launch_mode", lambda *_a: mode)
    monkeypatch.setattr(exe_launch, "launch_via_steam", lambda *a: calls.append("steam"))
    monkeypatch.setattr(exe_launch, "launch_via_heroic", lambda *a: calls.append("heroic") or True)
    monkeypatch.setattr(exe_launch, "launch_via_lutris", lambda *a: calls.append("lutris") or True)
    monkeypatch.setattr(exe_launch, "heroic_app_names_for_launch", lambda _g: ["x"])
    monkeypatch.setattr(exe_launch, "lutris_slugs_for_launch", lambda _g: ["x"])
    monkeypatch.setattr(exe_launch, "resolve_game_exe", lambda _g: exe)
    monkeypatch.setattr(exe_launch, "launch_exe_via_proton",
                        lambda p, _g, _l=None: calls.append(("proton", p)))
    logs = []
    exe_launch.launch_game(g, log_fn=logs.append)
    assert calls == [("proton", exe)]
    if mode != "auto":
        assert any("own game copy" in m for m in logs)


def test_steam_install_still_launches_through_steam(env, monkeypatch):
    g, _steam, _copy, profiles = env
    _use(g, profiles / "default")
    calls = []
    monkeypatch.setattr(exe_launch, "load_launch_mode", lambda *_a: "steam")
    monkeypatch.setattr(exe_launch, "launch_via_steam", lambda sid, _l: calls.append(sid))
    exe_launch.launch_game(g)
    assert calls == ["489830"]


def test_version_changing_wizards_are_marked_steam_install_only():
    from wizards_qt import get_spec
    only = {"wizards.game_version.GameVersionWizard",
            "wizards.skyrim_runtime.SkyrimRuntimeWizard",
            "wizards.fallout_downgrade.FalloutDowngradeWizard",
            "wizards.fallout4_downgrade.Fallout4DowngradeWizard"}
    for path in only:
        assert get_spec(path).steam_install_only, path
    assert not get_spec("wizards.wrye_bash.WryeBashWizard").steam_install_only


def test_modhold_reads_a_copys_version_from_its_exe(env, monkeypatch):
    from Modhold import game_versions
    from Utils.steam import depot_versions as dv
    g, _steam, copy, profiles = env
    merge_profile_settings(profiles / "LoreRim", {"game_path": str(copy)})
    _use(g, profiles / "LoreRim")
    monkeypatch.setattr(dv, "_exe_version_at",
                        lambda root, exe: "1.6.1170" if Path(root) == copy else "?")
    monkeypatch.setattr(dv, "installed_version", lambda *a: "1.7.104")
    assert game_versions.installed_game_version(g) == "1.6.1170"


def test_exes_inside_the_copy_are_started_as_the_game(env):
    """Proton's runinprefix skips the DXVK setup: Skyrim from a copy rendered
    through WineD3D (purple, slow). The game and its SKSE loader in the copy must
    get Proton's full run; tools elsewhere keep runinprefix."""
    g, steam, copy, profiles = env
    merge_profile_settings(profiles / "LoreRim", {"game_path": str(copy)})
    _use(g, profiles / "LoreRim")
    assert exe_launch._runs_from_game_copy(g, copy / "SkyrimSELauncher.exe")
    assert exe_launch._runs_from_game_copy(g, copy / "skse64_loader.exe")
    assert not exe_launch._runs_from_game_copy(g, steam / "SkyrimSE.exe")
    assert not exe_launch._runs_from_game_copy(g, copy.parent / "tools" / "xEdit.exe")
    _use(g, profiles / "default")
    assert not exe_launch._runs_from_game_copy(g, steam / "SkyrimSE.exe")

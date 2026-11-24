"""MosaicPathIndex: the file list Mosaic writes at deploy, installing and
removing the plugin, and the per-game setting.

Real case (LoreRim on Skyrim SE under Proton, 2026-11-24): Wine's
case-insensitive lookups cost ~150 s of a 364 s load; with the index the game
reached the main menu in 145 s (MO2: 205 s). The DLL itself is tested under
Wine by native/path_index/run_test.sh."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from Games.Bethesda.skyrim_se import SkyrimSE
from Utils import path_index as pi


def _parse(idx: Path) -> tuple[dict[str, int], set[str]]:
    lines = idx.read_text(encoding="utf-8").splitlines()
    assert lines[0] == pi.LIST_MAGIC.strip()
    dirs, files = {}, set()
    for ln in lines[1:]:
        kind, rest = ln[0], ln[2:]
        if kind == "D":
            ft, _, rel = rest.partition(" ")
            dirs[rel] = int(ft)
        else:
            files.add(rest)
    return dirs, files


def _game(tmp_path: Path) -> Path:
    g = tmp_path / "Stock Game"
    (g / "Data" / "Textures" / "Armor").mkdir(parents=True)
    (g / "Data" / "SKSE" / "Plugins").mkdir(parents=True)
    (g / "SkyrimSE.exe").write_bytes(b"MZ")
    (g / "Data" / "Skyrim.esm").write_bytes(b"x")
    (g / "Data" / "Textures" / "Armor" / "iron.dds").write_bytes(b"x")
    return g


def test_file_list_has_every_file_and_folder_with_wines_folder_times(tmp_path):
    g = _game(tmp_path)
    out = tmp_path / "list.idx"
    files, folders = pi.write_file_list(g, out)
    dirs, listed = _parse(out)
    assert listed == {"SkyrimSE.exe", "Data\\Skyrim.esm", "Data\\Textures\\Armor\\iron.dds"}
    assert set(dirs) == {".", "Data", "Data\\Textures", "Data\\Textures\\Armor", "Data\\SKSE",
                         "Data\\SKSE\\Plugins"}
    assert (files, folders) == (3, 6)
    # Wine's FILETIME for a folder: Unix mtime in 100 ns ticks since 1601
    st = os.stat(g / "Data" / "Textures")
    assert dirs["Data\\Textures"] == st.st_mtime_ns // 100 + 116444736000000000
    # parents come before their contents, which the DLL relies on
    order = [ln[2:].partition(" ")[2] for ln in out.read_text().splitlines()[1:]
             if ln.startswith("D ")]
    assert order.index("Data") < order.index("Data\\Textures") < order.index("Data\\Textures\\Armor")


def test_file_list_follows_folder_links_once_and_skips_impossible_names(tmp_path):
    g = _game(tmp_path)
    outside = tmp_path / "enbseries"
    outside.mkdir()
    (outside / "effect.fx").write_bytes(b"x")
    (g / "enbseries").symlink_to(outside)                    # a linked folder: followed
    (g / "Data" / "loop").symlink_to(g / "Data")             # a loop: listed once
    (g / "Data" / "back\\slash.txt").write_bytes(b"x")       # not a Windows name
    (g / "Data" / "linked.esp").symlink_to(g / "Data" / "Skyrim.esm")
    dirs, files = _parse(_write(g, tmp_path))
    assert "enbseries\\effect.fx" in files and "enbseries" in dirs
    assert "Data\\linked.esp" in files
    assert not any("back" in f for f in files)
    assert not any(d.startswith("Data\\loop\\") for d in dirs)


def _write(g: Path, tmp_path: Path) -> Path:
    out = tmp_path / "list.idx"
    pi.write_file_list(g, out)
    return out


def _dll(tmp_path, monkeypatch) -> bytes:
    dll = tmp_path / "built" / pi.DLL_NAME
    dll.parent.mkdir()
    dll.write_bytes(b"MZ fake plugin")
    monkeypatch.setattr(pi, "bundled_dll", lambda: dll)
    return dll.read_bytes()


def test_install_puts_the_dll_and_a_list_into_the_plugin_folder(tmp_path, monkeypatch):
    g = _game(tmp_path)
    data = _dll(tmp_path, monkeypatch)
    logs = []
    assert pi.install(g, "SKSE/Plugins", logs.append)
    plugins = g / "Data" / "SKSE" / "Plugins"
    assert (plugins / pi.DLL_NAME).read_bytes() == data
    assert not (plugins / pi.DLL_NAME).is_symlink()
    assert "Data\\Skyrim.esm" in _parse(plugins / pi.LIST_NAME)[1]
    assert any("installed" in m for m in logs)


def test_install_without_a_built_dll_does_nothing(tmp_path, monkeypatch):
    g = _game(tmp_path)
    monkeypatch.setattr(pi, "bundled_dll", lambda: None)
    logs = []
    assert not pi.install(g, "SKSE/Plugins", logs.append)
    assert not (g / "Data" / "SKSE" / "Plugins" / pi.DLL_NAME).exists()
    assert any("isn't built" in m for m in logs)


def test_remove_takes_the_dll_list_and_log_away(tmp_path, monkeypatch):
    g = _game(tmp_path)
    _dll(tmp_path, monkeypatch)
    pi.install(g, "SKSE/Plugins", lambda _m: None)
    plugins = g / "Data" / "SKSE" / "Plugins"
    (plugins / pi.LOG_NAME).write_text("calls 1")
    (plugins / "SomeoneElses.dll").write_bytes(b"x")
    assert pi.remove(g, "SKSE/Plugins", lambda _m: None) == 3
    assert sorted(p.name for p in plugins.iterdir()) == ["SomeoneElses.dll"]


@pytest.fixture
def skyrim(tmp_path, monkeypatch):
    game = SkyrimSE()
    g = _game(tmp_path)
    game._game_path = g
    paths = tmp_path / "cfg" / "paths.json"
    monkeypatch.setattr(SkyrimSE, "_paths_file", property(lambda _s: paths))
    _dll(tmp_path, monkeypatch)
    return game, g


def test_skyrim_has_it_on_by_default_and_saves_the_choice(skyrim, tmp_path):
    game, _g = skyrim
    assert game.path_index_plugin_dir == "SKSE/Plugins"
    assert game.path_index
    game.set_path_index(False)
    saved = json.loads((tmp_path / "cfg" / "paths.json").read_text())
    assert saved["path_index"] is False
    fresh = SkyrimSE()
    fresh._load_paths_extra(saved)
    assert not fresh.path_index
    fresh._load_paths_extra({})                       # older paths.json: on
    assert fresh.path_index


def test_switching_it_off_removes_the_plugin_at_once(skyrim):
    game, g = skyrim
    game._install_path_index(lambda _m: None)
    plugins = g / "Data" / "SKSE" / "Plugins"
    assert (plugins / pi.DLL_NAME).is_file()
    game.set_path_index(False)
    assert not (plugins / pi.DLL_NAME).exists() and not (plugins / pi.LIST_NAME).exists()
    game._install_path_index(lambda _m: None)         # off: a deploy installs nothing
    assert not (plugins / pi.DLL_NAME).exists()


def test_games_without_a_plugin_folder_never_get_it(skyrim, monkeypatch):
    game, g = skyrim
    monkeypatch.setattr(SkyrimSE, "path_index_plugin_dir", None)
    assert not game.path_index
    game._install_path_index(lambda _m: None)
    assert not (g / "Data" / "SKSE" / "Plugins" / pi.DLL_NAME).exists()


def test_restore_removes_it_before_data_is_cleared(skyrim, monkeypatch):
    """Restore moves unknown files in Data/ to overwrite; the plugin must be
    gone by then, not end up in the user's overwrite folder."""
    game, g = skyrim
    game._install_path_index(lambda _m: None)
    seen = {}

    def fake_restore_data_core(data_dir, **_kw):
        seen["dll_left"] = (Path(data_dir) / "SKSE" / "Plugins" / pi.DLL_NAME).exists()
        return 0

    import Games.Bethesda.skyrim_se as sse
    monkeypatch.setattr(sse, "restore_data_core", fake_restore_data_core)
    for name in ("_remove_plugins_txt_symlink", "revert_archive_invalidation",
                 "_restore_launcher", "capture_runtime_files_to_root_folder"):
        monkeypatch.setattr(game, name, lambda *_a, **_k: 0)
    monkeypatch.setattr(sse, "cleanup_custom_deploy_dirs", lambda *_a, **_k: None)
    monkeypatch.setattr(type(game), "custom_routing_rules", property(lambda _s: []))
    game.restore(log_fn=lambda _m: None)
    assert seen == {"dll_left": False}

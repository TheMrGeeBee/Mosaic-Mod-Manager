"""Per-exe settings and Proton prefixes for exes outside the game folder.

Real case (2026-11-24): with a LoreRim profile on its game copy, a custom exe
pointing at Steam's own SkyrimSELauncher.exe was given a Proton version.
Settings were keyed by file name only, so Play (the copy's
SkyrimSELauncher.exe) picked that Proton version up too; and the custom exe's
isolated prefix was created inside Steam's Skyrim install."""
from __future__ import annotations

from pathlib import Path

from Utils.exe_launch import exe_launch


class _Game:
    exe_name = "SkyrimSE.exe"
    preferred_launch_exe = "skse64_loader.exe"

    def __init__(self, root: Path):
        self._root = root

    def get_game_path(self):
        return self._root


def _copy_and_steam(tmp_path):
    copy = tmp_path / "Modlists" / "LoreRim" / "Stock Game"
    steam = tmp_path / "steamapps" / "common" / "Skyrim Special Edition"
    for d in (copy, steam):
        d.mkdir(parents=True)
        for n in ("SkyrimSE.exe", "SkyrimSELauncher.exe", "skse64_loader.exe"):
            (d / n).write_bytes(b"MZ")
    return copy, steam


def test_game_folder_exes_keep_their_name_key(tmp_path):
    copy, _ = _copy_and_steam(tmp_path)
    game = _Game(copy)
    assert exe_launch.exe_settings_key(game, copy / "SkyrimSELauncher.exe") == "SkyrimSELauncher.exe"


def test_same_named_exe_outside_the_game_folder_gets_its_own_key(tmp_path):
    copy, steam = _copy_and_steam(tmp_path)
    game = _Game(copy)
    other = steam / "SkyrimSELauncher.exe"
    key = exe_launch.exe_settings_key(game, other)
    assert key != "SkyrimSELauncher.exe"
    assert key == f"SkyrimSELauncher.exe@{other}"
    # the match ignores case, as Windows names do
    lower = tmp_path / "elsewhere" / "skyrimselauncher.EXE"
    lower.parent.mkdir()
    lower.write_bytes(b"MZ")
    assert "@" in exe_launch.exe_settings_key(game, lower)


def test_unrelated_tools_keep_their_name_key(tmp_path):
    copy, _ = _copy_and_steam(tmp_path)
    tool = tmp_path / "Applications" / "SSEEdit" / "SSEEdit.exe"
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b"MZ")
    assert exe_launch.exe_settings_key(_Game(copy), tool) == "SSEEdit.exe"


def test_custom_exe_override_no_longer_reaches_play(tmp_path, monkeypatch):
    copy, steam = _copy_and_steam(tmp_path)
    game = _Game(copy)
    store: dict = {}
    monkeypatch.setattr(exe_launch, "_read_launch_mode_data", lambda g: dict(store))

    def write(g, key, value):
        if value is None:
            store.pop(key, None)
        else:
            store[key] = value
    monkeypatch.setattr(exe_launch, "_write_launch_mode_key", write)

    custom = steam / "SkyrimSELauncher.exe"
    exe_launch.save_proton_override(game, exe_launch.exe_settings_key(game, custom), "proton-cachyos-slr")
    play_key = exe_launch.exe_settings_key(game, copy / "SkyrimSELauncher.exe")
    assert exe_launch.load_proton_override(game, play_key) is None
    assert exe_launch.load_proton_override(
        game, exe_launch.exe_settings_key(game, custom)) == "proton-cachyos-slr"


def test_prefix_stays_next_to_exes_outside_steam(tmp_path):
    tool = tmp_path / "Applications" / "SSEEdit" / "SSEEdit.exe"
    assert exe_launch.isolated_prefix_dir(tool, "GE-Proton10") == tool.parent / "prefix_GE-Proton10"


def test_prefix_never_goes_into_a_steam_install(tmp_path, monkeypatch):
    prefixes = tmp_path / "wine_prefixes"
    monkeypatch.setattr("Utils.config_paths.get_wine_prefixes_dir", lambda: prefixes)
    _, steam = _copy_and_steam(tmp_path)
    exe = steam / "SkyrimSELauncher.exe"
    d = exe_launch.isolated_prefix_dir(exe, "proton-cachyos-slr")
    assert d.parent == prefixes
    assert not d.is_relative_to(steam)
    assert d.name.startswith("SkyrimSELauncher-") and d.name.endswith("_proton-cachyos-slr")
    # stable for the same exe, different for another install's exe
    assert exe_launch.isolated_prefix_dir(exe, "proton-cachyos-slr") == d
    other = tmp_path / "lib2" / "steamapps" / "common" / "Skyrim Special Edition" / "SkyrimSELauncher.exe"
    assert exe_launch.isolated_prefix_dir(other, "proton-cachyos-slr") != d

"""Steam's per-game launch options, for a game copy Mosaic starts itself.

Real case (2026-11-24): LoreRim on its game copy was started directly with
Proton -- Steam can only start its own install -- and silently ran with a
hidden per-exe value from Mosaic's settings instead of the options set in
Steam (Wayland, HDR, a MangoHud config, WINE_DISABLE_SFN)."""
from __future__ import annotations

import os

from Utils.exe_launch import exe_launch
from Utils.steam.launch_options import steam_launch_options

SKYRIM = "489830"


def _localconfig(root, user, apps: dict, mtime: float):
    cfg = root / "userdata" / user / "config" / "localconfig.vdf"
    cfg.parent.mkdir(parents=True)
    body = "".join(
        f'\t\t\t\t\t"{app}"\n\t\t\t\t\t{{\n'
        + (f'\t\t\t\t\t\t"LaunchOptions"\t\t"{opts}"\n' if opts is not None else "")
        + '\t\t\t\t\t\t"LastPlayed"\t\t"1"\n\t\t\t\t\t}\n'
        for app, opts in apps.items())
    cfg.write_text('"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
                   '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"apps"\n\t\t\t\t{\n' + body
                   + '\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n', encoding="utf-8")
    os.utime(cfg, (mtime, mtime))


def test_reads_the_options_set_in_steam(tmp_path):
    _localconfig(tmp_path, "1", {"377160": "OTHER=1 %command%",
                                 SKYRIM: 'PROTON_ENABLE_HDR=1 mangohud %command% -skip \\"intro\\"'},
                 1000)
    assert steam_launch_options(SKYRIM, [tmp_path]) == 'PROTON_ENABLE_HDR=1 mangohud %command% -skip "intro"'


def test_game_without_options_and_unknown_game(tmp_path):
    _localconfig(tmp_path, "1", {SKYRIM: None}, 1000)
    assert steam_launch_options(SKYRIM, [tmp_path]) == ""       # known, no options
    assert steam_launch_options("12345", [tmp_path]) is None    # not in this Steam
    assert steam_launch_options(SKYRIM, [tmp_path / "nowhere"]) is None


def test_the_account_that_used_steam_last_wins(tmp_path):
    _localconfig(tmp_path, "1", {SKYRIM: "OLD=1 %command%"}, 1000)
    _localconfig(tmp_path, "2", {SKYRIM: "NEW=1 %command%"}, 2000)
    assert steam_launch_options(SKYRIM, [tmp_path]) == "NEW=1 %command%"


def test_a_broken_config_is_skipped(tmp_path):
    _localconfig(tmp_path, "1", {SKYRIM: "GOOD=1 %command%"}, 1000)
    bad = tmp_path / "userdata" / "2" / "config" / "localconfig.vdf"
    bad.parent.mkdir(parents=True)
    bad.write_text('"UserLocalConfigStore" { "unterminated', encoding="utf-8")
    os.utime(bad, (2000, 2000))
    assert steam_launch_options(SKYRIM, [tmp_path]) == "GOOD=1 %command%"


def test_copy_launch_uses_steams_options_over_mosaics(monkeypatch):
    import Utils.steam.launch_options as lo
    logs = []
    monkeypatch.setattr(lo, "steam_launch_options", lambda _a: "WINE_DISABLE_SFN=1 %command%")
    assert exe_launch._steam_options_for_copy(SKYRIM, "HIDDEN=1 %command%", logs.append) \
        == "WINE_DISABLE_SFN=1 %command%"
    assert any("from Steam" in m for m in logs)
    monkeypatch.setattr(lo, "steam_launch_options", lambda _a: "")
    assert exe_launch._steam_options_for_copy(SKYRIM, "HIDDEN=1 %command%", logs.append) == ""


def test_copy_launch_falls_back_to_mosaics_when_steam_has_nothing(monkeypatch):
    import Utils.steam.launch_options as lo
    monkeypatch.setattr(lo, "steam_launch_options", lambda _a: None)
    logs = []
    assert exe_launch._steam_options_for_copy(SKYRIM, "MINE=1 %command%", logs.append) \
        == "MINE=1 %command%"

    def boom(_a):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(lo, "steam_launch_options", boom)
    assert exe_launch._steam_options_for_copy(SKYRIM, "MINE=1 %command%", logs.append) \
        == "MINE=1 %command%"

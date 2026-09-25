"""Closing a wizard tool (BethINI, xEdit, ...) makes Mosaic run ``wineserver -k`` on
the prefix. That takes every process in the prefix with it - including a game the
user started meanwhile. Real case (2026-09-26): Play was pressed while BethINI was
open; closing BethINI 19 s later killed the still-starting game ("failed to
launch"). The shutdown now leaves the prefix alone while the game runs in it."""
from __future__ import annotations

from pathlib import Path

from Utils.exe_launch import exe_launch as el


def _proc(root: Path, pid: int, argv: list[str], env: dict[str, str] | None = None):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (env or {}).items()))


COMPAT = Path("/games/steamapps/compatdata/489830")


def test_steam_launched_game_is_detected(tmp_path):
    _proc(tmp_path, 100, ["/steam/reaper", "SteamLaunch", "AppId=489830", "--", "proton", "waitforexitandrun", "x"])
    assert el.game_running_in_prefix(COMPAT, tmp_path)


def test_a_game_started_by_proton_directly_is_detected_by_its_compat_env(tmp_path):
    _proc(tmp_path, 100, ["proton", "waitforexitandrun", "/g/SkyrimSE.exe"],
          {"STEAM_COMPAT_DATA_PATH": str(COMPAT)})
    assert el.game_running_in_prefix(COMPAT, tmp_path)


def test_a_wizard_tool_alone_is_not_the_game(tmp_path):
    _proc(tmp_path, 100, ["proton", "runinprefix", "/tools/Bethini.exe"],
          {"STEAM_COMPAT_DATA_PATH": str(COMPAT)})
    assert not el.game_running_in_prefix(COMPAT, tmp_path)


def test_another_games_prefix_is_unaffected(tmp_path):
    _proc(tmp_path, 100, ["/steam/reaper", "SteamLaunch", "AppId=377160", "--", "x"])
    _proc(tmp_path, 101, ["proton", "waitforexitandrun", "y"], {"STEAM_COMPAT_DATA_PATH": "/other/compatdata/377160"})
    assert not el.game_running_in_prefix(COMPAT, tmp_path)


def test_unreadable_proc_is_treated_as_not_running(tmp_path):
    assert not el.game_running_in_prefix(COMPAT, tmp_path / "nowhere")
    _proc(tmp_path, 100, ["x"])
    (tmp_path / "100" / "cmdline").unlink()
    assert not el.game_running_in_prefix(COMPAT, tmp_path)


def test_shutdown_skips_wineserver_kill_while_the_game_runs(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(el, "game_running_in_prefix", lambda *_a, **_k: True)
    monkeypatch.setattr(el.subprocess, "run", lambda *a, **k: ran.append(a))
    logs = []
    proton = tmp_path / "Proton" / "proton"
    (proton.parent / "files" / "bin").mkdir(parents=True)
    (proton.parent / "files" / "bin" / "wineserver").write_text("x")
    el.shutdown_prefix_wineserver(proton, tmp_path / "compat", logs.append)
    assert not ran and any("leaving its wineserver alone" in m for m in logs)


def test_shutdown_still_runs_when_no_game_is_running(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(el, "game_running_in_prefix", lambda *_a, **_k: False)
    monkeypatch.setattr(el.subprocess, "run", lambda *a, **k: ran.append(a))
    proton = tmp_path / "Proton" / "proton"
    (proton.parent / "files" / "bin").mkdir(parents=True)
    (proton.parent / "files" / "bin" / "wineserver").write_text("x")
    (tmp_path / "compat" / "pfx").mkdir(parents=True)
    el.shutdown_prefix_wineserver(proton, tmp_path / "compat")
    assert len(ran) == 1 and "-k" in ran[0][0]

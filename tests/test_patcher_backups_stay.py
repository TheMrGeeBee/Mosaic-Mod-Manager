"""A patcher's backup of the original exe stays in the game folder on restore.

Real case, 2026-11-25: the 4GB patch ran while Nuclear Sunset was deployed;
the TTW wizard's restore-to-vanilla then read FalloutNV_backup.exe as a runtime
file and moved it into the profile's Root_Folder. Fallout 3's Anniversary
Patcher backup is also how Mosaic knows the game loads FOSE itself."""
from __future__ import annotations

import pytest

from Games.Bethesda.fallout_3_goty import Fallout3_GOTY
from Games.Bethesda.fallout_nv import Fallout_NV
from Utils.deploy.deploy_shared import _move_runtime_files, _write_deploy_snapshot
from Utils.modding_tools import fnv4gb_tools


def _sweep(tmp_path, game, backup: str):
    root = tmp_path / "game"
    root.mkdir()
    for i in range(10):                          # vanilla files in the snapshot
        (root / f"vanilla{i}.dll").write_bytes(b"v")
    snap = tmp_path / "deploy_snapshot.txt"
    _write_deploy_snapshot(root, snap)
    (root / backup).write_bytes(b"original exe")    # made while deployed
    (root / "crash.log").write_bytes(b"runtime")
    dest = tmp_path / "Root_Folder"
    moved = _move_runtime_files(root, snap, dest,
                                restore_whitelist=game.restore_whitelist_matcher())
    return root, dest, moved


@pytest.mark.parametrize("game, backup", [
    (Fallout_NV(), "FalloutNV_backup.exe"),
    (Fallout3_GOTY(), "Fallout3_backup.exe"),
])
def test_the_backup_stays_in_the_game_folder(tmp_path, game, backup):
    root, dest, moved = _sweep(tmp_path, game, backup)
    assert (root / backup).read_bytes() == b"original exe"
    assert not (dest / backup).exists()
    assert (dest / "crash.log").is_file() and moved == 1    # other runtime files still go


def test_a_4gb_patched_exe_is_known_without_its_backup(tmp_path, monkeypatch):
    root = tmp_path / "Fallout New Vegas"
    root.mkdir()
    (root / "FalloutNVLauncher.exe").write_bytes(b"launcher")
    (root / "nvse_loader.exe").write_bytes(b"nvse")
    (root / "FalloutNV.exe").write_bytes(b"patched")
    monkeypatch.setattr(fnv4gb_tools, "_PATCHED_HASHES",
                        {__import__("hashlib").sha1(b"patched").hexdigest().upper()})
    g = Fallout_NV()
    g._game_path = root
    g._script_extender_swap = True
    logs = []
    g.swap_launcher(logs.append)
    assert (root / "FalloutNVLauncher.exe").read_bytes() == b"launcher"
    assert any("4GB patch" in m for m in logs)

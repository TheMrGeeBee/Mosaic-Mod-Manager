"""Root-flagged mods whose staging folders differ in case from the filemap's."""
from __future__ import annotations

from Utils.deploy.deploy_root import deploy_root_flagged_mods
from Utils.deploy.deploy_shared import LinkMode


def test_root_flagged_file_found_when_staging_case_differs(tmp_path):
    """Real case (LoreRim 2026-10-19): the mod holds Data/skse/plugins/ but the
    filemap names Data/SKSE/Plugins/, so EngineFixes.dll was never deployed."""
    staging = tmp_path / "mods"
    src = staging / "Engine Fixes" / "Data" / "skse" / "plugins" / "EngineFixes.dll"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"dll")
    game = tmp_path / "game"
    game.mkdir()
    filemap = tmp_path / "filemap_root.txt"
    filemap.write_text("Data/SKSE/Plugins/EngineFixes.dll\tEngine Fixes\n", encoding="utf-8")
    logs = []

    n = deploy_root_flagged_mods(filemap, game, staging, mode=LinkMode.COPY, log_fn=logs.append)

    assert n == 1
    assert (game / "Data" / "SKSE" / "Plugins" / "EngineFixes.dll").read_bytes() == b"dll"
    assert not any("source not found" in line for line in logs)


def test_root_file_replaces_the_game_file_it_differs_from_only_by_case(tmp_path):
    """Real case (VtMB, Fashionista): Steam installs the executable as
    ``vampire.exe``; the Unofficial Patch ships ``Vampire.exe``. The patched,
    4 GB-aware executable must land *over* the stock one -- deployed beside it
    on a case-sensitive filesystem, the game keeps launching the stock file
    and crashes under a large modlist."""
    staging = tmp_path / "mods"
    src = staging / "Unofficial Patch Plus - Root" / "Vampire.exe"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"patched 4GB-aware exe")
    game = tmp_path / "game"
    game.mkdir()
    (game / "vampire.exe").write_bytes(b"steam stub")
    filemap = tmp_path / "filemap_root.txt"
    filemap.write_text("Vampire.exe\tUnofficial Patch Plus - Root\n", encoding="utf-8")

    n = deploy_root_flagged_mods(filemap, game, staging, mode=LinkMode.COPY,
                                 log_fn=lambda _m: None)

    assert n == 1
    names = sorted(p.name for p in game.iterdir())
    assert names == ["vampire.exe"], f"stock exe not replaced: {names}"
    assert (game / "vampire.exe").read_bytes() == b"patched 4GB-aware exe"
    # The stock file is recoverable, under the name it had on disk.
    backup = tmp_path / "Root_Backup" / "vampire.exe"
    assert backup.read_bytes() == b"steam stub"


def test_root_file_the_game_does_not_have_keeps_the_mods_spelling(tmp_path):
    staging = tmp_path / "mods"
    src = staging / "M - Root" / "ReShade.ini"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"cfg")
    game = tmp_path / "game"
    game.mkdir()
    filemap = tmp_path / "filemap_root.txt"
    filemap.write_text("ReShade.ini\tM - Root\n", encoding="utf-8")

    assert deploy_root_flagged_mods(filemap, game, staging, mode=LinkMode.COPY,
                                    log_fn=lambda _m: None) == 1
    assert (game / "ReShade.ini").read_bytes() == b"cfg"


def test_restore_puts_back_the_game_file_under_its_own_name(tmp_path):
    from Utils.deploy.deploy_root import restore_root_folder
    staging = tmp_path / "mods"
    src = staging / "UP - Root" / "Vampire.exe"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"patched")
    game = tmp_path / "game"
    game.mkdir()
    (game / "vampire.exe").write_bytes(b"steam stub")
    filemap = tmp_path / "filemap_root.txt"
    filemap.write_text("Vampire.exe\tUP - Root\n", encoding="utf-8")
    deploy_root_flagged_mods(filemap, game, staging, mode=LinkMode.COPY,
                             log_fn=lambda _m: None)

    restore_root_folder(tmp_path / "Root_Folder", game, log_fn=lambda _m: None)

    names = sorted(p.name for p in game.iterdir())
    assert names == ["vampire.exe"], names
    assert (game / "vampire.exe").read_bytes() == b"steam stub"

"""A runtime-created file whose path also exists in the staging pool.

Real case (VtMB, Fashionista 2026-10-31): the game writes save/autosave.sav,
and three *disabled* mods in the 348-mod shared pool happen to ship a file at
that same relative path. The restore walk matched it against the mod index and
dropped it, so switching profiles destroyed the player's savegame.
"""
from __future__ import annotations

from pathlib import Path

from Utils.deploy.deploy_standard import restore_data_core
from Utils.filemap import update_mod_index


def _setup(tmp_path: Path):
    """A deployed profile folder holding one runtime-written save, plus a mod
    in staging that ships the same relative path."""
    staging = tmp_path / "mods"
    (staging / "Some Overhaul" / "save").mkdir(parents=True)
    (staging / "Some Overhaul" / "save" / "autosave.sav").write_bytes(b"the mod's own file")

    deploy = tmp_path / "Mosaic_Profile"
    (deploy / "save").mkdir(parents=True)
    (deploy / "save" / "autosave.sav").write_bytes(b"THE PLAYER'S SAVEGAME")

    core = tmp_path / "Mosaic_Profile_Core"
    core.mkdir()
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir()
    # The index covers every mod in the pool, enabled or not — which is what
    # makes the runtime save collide with a mod's own file.
    update_mod_index(tmp_path / "modindex.bin", "Some Overhaul",
                     {"save/autosave.sav": "save/autosave.sav"}, {})
    return staging, deploy, core, overwrite


def test_runtime_save_is_rescued_even_when_a_mod_ships_the_same_path(tmp_path):
    staging, deploy, core, overwrite = _setup(tmp_path)

    restore_data_core(deploy, core_dir=core, overwrite_dir=overwrite,
                      staging_root=staging,
                      strip_prefixes={"vampire", "unofficial_patch"},
                      runtime_dirs={"save"}, log_fn=lambda _m: None)

    rescued = overwrite / "save" / "autosave.sav"
    assert rescued.is_file(), "the player's savegame was destroyed by restore"
    assert rescued.read_bytes() == b"THE PLAYER'S SAVEGAME"
    # and the shared pool must not have been written through
    assert (staging / "Some Overhaul" / "save" / "autosave.sav").read_bytes() \
        == b"the mod's own file"


def test_runtime_save_is_rescued_without_staging_root_too(tmp_path):
    """The VtMB handler passed no staging_root, which took the 'skip as before'
    branch and deleted the file."""
    _staging, deploy, core, overwrite = _setup(tmp_path)

    restore_data_core(deploy, core_dir=core, overwrite_dir=overwrite,
                      runtime_dirs={"save"}, log_fn=lambda _m: None)

    assert (overwrite / "save" / "autosave.sav").read_bytes() == b"THE PLAYER'S SAVEGAME"


def test_without_runtime_dirs_behaviour_is_unchanged_for_other_games(tmp_path):
    """Only folders a game declares as runtime state are exempted; everything
    else keeps the existing mod-file handling."""
    staging, deploy, core, overwrite = _setup(tmp_path)

    restore_data_core(deploy, core_dir=core, overwrite_dir=overwrite,
                      staging_root=staging, strip_prefixes=set(),
                      log_fn=lambda _m: None)

    assert not (overwrite / "save" / "autosave.sav").exists()


def test_with_staging_an_edited_mod_file_goes_back_to_its_mod_not_the_bin(tmp_path):
    """What passing staging_root buys a game that has no runtime_dirs to
    declare (BepInEx): a file matching the mod index is returned to the mod
    that owns it instead of being dropped with the folder. Without
    staging_root the walk takes 'no staging check — skip as before', which
    deletes it."""
    staging, deploy, core, overwrite = _setup(tmp_path)
    owned = staging / "Some Overhaul" / "save" / "autosave.sav"

    restore_data_core(deploy, core_dir=core, overwrite_dir=overwrite,
                      staging_root=staging, strip_prefixes={"save"},
                      log_fn=lambda _m: None)

    assert owned.read_bytes() == b"THE PLAYER'S SAVEGAME", \
        "the edited file was neither returned to its mod nor rescued"


def test_runtime_dirs_wins_over_the_staging_writeback(tmp_path):
    """A game that declares the folder as runtime state keeps the file for the
    player instead of writing it into a mod — which for a shared mod pool
    would reach every profile using it."""
    staging, deploy, core, overwrite = _setup(tmp_path)

    restore_data_core(deploy, core_dir=core, overwrite_dir=overwrite,
                      staging_root=staging, strip_prefixes={"save"},
                      runtime_dirs={"save"}, log_fn=lambda _m: None)

    assert (overwrite / "save" / "autosave.sav").read_bytes() == b"THE PLAYER'S SAVEGAME"
    assert (staging / "Some Overhaul" / "save" / "autosave.sav").read_bytes() \
        == b"the mod's own file"

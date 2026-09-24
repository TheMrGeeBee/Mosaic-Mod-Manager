"""Removing a mod re-links another enabled mod's copy of the same file.

Replays the Waterproof Shadowheart case: the old copy is deployed (symlink in
the game's Mods folder), a fresh download of the same mod is installed as a
second folder but not deployed yet, and the old copy is removed.  The game
must keep the file instead of losing it until the next Deploy.
"""
from __future__ import annotations

import os

from Utils.filemap import update_mod_index
from Utils.mods.modlist import ModEntry, write_modlist
from Utils.mods.mod_remove import remove_mods


class _Game:
    def __init__(self, staging, deploy_dir):
        self._staging = staging
        self._deploy = deploy_dir

    def get_effective_mod_staging_path(self):
        return self._staging

    def get_deploy_active(self):
        return True

    def get_mod_data_path(self):
        return self._deploy

    def get_game_path(self):
        return None

    plugin_extensions: list = []


def _setup(tmp_path, *, deployed_link: bool = True):
    staging = tmp_path / "mods"
    deploy = tmp_path / "GameMods"
    deploy.mkdir()
    for mod in ("Waterproof Shadowheart", "WaterproofShadowheart"):
        (staging / mod).mkdir(parents=True)
        (staging / mod / "WaterproofShadowheart.pak").write_bytes(mod.encode())
        update_mod_index(tmp_path / "modindex.bin", mod,
                         {"waterproofshadowheart.pak": "WaterproofShadowheart.pak"}, {})
    if deployed_link:
        os.symlink(staging / "Waterproof Shadowheart" / "WaterproofShadowheart.pak",
                   deploy / "WaterproofShadowheart.pak")
    profile = tmp_path / "profile"
    profile.mkdir()
    write_modlist(profile / "modlist.txt", [
        ModEntry(name="WaterproofShadowheart", enabled=True, locked=False),
        ModEntry(name="Waterproof Shadowheart", enabled=True, locked=False),
    ])
    return staging, deploy, profile


def test_removed_file_is_relinked_from_the_other_copy(tmp_path):
    staging, deploy, profile = _setup(tmp_path)
    logs: list[str] = []
    remove_mods(_Game(staging, deploy), profile, ["Waterproof Shadowheart"],
                log_fn=logs.append)
    link = deploy / "WaterproofShadowheart.pak"
    assert link.is_symlink()
    assert os.readlink(link) == str(staging / "WaterproofShadowheart" /
                                    "WaterproofShadowheart.pak")
    assert link.read_bytes() == b"WaterproofShadowheart"
    assert any("Re-linked 1 file" in m for m in logs)


def test_disabled_copy_is_not_linked(tmp_path):
    staging, deploy, profile = _setup(tmp_path)
    write_modlist(profile / "modlist.txt", [
        ModEntry(name="WaterproofShadowheart", enabled=False, locked=False),
        ModEntry(name="Waterproof Shadowheart", enabled=True, locked=False),
    ])
    remove_mods(_Game(staging, deploy), profile, ["Waterproof Shadowheart"])
    assert not os.path.lexists(deploy / "WaterproofShadowheart.pak")


def test_nothing_linked_when_nothing_was_removed(tmp_path):
    staging, deploy, profile = _setup(tmp_path, deployed_link=False)
    remove_mods(_Game(staging, deploy), profile, ["Waterproof Shadowheart"])
    assert not os.path.lexists(deploy / "WaterproofShadowheart.pak")

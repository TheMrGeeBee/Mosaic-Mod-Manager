"""Restore then deploy must not replace a profile's own INIs.

Real case (2026-11-25): a LoreRim profile ran on LoreRim's own Skyrim.ini and
SkyrimPrefs.ini. A Restore put My Games' originals back from their .bak
copies, and the next Deploy took those originals for settings the game had
written and copied them over the profile's INIs -- LoreRim's settings were
gone and the game ran on the user's old ones."""
from __future__ import annotations

from pathlib import Path

from Games.Bethesda.bethesda_ini import RESTORED_INIS_NOTE
from Games.Bethesda.fallout_3 import Fallout_3


class _Game:
    """Just the profile-INI parts of a Bethesda game."""
    _symlink_profile_ini_files = Fallout_3._symlink_profile_ini_files
    _remove_profile_ini_symlinks = Fallout_3._remove_profile_ini_symlinks
    _drop_foreign_profile_ini_links = Fallout_3._drop_foreign_profile_ini_links
    _profile_ini_files = True

    def __init__(self, root: Path):
        self.mygames = root / "My Games" / "Skyrim Special Edition"
        self.mygames.mkdir(parents=True)
        self.profiles = root / "profiles"

    def _mygames_paths(self):
        return [self.mygames]

    def _profile_ini_dir(self, profile):
        return self.profiles / profile / "ini files"

    def own(self, profile, text):
        d = self._profile_ini_dir(profile)
        d.mkdir(parents=True, exist_ok=True)
        (d / "Skyrim.ini").write_text(text)

    def deploy(self, profile):
        self._symlink_profile_ini_files(profile, lambda m: None)

    def restore(self, profile):
        self._remove_profile_ini_symlinks(profile, lambda m: None)

    def live(self):
        return (self.mygames / "Skyrim.ini").read_text()


def _linked_once(tmp_path):
    g = _Game(tmp_path)
    g.own("LoreRim", "lorerim\n")
    # the profile's INI is already linked, the original kept as .bak
    (g.mygames / "Skyrim.bak").write_text("original\n")
    (g.mygames / "Skyrim.ini").symlink_to(g._profile_ini_dir("LoreRim") / "Skyrim.ini")
    return g


def test_restore_then_deploy_keeps_the_profiles_own_ini(tmp_path):
    g = _linked_once(tmp_path)
    g.restore("LoreRim")
    assert g.live() == "original\n"                       # restore did its job
    g.deploy("LoreRim")
    assert (g._profile_ini_dir("LoreRim") / "Skyrim.ini").read_text() == "lorerim\n"
    assert (g.mygames / "Skyrim.ini").is_symlink() and g.live() == "lorerim\n"
    assert (g.mygames / "Skyrim.bak").read_text() == "original\n"
    assert not (g.mygames / RESTORED_INIS_NOTE).exists()


def test_it_holds_over_several_restore_deploy_rounds(tmp_path):
    g = _linked_once(tmp_path)
    for _ in range(3):
        g.restore("LoreRim")
        g.deploy("LoreRim")
    assert g.live() == "lorerim\n"
    assert (g.mygames / "Skyrim.bak").read_text() == "original\n"


def test_another_profile_deployed_after_a_restore_keeps_its_own_too(tmp_path):
    g = _linked_once(tmp_path)
    g.own("Vanilla+", "vanilla plus\n")
    g.restore("LoreRim")
    g.deploy("Vanilla+")
    assert (g._profile_ini_dir("Vanilla+") / "Skyrim.ini").read_text() == "vanilla plus\n"
    assert g.live() == "vanilla plus\n"


def test_an_ini_the_game_changed_after_the_restore_is_still_carried_over(tmp_path):
    # Played without Mosaic in between: the game wrote real settings.
    g = _linked_once(tmp_path)
    g.restore("LoreRim")
    (g.mygames / "Skyrim.ini").write_text("original\nbFreebiesSeen=1\n")
    g.deploy("LoreRim")
    assert (g._profile_ini_dir("LoreRim") / "Skyrim.ini").read_text() == (
        "original\nbFreebiesSeen=1\n")


def test_without_a_restore_a_live_ini_still_wins_as_before(tmp_path):
    g = _Game(tmp_path)
    g.own("Mine", "profile\n")
    (g.mygames / "Skyrim.ini").write_text("changed in game\n")
    g.deploy("Mine")
    assert (g._profile_ini_dir("Mine") / "Skyrim.ini").read_text() == "changed in game\n"

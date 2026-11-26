"""A Wabbajack modlist's own game INIs become the profile's INIs.

Real case (2026-11-24): LoreRim's Skyrim.ini, SkyrimPrefs.ini and
SkyrimCustom.ini were only kept aside in wabbajack_profile_files, so the game
ran on whatever INIs were live in the prefix. Copying them in by hand
brought MO2's sLocalSavePath=__MO_Saves\\ along, and the saves "vanished"."""
from __future__ import annotations

from pathlib import Path

from Games.Bethesda.bethesda_ini import (
    INSTALLED_INIS_MARKER,
    mark_installed_inis,
    read_installed_inis,
)
from Games.Bethesda.fallout_3 import Fallout_3
from Utils.profile.profile_state import read_profile_settings
from Utils.wabbajack import wabbajack_install as wi

SKYRIM_INIS = ["Skyrim.ini", "SkyrimPrefs.ini", "SkyrimCustom.ini"]


def _mo2_profile(tmp_path) -> Path:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "modlist.txt").write_text("+ModA\n")
    (scratch / "plugins.txt").write_text("*ModA.esp\n")
    # MO2 writes them lower-cased, with CRLF line ends
    (scratch / "skyrim.ini").write_bytes(
        b"[General]\r\nsLocalSavePath=__MO_Saves\\\r\nsLanguage=ENGLISH\r\n")
    (scratch / "skyrimprefs.ini").write_bytes(b"[Display]\r\niSize W=3440\r\n")
    (scratch / "skyrimcustom.ini").write_bytes(
        b"[Archive]\r\nbInvalidateOlderFiles=1\r\n[General]\r\nsLocalSavePath = __MO_Saves\\\r\n")
    (scratch / "settings.ini").write_text("[General]\nLocalSaves=true\n")
    return scratch


def _write(tmp_path, **kw):
    profile = tmp_path / "profiles" / "LoreRim"
    profile.mkdir(parents=True)
    report = wi.WabbajackInstallReport()
    logged: list[str] = []
    wi._write_profile(profile, _mo2_profile(tmp_path), {"ModA"}, report, logged.append, **kw)
    return profile, report, logged


def test_game_inis_go_into_the_profiles_ini_folder_with_the_games_names(tmp_path):
    profile, report, _ = _write(tmp_path, ini_targets=SKYRIM_INIS, ini_subdir="ini files")
    ini_dir = profile / "ini files"
    assert sorted(p.name for p in ini_dir.glob("*.ini")) == sorted(SKYRIM_INIS)
    assert sorted(report.applied_profile_inis) == sorted(SKYRIM_INIS)
    assert (ini_dir / "SkyrimPrefs.ini").read_bytes() == b"[Display]\r\niSize W=3440\r\n"
    # only MO2's own settings file is left aside
    assert report.held_profile_files == ["settings.ini"]
    assert read_profile_settings(profile).get("profile_ini_files") is True


def test_mo2_save_folder_is_set_back_to_the_games(tmp_path):
    profile, _, logged = _write(tmp_path, ini_targets=SKYRIM_INIS, ini_subdir="ini files")
    ini_dir = profile / "ini files"
    assert (ini_dir / "Skyrim.ini").read_bytes() == (
        b"[General]\r\nsLocalSavePath=Saves\\\r\nsLanguage=ENGLISH\r\n")
    assert (ini_dir / "SkyrimCustom.ini").read_bytes() == (
        b"[Archive]\r\nbInvalidateOlderFiles=1\r\n[General]\r\nsLocalSavePath = Saves\\\r\n")
    assert sum("__MO_Saves" in m for m in logged) == 2


def test_installed_inis_are_marked_for_their_first_link(tmp_path):
    profile, _, _ = _write(tmp_path, ini_targets=SKYRIM_INIS, ini_subdir="ini files")
    assert read_installed_inis(profile / "ini files") == {n.lower() for n in SKYRIM_INIS}


def test_games_without_profile_inis_keep_them_aside(tmp_path):
    profile, report, _ = _write(tmp_path)
    assert not (profile / "ini files").exists()
    assert report.applied_profile_inis == []
    assert sorted(report.held_profile_files) == [
        "settings.ini", "skyrim.ini", "skyrimcustom.ini", "skyrimprefs.ini"]
    assert "profile_ini_files" not in read_profile_settings(profile)


def test_profile_ini_targets_for_skyrim_and_other_games():
    class Skyrim:
        name = "Skyrim Special Edition"
        _PROFILE_INI_SUBDIR = "ini files"

        def set_profile_ini_files(self, v):
            pass

    class Other:
        name = "Cyberpunk 2077"

    assert wi._profile_ini_targets(Skyrim()) == (SKYRIM_INIS, "ini files")
    assert wi._profile_ini_targets(Other()) == (None, None)


# --- the deploy step that links them into My Games --------------------------

class _Linker:
    _symlink_profile_ini_files = Fallout_3._symlink_profile_ini_files
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


def test_first_link_keeps_the_installed_ini_over_the_live_one(tmp_path):
    g = _Linker(tmp_path)
    ini_dir = g._profile_ini_dir("LoreRim")
    ini_dir.mkdir(parents=True)
    (ini_dir / "Skyrim.ini").write_text("modlist\n")
    mark_installed_inis(ini_dir, ["Skyrim.ini"])
    (g.mygames / "Skyrim.ini").write_text("live\n")

    g._symlink_profile_ini_files("LoreRim", lambda m: None)

    assert (ini_dir / "Skyrim.ini").read_text() == "modlist\n"
    assert (g.mygames / "Skyrim.ini").is_symlink()
    assert (g.mygames / "Skyrim.ini").read_text() == "modlist\n"
    assert (g.mygames / "Skyrim.bak").read_text() == "live\n"
    assert not (ini_dir / INSTALLED_INIS_MARKER).exists(), "only the first link"


def test_without_the_marker_the_live_ini_still_wins(tmp_path):
    g = _Linker(tmp_path)
    ini_dir = g._profile_ini_dir("Mine")
    ini_dir.mkdir(parents=True)
    (ini_dir / "Skyrim.ini").write_text("profile\n")
    (g.mygames / "Skyrim.ini").write_text("changed in game\n")

    g._symlink_profile_ini_files("Mine", lambda m: None)

    assert (ini_dir / "Skyrim.ini").read_text() == "changed in game\n"


def test_new_vegas_takes_the_lists_falloutcustom_ini():
    """Real case (2026-11-26): Nuclear Sunset's falloutcustom.ini (TTW's
    SCharGenQuest and the rest) was kept aside, so TTW warned of misconfigured
    INI settings at the main menu."""
    class NewVegas:
        name = "Fallout New Vegas"
        _PROFILE_INI_SUBDIR = "ini files"

        def set_profile_ini_files(self, _on): ...

    targets, subdir = wi._profile_ini_targets(NewVegas())
    assert subdir == "ini files"
    assert "FalloutCustom.ini" in targets

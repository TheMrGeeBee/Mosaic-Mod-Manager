"""vanilla_archives: which BSAs in a game folder count as the base game."""
from __future__ import annotations

import os
from types import SimpleNamespace

from Utils.nif.catalog_loader import vanilla_archives

GAME = SimpleNamespace(
    vanilla_plugins=["Skyrim.esm", "Update.esm", "Dawnguard.esm"],
    vanilla_ccc_filename="Skyrim.ccc")


def _touch(d, *names):
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"x")


def names(paths):
    return [p.name for p in paths]


def test_engine_naming_and_plugin_order(tmp_path):
    data = tmp_path / "Data"
    _touch(data, "Skyrim - Textures0.bsa", "Skyrim - Meshes0.bsa", "Update.bsa",
           "Dawnguard.bsa", "Skyrim Bandit Expansion.bsa", "SkyrimReputation_SSE.bsa",
           "Skyrim - Meshes0.bsa.bak", "readme.txt")
    got = names(vanilla_archives(GAME, data, set()))
    assert got == ["Skyrim - Meshes0.bsa", "Skyrim - Textures0.bsa", "Update.bsa",
                   "Dawnguard.bsa"]          # mod-looking names never match


def test_creation_club_archives_follow_the_ccc_list(tmp_path):
    data = tmp_path / "Data"
    _touch(data, "Skyrim - Meshes0.bsa", "ccfoo001-bar.bsa", "ccother.bsa")
    (tmp_path / "Skyrim.ccc").write_text("ccfoo001-bar.esl\n\n")
    got = names(vanilla_archives(GAME, data, set()))
    assert got == ["Skyrim - Meshes0.bsa", "ccfoo001-bar.bsa"]


def test_archives_a_mod_ships_are_excluded(tmp_path):
    data = tmp_path / "Data"
    _touch(data, "Skyrim - Meshes0.bsa", "Dawnguard - HoV Req.bsa")
    got = names(vanilla_archives(GAME, data, {"dawnguard - hov req.bsa"}))
    assert got == ["Skyrim - Meshes0.bsa"]


def test_data_core_is_the_source_of_truth_when_present(tmp_path):
    core, data = tmp_path / "Data_Core", tmp_path / "Data"
    _touch(core, "Skyrim - Meshes0.bsa")
    _touch(data, "Skyrim - Meshes0.bsa", "Update.bsa")   # deployed farm: ignored
    assert names(vanilla_archives(GAME, data, set())) == ["Skyrim - Meshes0.bsa"]
    assert vanilla_archives(GAME, data, set())[0].parent == core


def test_files_deployed_from_staging_are_not_vanilla(tmp_path):
    staging, data = tmp_path / "staging", tmp_path / "Data"
    _touch(staging / "modA", "Update.bsa")
    _touch(data, "Skyrim - Meshes0.bsa")
    os.symlink(staging / "modA" / "Update.bsa", data / "Update.bsa")
    assert names(vanilla_archives(GAME, data, set(), staging)) == ["Skyrim - Meshes0.bsa"]


def test_missing_folder(tmp_path):
    assert vanilla_archives(GAME, None, set()) == []
    assert vanilla_archives(GAME, tmp_path / "nope", set()) == []


FO4_GAME = SimpleNamespace(
    vanilla_plugins=["Fallout4.esm", "DLCRobot.esm"],
    vanilla_ccc_filename="Fallout4.ccc",
    archive_extensions=frozenset({".ba2"}))


def test_ba2_extension_is_used_when_the_game_declares_it(tmp_path):
    data = tmp_path / "Data"
    _touch(data, "Fallout4 - Meshes.ba2", "Fallout4 - Textures1.ba2", "DLCRobot - Main.ba2",
           "Fallout4 - Meshes.bsa",                    # wrong extension for this game: never matches
           "SomeMod - Main.ba2")                        # mod-looking name: never matches
    got = names(vanilla_archives(FO4_GAME, data, set()))
    assert got == ["Fallout4 - Meshes.ba2", "Fallout4 - Textures1.ba2", "DLCRobot - Main.ba2"]

"""Where a Wabbajack (MO2) mod's files go, per game.

MO2 keeps each game's mods relative to its own data folder; Mosaic's handler
deploys a mod into its own. They're the same folder for most games; where
they aren't, the installer must move the files, or the game never sees them."""
from __future__ import annotations

from pathlib import Path

from Utils.wabbajack import wabbajack_install as wi
from Utils.wabbajack.wabbajack_manifest import FromArchiveDirective, ModList


def _plan(*tos):
    return wi.InstallPlan(mod_directives=[FromArchiveDirective(to=t) for t in tos])


class _Game:
    def __init__(self, root, data):
        self._root, self._data = Path(root), Path(root) / data if data is not None else None

    def get_game_path(self):
        return self._root

    def get_mod_data_path(self):
        return self._data


def test_dragon_age_files_go_under_packages_core_override(tmp_path):
    plan = _plan("Better Faces/face.mop", "Better Faces/meta.ini",
                 "Already Deep/packages/core/override/x.dds")
    wi.layout_root_builder_mods(plan, ModList(game_type="DragonAgeOrigins"),
                                _Game(tmp_path, "docs"))
    assert [d.to for d in plan.mod_directives] == [
        "Better Faces/packages/core/override/face.mop",
        "Better Faces/meta.ini",
        "Already Deep/packages/core/override/x.dds"]


def test_a_root_builder_mod_keeps_its_other_files_in_the_games_mod_folder(tmp_path):
    # Stardew deploys mods into Mods/, not Data/.
    plan = _plan("SMAPI/Root/StardewModdingAPI.exe", "SMAPI/ContentPatcher/manifest.json")
    layout = wi.layout_root_builder_mods(plan, ModList(game_type="StardewValley"),
                                         _Game(tmp_path, "Mods"))
    assert layout.converted == ["SMAPI"]
    assert [d.to for d in plan.mod_directives] == [
        "SMAPI/StardewModdingAPI.exe", "SMAPI/Mods/ContentPatcher/manifest.json"]


def test_bethesda_root_builder_mods_still_use_data(tmp_path):
    plan = _plan("SKSE/Root/skse64_loader.exe", "SKSE/Scripts/a.pex")
    wi.layout_root_builder_mods(plan, ModList(game_type="SkyrimSpecialEdition"),
                                _Game(tmp_path, "Data"))
    assert [d.to for d in plan.mod_directives] == [
        "SKSE/skse64_loader.exe", "SKSE/Data/Scripts/a.pex"]


def test_a_game_that_deploys_to_its_own_folder_only_moves_root_up(tmp_path):
    plan = _plan("Loader/Root/loader.dll", "Loader/data/x.cpk")
    wi.layout_root_builder_mods(plan, ModList(game_type="NieRAutomata"),
                                _Game(tmp_path, "."))
    assert [d.to for d in plan.mod_directives] == ["Loader/loader.dll", "Loader/data/x.cpk"]


def test_a_mod_folder_outside_the_game_gets_no_data_prefix(tmp_path):
    # The Sims 4 deploys into Documents; a Root Builder mod keeps its layout.
    game = _Game(tmp_path / "game", None)
    game._data = tmp_path / "Documents" / "Mods"
    assert wi._mod_data_subdir(game) == ""

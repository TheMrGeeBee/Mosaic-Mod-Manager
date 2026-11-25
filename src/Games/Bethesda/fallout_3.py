"""Fallout 3 -- the Fallout 3 specifics on top of the shared Bethesda base."""

from __future__ import annotations

from Games.base_game import WizardTool
from Games.Bethesda.bethesda_game import BethesdaGame


class Fallout_3(BethesdaGame):
    # FOSE (32-bit) loads MosaicPathIndex (Utils/path_index) from here.
    path_index_plugin_dir = "FOSE/Plugins"
    path_index_bits = 32

    vanilla_plugins = ["Fallout3.esm"]

    vanilla_dlc_plugins = [
        "Anchorage.esm", "ThePitt.esm", "BrokenSteel.esm",
        "PointLookout.esm", "Zeta.esm",
    ]

    synthesis_registry_name = "Fallout3"

    @property
    def name(self) -> str:
        return "Fallout 3"

    @property
    def game_id(self) -> str:
        return "Fallout3"

    @property
    def exe_name(self) -> str:
        return "Fallout3Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "22300"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout3"

    @property
    def loot_game_type(self) -> str:
        return "Fallout3"

    @property
    def loot_masterlist_repo(self) -> str:
        return "fallout3"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["fose_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["fose*.dll"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
                ]

    def _script_extender_loads_itself(self) -> "str | None":
        """The Fallout Anniversary Patcher (Mosaic's Downgrade wizard, or a
        list's manual-install files) rewrites Fallout3.exe to load FOSE itself
        and keeps the original as Fallout3_backup.exe. An older fose_loader
        put in the launcher's place rejects that exe ("unknown version", CRC
        6E3D50D1) -- real case: Fallout Rebirth's FOSE, 2026-11-25."""
        from Games.Bethesda.bethesda_ini import existing_case
        if self._game_path is None:
            return None
        if existing_case(self._game_path, "Fallout3_backup.exe").is_file():
            return ("Fallout3.exe is patched by the Fallout Anniversary Patcher "
                    "and loads FOSE itself")
        return None

    @property
    def restore_whitelist(self) -> list:
        """Keep the Anniversary Patcher's Fallout3_backup.exe in the game
        folder on restore -- it is both Steam's original exe and how Mosaic
        knows the game is patched (see _script_extender_loads_itself). Made
        while a profile is deployed, restore read it as a runtime file and
        moved it into the profile's Root_Folder."""
        from Utils.deploy.deploy import RestoreWhitelistRule
        return [RestoreWhitelistRule(path="", filenames=["Fallout3_backup.exe"])]

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="downgrade_fo3",
                label="Downgrade Fallout 3",
                description=(
                    "Downgrade to pre-Anniversary Edition so that "
                    "the script extender (FOSE) works correctly."
                ),
                dialog_class_path="wizards.fallout_downgrade.FalloutDowngradeWizard",
            ),
            WizardTool(
                id="install_se_fo3",
                label="Install Script Extender (FOSE)",
                description="Download and install FOSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://fose.silverlock.org/download/fose_v1_2_beta2.7z",
                    "archive_keywords": ["fose"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_fo3",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="FO3Edit", id_suffix="fo3",
                nexus_url="https://www.nexusmods.com/fallout3/mods/637?tab=files",
                nexus_file_id=1000026408,
            ),
            *self._bodyslide_linux_wizard_tools(),
        ]

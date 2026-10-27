"""
starfield.py
Starfield game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3
from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key


class Starfield(Fallout_3):

    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = [
        "Starfield.esm", "Constellation.esm", "ShatteredSpace.esm",
        "OldMars.esm", "SFBGS003.esm", "SFBGS004.esm", "SFBGS006.esm",
        "SFBGS007.esm", "SFBGS008.esm", "BlueprintShips-Starfield.esm",
        "SFBGS00D.esm", "SFBGS047.esm", "SFBGS050.esm", "BlueprintShips-SFBGS050.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    vanilla_ccc_filename = "Starfield.ccc"
    # The game reads Starfield.ccc from My Games\Starfield before the game root.
    ccc_in_my_games = True
    synthesis_registry_name = "Starfield"

    def content_catalog_files(self) -> list[str]:
        """Every Data/ file name the game's ContentCatalog.txt claims.

        The catalog is the game's own record of Creations installed from the
        in-game menu — Bethesda's free CC content and third-party ones alike,
        plugins plus their .ba2 archives. Those files sit in Data/ but belong
        to the game, not to Mosaic.
        """
        import json
        names: list[str] = []
        if self._prefix_path is None:
            return names
        catalog = self._prefix_path / self._APPDATA_SUBPATH / "ContentCatalog.txt"
        try:
            data = json.loads(catalog.read_text(encoding="utf-8-sig", errors="ignore"))
        except (OSError, ValueError):
            return names
        if not isinstance(data, dict):
            return names
        for entry in data.values():
            files = entry.get("Files") if isinstance(entry, dict) else None
            if not isinstance(files, list):
                continue
            names.extend(f for f in files if isinstance(f, str) and f.strip())
        return names

    def catalog_vanilla_plugins(self) -> list[str]:
        """Plugins the in-game Creations menu installed, per ContentCatalog.txt.

        Starfield ships no Starfield.ccc, and Creations downloaded in-game land
        in Data/ after the Data_Core snapshot, so without this the CC masters
        (e.g. sfbgs019.esm) look missing. Everything the catalog lists is the
        game's: Bethesda's free CC content (sfbgs*), its paid Creations
        (sfta*, kgcdoom) and third-party ones alike — the game downloaded them
        and Mosaic never staged them. The exception is a Creation the active
        profile also installs as a mod (the same content from Nexus): that copy
        IS Mosaic's, so it stays a mod row.
        """
        plugins = [f for f in self.content_catalog_files()
                   if f.lower().endswith((".esm", ".esl", ".esp"))]
        if not plugins:
            return plugins
        mod_owned = self._mod_deployed_plugin_names()
        return [f for f in plugins if f.lower() not in mod_owned]

    def _mod_deployed_plugin_names(self) -> set[str]:
        """Lowercased top-level plugin names the active profile's mods deploy.

        Read from filemap.txt, whose second field names the owning mod. The
        overwrite folder is not a mod — a Creation that an older build swept
        in there is still the game's content, not Mosaic's.
        """
        names: set[str] = set()
        try:
            filemap = self.get_effective_filemap_path()
        except Exception:
            return names
        try:
            with filemap.open(encoding="utf-8", errors="surrogateescape") as fh:
                for line in fh:
                    rel, _, mod = line.rstrip("\n").partition("\t")
                    if not mod or mod == "Overwrite":
                        continue
                    rel = rel.replace("\\", "/")
                    if "/" in rel:
                        continue  # top-level plugins only
                    low = rel.lower()
                    if low.endswith((".esm", ".esl", ".esp")):
                        names.add(low)
        except OSError:
            pass
        return names

    def plugins_txt_listed_vanilla(self) -> set[str]:
        """Creations must be listed in Plugins.txt or the engine ignores them.

        Only the hardcoded base masters (``vanilla_plugins``) are implicitly
        active in Starfield — libloadorder force-loads those and nothing else.
        A Creation is vanilla to Mosaic (the game owns the files) but still
        needs its ``*`` entry, so keep it out of the blanket vanilla exclusion
        in save_plugins.

        Only Creations whose file is actually in Data/ count. The catalog
        keeps listing a Creation whose files have gone (deleted by hand, or
        swept out by an older build's restore), and an entry for a missing
        plugin is one the panel's phantom prune strips straight back out —
        re-adding it every reload would just fight that prune.
        """
        names = {f.lower() for f in self.catalog_vanilla_plugins()}
        return (names & self._present_plugin_names()) if names else names

    def _present_plugin_names(self) -> set[str]:
        """Lowercased plugin file names currently in the game's Data dir."""
        try:
            data_dir = self.get_vanilla_plugins_path()
            if data_dir is None or not data_dir.is_dir():
                return set()
            return {e.name.lower() for e in data_dir.iterdir() if e.is_file()}
        except OSError:
            return set()

    @property
    def restore_whitelist(self) -> list:
        """Leave in-game Creations downloads in the game folder on restore.

        A Creation installed from the in-game menu while a profile is deployed
        lands in the live Data/ as a plain file: it is not in Data_Core (it
        postdates the snapshot) and not in any filemap, so the restore sweep
        read it as a runtime file and moved it into the profile's overwrite/,
        where deleting the profile destroyed it. The game put those files
        there and the game tracks them in ContentCatalog.txt, so whitelist
        them (and the game-root Creations/ staging folder) to keep them in
        place instead.
        """
        from Utils.deploy.deploy import RestoreWhitelistRule
        rules = [RestoreWhitelistRule(path="", folders=["Creations"])]
        catalog_files = self.content_catalog_files()
        if catalog_files:
            rules.append(RestoreWhitelistRule(path="Data", filenames=catalog_files))
        return rules

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_starfield",
                label="Install Script Extender (SFSE)",
                description="Download and install SFSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/starfield/mods/106",
                    "archive_keywords": ["sfse"],
                },
            ),
            WizardTool(
                id="run_bethini_starfield",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Starfield INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_starfield",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            # Starfield no longer has a dedicated Nexus xEdit build — it uses
            # the Discord-released xEdit build (xSFEdit) exclusively.
            *self._xedit_wizard_tools(
                build="SF1Edit", id_suffix="starfield",
                nexus_url="https://www.nexusmods.com/starfield/mods/121?tab=files",
                discord_only=True,
            ),
            *self._bodyslide_linux_wizard_tools(),
        ]

    @property
    def name(self) -> str:
        return "Starfield"

    @property
    def game_id(self) -> str:
        return "Starfield"

    @property
    def exe_name(self) -> str:
        # Starfield has no separate launcher; the main executable is the launch target.
        return "Starfield.exe"

    @property
    def steam_id(self) -> str:
        return "1716740"

    @property
    def nexus_game_domain(self) -> str:
        return "starfield"
    
    @property
    def loot_game_type(self) -> str:
        return "Starfield"

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".ba2"})

    @property
    def loot_masterlist_repo(self) -> str:
        return "starfield"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["sfse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["sfse*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".sfs"]),
        ]

    # Plugins.txt lives at AppData/Local/Starfield/Plugins.txt (capital P) —
    # same pattern as Oblivion. The class default drives plugins_txt_filename.
    _PLUGINS_TXT_FILENAME = "Plugins.txt"
    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Starfield")
    _APPDATA_SUBPATH_GOG = None
    _MYGAMES_SUBPATH = Path("Starfield")
    _MYGAMES_SUBPATH_GOG = None
    _ARCHIVE_INI_FILENAME = "Starfield.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "StarfieldPrefs.ini"
    _CUSTOM_INI_FILENAME = "StarfieldCustom.ini"
    # BA2-based — no dummy BSA.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None
    _archive_list_needs_mod_bsas = False

    # -- Loose-file loading -------------------------------------------------
    _STARFIELD_CUSTOM_INI_KEYS = (
        ("bInvalidateOlderFiles", "1"),
        ("sResourceDataDirsFinal", ""),
    )

    def _starfield_custom_ini_paths(self) -> list[Path]:
        """Return the StarfieldCustom.ini path in each managed My Games dir."""
        return [d / self._CUSTOM_INI_FILENAME
                for d in {p.parent for p in self._get_archive_ini_paths()}]

    def apply_archive_invalidation(self, log_fn) -> None:
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        if not self.archive_invalidation:
            self.revert_archive_invalidation(_log)
            return
        custom_inis = self._starfield_custom_ini_paths()
        if not custom_inis:
            _log("  WARN: Prefix path not set — skipping archive invalidation.")
            return
        for ini in custom_inis:
            ini.parent.mkdir(parents=True, exist_ok=True)
            for key, value in self._STARFIELD_CUSTOM_INI_KEYS:
                _set_ini_key(ini, "Archive", key, value)
        names = ", ".join(i.name for i in custom_inis)
        _log(f"  Archive invalidation enabled in {names}.")

    def revert_archive_invalidation(self, log_fn) -> None:
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        custom_inis = [i for i in self._starfield_custom_ini_paths() if i.is_file()]
        if not custom_inis:
            return
        for ini in custom_inis:
            for key, _value in self._STARFIELD_CUSTOM_INI_KEYS:
                if _read_ini_key(ini, "Archive", key) is not None:
                    _set_ini_key(ini, "Archive", key, None)
        names = ", ".join(i.name for i in custom_inis)
        _log(f"  Archive invalidation reverted in {names}.")

    @property
    def _script_extender_exe(self) -> str:
        return "sfse_loader.exe"

    def _plugins_txt_target(self) -> Path | None:
        """Return the in-prefix path where Starfield expects Plugins.txt (capital P
        by default; the Configure Game panel can override the casing)."""
        if self._prefix_path is None:
            return None
        return self._prefix_path / self._APPDATA_SUBPATH / self.plugins_txt_filename

    def _symlink_plugins_txt(self, profile: str, log_fn) -> None:
        """Write a Blueprint-stripped copy of Plugins.txt into the prefix.

        Starfield silently drops every plugin appearing after a Blueprint
        (or BlueprintShips) plugin in Plugins.txt, so the prefix-side file
        must omit them entirely — matching libloadorder's behavior. The
        profile's plugins.txt is left untouched so blueprints stay visible
        in the load-order UI.
        """
        from Utils.plugins.plugin_parser import is_blueprint_flagged
        from Utils.plugins.plugins import read_plugins

        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            _log("  WARN: Prefix path not set — skipping Plugins.txt write.")
            return

        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            _log(f"  WARN: plugins.txt not found at {source} — skipping write.")
            return

        if self._game_path is None:
            _log("  WARN: Game path not set — skipping Plugins.txt write.")
            return
        data_dir = self._game_path / "Data"

        entries = read_plugins(source, star_prefix=True)
        kept: list = []
        stripped: list[str] = []
        for e in entries:
            plugin_file = data_dir / e.name
            if plugin_file.is_file() and is_blueprint_flagged(plugin_file):
                stripped.append(e.name)
                continue
            kept.append(e)

        from Utils.plugins.plugins import deploy_plugins_copy
        lines = [(f"*{e.name}" if e.enabled else e.name) for e in kept]
        content = "\n".join(lines) + ("\n" if lines else "")
        deploy_plugins_copy(target.parent, target.name, content, _log)
        if stripped:
            _log(f"  Stripped {len(stripped)} Blueprint plugin(s) from Plugins.txt: "
                 + ", ".join(stripped))

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        """Remove the deployed Plugins.txt copy from the prefix on restore."""
        from Utils.plugins.plugins import remove_plugins_copy
        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            return
        remove_plugins_copy(target.parent, target.name, _log)

    def swap_launcher(self, log_fn) -> None:
        """Replace Starfield.exe with sfse_loader.exe and write Data/SFSE/sfse.ini.

        SFSE reads its RuntimeName setting from Data/SFSE/sfse.ini when the
        loader has been renamed away from sfse_loader.exe.
        """
        super().swap_launcher(log_fn)
        _log = log_fn
        if self._game_path is None:
            return
        backup_name = Path(self.exe_name).stem + ".bak"
        backup = self._game_path / backup_name
        if not backup.is_file():
            return
        sfse_ini = self._game_path / "Data" / "SFSE" / "sfse.ini"
        sfse_ini.parent.mkdir(parents=True, exist_ok=True)
        sfse_ini.write_text(f"[Loader]\nRuntimeName={backup_name}\n", encoding="utf-8")
        _log(f"  Wrote Data/SFSE/sfse.ini (RuntimeName={backup_name}).")

    def _restore_launcher(self, log_fn) -> None:
        """Reverse the launcher swap and remove Data/SFSE/sfse.ini."""
        super()._restore_launcher(log_fn)
        _log = log_fn
        if self._game_path is None:
            return
        sfse_ini = self._game_path / "Data" / "SFSE" / "sfse.ini"
        if sfse_ini.is_file():
            sfse_ini.unlink()
            _log("  Removed Data/SFSE/sfse.ini.")

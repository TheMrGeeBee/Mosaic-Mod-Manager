"""
mount_and_blade_2_bannerlord.py
Game handler for Mount & Blade II: Bannerlord.

Steam install: library folder is named Mount___Blade_II__Bannerlord under
  steamapps/common/.

Mod structure:
  Mods install into <game root>/Modules/
  Staged mods live in Profiles/Mount & Blade II: Bannerlord/mods/
"""

import shutil
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.deploy.deploy import (
    LinkMode,
    deploy_core,
    deploy_filemap,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    expand_separator_deploy_paths,
    cleanup_custom_deploy_dirs,
    move_to_core,
    restore_data_core,
)
from Utils.mods.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()


class MountAndBlade2Bannerlord(BaseGame):

    def __init__(self) -> None:
        super().__init__()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Mount & Blade II: Bannerlord"

    @property
    def game_id(self) -> str:
        return "Mount___Blade_II__Bannerlord"

    @property
    def exe_name(self) -> str:
        return "bin/Win64_Shipping_Client/Bannerlord.Native.exe"

    @property
    def steam_id(self) -> str:
        return "261550"

    @property
    def nexus_game_domain(self) -> str:
        return "mountandblade2bannerlord"

    @property
    def mods_dir(self) -> str:
        return "Modules"

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # Modules/ is reverted via its _Core backup; capture only files outside it.
        return {self.mods_dir.split("/")[0]}

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"modules"}

    @property
    def mod_root_folder_signal_dirs(self) -> set[str]:
        # A Bannerlord Module always nests its own bin/ one level under the
        # module's own folder (<ModuleName>/bin/...) — a mod archive whose
        # top level IS bin/ directly (BLSE, other BUTR tools) can only be a
        # root-folder payload, never a Module. Catches this even when a
        # Nexus collection's manifest doesn't tag the mod as install_type
        # "dinput" itself (not every collection curator sets it).
        return {"bin"}

    # -----------------------------------------------------------------------
    # Script extender (BLSE)
    # -----------------------------------------------------------------------
    # BLSE ships two loader exes side by side with the real Bannerlord.Native.exe
    # in bin/Win64_Shipping_Client/ — unlike the Bethesda xSE games it does NOT
    # replace the launcher on disk, so this uses framework_launch_exes (extra
    # Run-dropdown entries) rather than swap_launcher.
    _BLSE_SUBDIR = "bin/Win64_Shipping_Client"
    _BLSE_LAUNCHER_EXE = f"{_BLSE_SUBDIR}/Bannerlord.BLSE.Launcher.exe"
    _BLSE_LAUNCHER_EX_EXE = f"{_BLSE_SUBDIR}/Bannerlord.BLSE.LauncherEx.exe"

    @property
    def frameworks(self) -> dict[str, str]:
        return {"BLSE": self._BLSE_LAUNCHER_EXE}

    @property
    def framework_launch_exes(self) -> dict[str, str]:
        return {
            "BLSE (Vanilla Launcher)": self._BLSE_LAUNCHER_EXE,
            "BLSE (Extended Launcher)": self._BLSE_LAUNCHER_EX_EXE,
        }

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_blse",
                label="Install Script Extender (BLSE)",
                description=(
                    "Download and install BLSE into the game folder. "
                    "Also install Harmony below — BLSE requires it."
                ),
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/mountandblade2bannerlord/mods/1",
                    "archive_keywords": ["blse"],
                    # BLSE's zip top level IS "bin/" (containing
                    # Win64_Shipping_Client/ and the Game Pass variant) —
                    # meaningful, not a throwaway version wrapper.
                    "strip_top_dir": False,
                },
            ),
            WizardTool(
                id="install_harmony_bannerlord",
                label="Install Harmony (BLSE requirement)",
                description="Download and install Harmony, required by BLSE, into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/mountandblade2bannerlord/mods/2006",
                    "archive_keywords": ["harmony"],
                    # Zip top level IS "Modules/" (containing
                    # Bannerlord.Harmony/) — meaningful, not a wrapper.
                    "strip_top_dir": False,
                },
            ),
            WizardTool(
                id="install_dotnet472_bannerlord",
                label="Install .NET Framework 4.7.2 (required by BLSE)",
                description=(
                    "Install the legacy .NET Framework 4.7.2 into this game's "
                    "Proton prefix — BLSE's launcher needs it and crashes on "
                    "startup without it, separately from Bannerlord's own "
                    "bundled Mono runtime."
                ),
                dialog_class_path="wizards.winetricks_verb.WinetricksVerbWizard",
                extra={"verb": "dotnet472", "label": ".NET Framework 4.7.2",
                       "timeout": 1800},
            ),
        ]

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_mod_data_path(self) -> Path | None:
        """Mods go into Modules/ inside the game directory."""
        if self._game_path is None:
            return None
        return self._game_path / self.mods_dir

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def set_staging_path(self, path: Path | str | None) -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into <game root>/Modules/.

        Workflow:
          1. Move Modules/ → Modules_Core/  (vanilla backup)
          2. Transfer mod files listed in filemap.txt into Modules/
          3. Fill gaps with vanilla files from Modules_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        modules_dir = self._game_path / self.mods_dir
        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()
        core = self.mods_dir + "_Core"

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log(f"Step 1: Moving {modules_dir.name}/ → {core}/ ...")
        move_to_core(modules_dir, log_fn=_log)
        _log(f"  Backed up existing files → {core}/.")
        modules_dir.mkdir(parents=True, exist_ok=True)

        _log(f"Step 2: Transferring mod files into {modules_dir} ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(
            filemap, modules_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            core_dir=modules_dir.parent / (modules_dir.name + "_Core"),
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log(f"Step 3: Filling gaps with vanilla files from {core}/ ...")
        linked_core = deploy_core(modules_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in {modules_dir.name}/."
        )

        self._ensure_direct_blse_harmony_dll(_log)

        # Capture runtime files generated outside Modules/ on the next restore.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def _ensure_direct_blse_harmony_dll(self, log_fn) -> None:
        """Keep a direct copy of 0Harmony.dll in bin/Win64_Shipping_Client/.

        BLSE's launcher does its own default CLR assembly probing (same
        folder as its own exe) before it ever sets up a search path into
        Modules/ — without 0Harmony.dll sitting there directly, it throws a
        TypeLoadException on startup ("Could not load file or assembly
        '0Harmony'..."), even though Bannerlord's own module system is
        perfectly happy with Harmony living only under
        Modules/Bannerlord.Harmony/. Confirmed necessary in practice
        (2026-09-29) across two different collections/BLSE versions — a
        fresh collection install or Harmony update both drop this copy, so
        it's reapplied on every deploy rather than left as a one-off manual
        fix. A no-op when no Harmony module is installed (non-BLSE setups).
        """
        if self._game_path is None:
            return
        src = self._game_path / self.mods_dir / "Bannerlord.Harmony" / self._BLSE_SUBDIR / "0Harmony.dll"
        if not src.is_file():
            return
        dst = self._game_path / self._BLSE_SUBDIR / "0Harmony.dll"
        try:
            if dst.is_file() and dst.stat().st_size == src.stat().st_size:
                return
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            log_fn(f"  Copied 0Harmony.dll → {self._BLSE_SUBDIR}/ (BLSE bootstrap requirement).")
        except OSError as exc:
            log_fn(f"  Could not copy 0Harmony.dll for BLSE: {exc}")

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore Modules/ to vanilla: clear deployed mods and move Modules_Core/ back."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        modules_dir = self._game_path / self.mods_dir
        core = self.mods_dir + "_Core"
        core_dir = self._game_path / core

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        _log(f"Restore: clearing {modules_dir.name}/ and moving {core}/ back if present ...")
        restored = restore_data_core(
            modules_dir, core_dir=core_dir,
            overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log
        )
        if restored > 0:
            _log(f"  Restored {restored} file(s). {core}/ removed.")

        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")

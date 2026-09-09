"""
witcher_ee.py
Game handler for The Witcher: Enhanced Edition (2007, Aurora engine).

Mod structure
-------------
Unlike Bethesda-style games, TW1 has no vanilla Data/ folder that mods merge
into directly. Mods are loose-file overrides that live in a manager-managed
Data/override/ folder that does NOT ship with the base game and must be
created on demand — and removed when unused, since the engine has been
reported to crash if Data/override/ exists but is completely empty.

Two other mod-content shapes are routed to different locations via
custom_routing_rules:
  System/...     — patched binaries / ASI / ReShade proxy DLLs belong next to
                   witcher.exe in the game's System/ subfolder, not
                   Data/override/. Some ship with their own System/ wrapper
                   folder; loose (unwrapped) proxy DLLs are also recognised
                   by filename so they don't fall into Data/override/ by
                   default.
  Documents/...  — occasional per-user config/save content mirrors the
                   game's real config folder, which lives inside the Wine
                   prefix at drive_c/users/steamuser/Documents/The Witcher/.

Most mods ship as plain loose files with no recognised wrapper folder at all
(mod_install_as_is_if_no_match = True) and fall through to the default data
path, Data/override/, unchanged. Some mod archives mirror the manual-install
instructions from community guides ("create a Data\\Override folder") and
ship a Data/Override/... wrapper themselves — mod_folder_strip_prefixes
strips that down to bare content before it ever reaches the filemap.
"""

from __future__ import annotations

from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy.deploy import (
    CustomRule, LinkMode, deploy_filemap, deploy_custom_rules,
    restore_custom_rules, load_per_mod_strip_prefixes,
    load_separator_deploy_paths, expand_separator_deploy_paths,
    expand_separator_link_modes, expand_separator_raw_deploy,
    cleanup_custom_deploy_dirs,
)
from Utils.mods.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Relative path from a Wine/Proton prefix root to the user's home inside the
# virtual Windows filesystem (Documents/The Witcher/ lives under here).
_PREFIX_USER_SUBPATH = "drive_c/users/steamuser"


class WitcherEnhancedEdition(BaseGame):

    def __init__(self) -> None:
        super().__init__()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "The Witcher Enhanced Edition"

    @property
    def game_id(self) -> str:
        return "witcher_ee"

    @property
    def exe_name(self) -> str:
        return "System/witcher.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return []

    @property
    def steam_id(self) -> str:
        return "20900"

    @property
    def nexus_game_domain(self) -> str:
        return "witcher"

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        # TW1 mods ship in wildly inconsistent shapes — most are loose files
        # with no wrapper at all (same rationale as Dragon Age Origins).
        return True

    @property
    def filemap_casing(self) -> str:
        # Aurora engine reads Data/override contents case-sensitively under
        # Wine/Linux — same rationale as The Witcher 3 / Cyberpunk 2077.
        return "lower"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        # Some mod archives mirror the community "create Data\Override"
        # manual-install instructions and ship a Data/Override/... wrapper
        # themselves. This strip is applied repeatedly (first "data", then
        # "override") at install time, so it collapses that wrapper to bare
        # content without affecting genuinely loose mods.
        return {"data", "override"}

    @property
    def custom_routing_rules(self) -> list:
        return [
            # A mod-provided System/ wrapper folder -> game root's System/,
            # full relative path preserved (dest="" is the game root; the
            # preserved path already starts with "System/...").
            CustomRule(dest="", folders=["system"], flatten=False),
            # Loose ASI-loader / ReShade proxy DLLs with NO wrapper folder
            # must still land next to witcher.exe in System/, not
            # Data/override/ (where mod_install_as_is_if_no_match would
            # otherwise route them).
            CustomRule(dest="System", filenames=[
                "d3d9.dll", "dinput8.dll", "dxgi.dll", "opengl32.dll",
                "version.dll", "winmm.dll", "d3dcompiler_47.dll",
                "ReShade.ini",
            ], flatten=True, loose_only=True),
            # Per-user config/save content shipped with its own Documents/
            # wrapper -> the Wine prefix's Documents folder, full relative
            # path preserved under the prefix's user-home subpath.
            CustomRule(dest=_PREFIX_USER_SUBPATH, folders=["documents"],
                       flatten=False, to_prefix=True),
        ]

    @property
    def restore_on_close_eligible(self) -> bool:
        return True  # restore is cheap: unlink + prune, no repack

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into Data/override/, created on demand — this folder
        never ships with the vanilla game. Hardcoded lowercase "override"
        regardless of filemap_casing: the engine expects this literal name,
        and it is never a casing question since no mod ships this folder
        itself (Mosaic always creates it fresh)."""
        if self._game_path is None:
            return None
        return self._game_path / "Data" / "override"

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_hardlink_deploy_targets(self) -> list[tuple[str, "Path | None"]]:
        return [
            ("Game directory", self._game_path),
            ("Wine prefix (Documents/config)", self._prefix_path),
        ]

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------
    # load_paths / save_paths are inherited from BaseGame (profile-aware).

    def set_staging_path(self, path: "Path | str | None") -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def set_prefix_path(self, path: "Path | str | None") -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods.

        Workflow:
          1. Route System/ and Documents/ content via custom rules
             (System/ -> game root's System/ folder; Documents/ -> the
             Wine prefix's Documents folder).
          2. Transfer remaining mod files listed in filemap.txt into
             Data/override/, created on demand.
          3. Prune Data/override/ if it ended up empty (no override-bound
             mod content) — the Aurora engine reportedly crashes on a
             present-but-empty override folder.

        There is no vanilla "Core" backup/gap-fill step here: unlike
        Skyrim's Data/ or DAO's managed subfolders, Data/override/ never
        ships with the base game and never contains vanilla content.
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        override_dir = self.get_mod_data_path()
        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries) or None

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 1: Routing System/ and Documents/ content via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                log_fn=_log,
                progress_fn=progress_fn,
                prefix_root=self.get_prefix_path(),
                raw_mods=per_mod_raw,
            )
            _log(f"  Routed {len(custom_exclude)} file(s).")

        _log("Step 2: Transferring remaining mod files into Data/override/ ...")
        override_dir.mkdir(parents=True, exist_ok=True)
        linked_mod, _placed = deploy_filemap(
            filemap, override_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            per_mod_link_modes=per_mod_modes,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        # No vanilla content to gap-fill — Data/override/ never ships with the
        # base game. If nothing landed there, remove it again so the engine
        # never sees a present-but-empty override folder.
        if override_dir.is_dir() and not any(override_dir.iterdir()):
            override_dir.rmdir()
            _log("  Data/override/ is empty — removed (no override-bound mod files).")

        _log(
            f"Deploy complete. {linked_mod} file(s) in Data/override/ "
            f"(+ {len(custom_exclude)} custom-routed file(s))."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mod files and Data/override/ if now empty."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        override_dir = self.get_mod_data_path()

        custom_rules = self.custom_routing_rules
        if custom_rules:
            _log("Restore: removing custom-routed System/ and Documents/ files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=custom_rules,
                log_fn=_log,
                prefix_root=self.get_prefix_path(),
            )

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        if override_dir is not None and override_dir.is_dir():
            _log("Restore: removing deployed files from Data/override/ ...")
            removed = self._remove_deployed_files(override_dir, log_fn=_log)
            _log(f"  Removed {removed} file(s).")
            self._prune_empty_subdirs(override_dir)
            if not any(override_dir.iterdir()):
                override_dir.rmdir()
                _log("  Data/override/ is now empty — removed.")

        _log("Restore complete.")

    def _remove_deployed_files(self, override_dir: Path, log_fn=None) -> int:
        """Delete every filemap-listed file that landed directly under
        Data/override/. Entries claimed by a custom rule (System/,
        Documents/) were deployed elsewhere and are already handled by
        restore_custom_rules() above — trying to unlink them here is a
        harmless no-op since they never exist under override_dir."""
        _log = log_fn or (lambda _: None)
        filemap = self.get_effective_filemap_path()
        if not filemap.is_file():
            return 0
        removed = 0
        try:
            lines = filemap.read_text(
                encoding="utf-8", errors="surrogateescape").splitlines()
        except OSError as exc:
            _log(f"  Warning: could not read filemap: {exc}")
            return 0
        for line in lines:
            if "\t" not in line:
                continue
            rel = line.split("\t", 1)[0].strip()
            if not rel:
                continue
            target = override_dir / rel
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                    removed += 1
            except OSError as exc:
                _log(f"  Warning: failed to remove {rel}: {exc}")
        return removed

    def _prune_empty_subdirs(self, root: Path) -> None:
        """Remove now-empty nested subdirectories under root (e.g. a mod's
        own "textures/" folder inside Data/override/) bottom-up, leaving
        root itself for the caller to check/remove."""
        import os
        for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
            p = Path(dirpath)
            if p == root:
                continue
            try:
                if not any(p.iterdir()):
                    p.rmdir()
            except OSError:
                pass

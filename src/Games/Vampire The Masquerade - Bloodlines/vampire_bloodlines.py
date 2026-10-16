"""
vampire_bloodlines.py
Game handler for Vampire: The Masquerade - Bloodlines.

Source-engine mod folders:
  Mods never touch the vanilla ``Vampire/`` folder. Each Mosaic profile
  deploys into its own sibling folder, ``Mosaic_<profile>/``, which the game
  loads with ``Vampire.exe -game Mosaic_<profile>`` (mod folder names must
  contain no spaces). ``-game`` is provided by the Bloodlines Advanced EXE
  Loader (``Loader.exe`` + ``Bin/loader``), bundled with the Unofficial Patch.

Layering (lowest → highest priority):
  Vampire/            vanilla, never written
  Unofficial_Patch/   base patch layer, populated by the Unofficial Patch wizard
  Mosaic_<profile>/   staged mods deployed on top of the patch layer

Launching:
  Play runs ``steam -applaunch 2600 -game Mosaic_<profile>`` (steam://rungameid
  drops arguments). Steam's own ``vampire.exe`` is a 25 KB stub without the 4 GB
  flag; the Unofficial Patch wizard replaces it with the patched Advanced EXE, as
  the official installer does under Wine. Deploy warns while it is unpatched.

Scripts under python/ are plain .py files run by the game's embedded Python 2.1
interpreter; Mosaic only links or copies them and never byte-compiles anything.
"""

import hashlib
import json
import os
import re
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

# Files the game rewrites at runtime. They must be real per-profile copies: a link
# into the patch layer or a mod's staged folder would let the engine write straight
# through it into the shared layer (it did, overwriting the HD overhaul's own
# textures). Once rescued to Overwrite on Restore they are redeployed from there,
# so they survive the Restore + Deploy of every Play.
#   * cfg/config.cfg — the game's own settings.
#   * the hacking-terminal files — the player's name is written into them at load.
#   * the outputs of the Unofficial Patch's "Vampire hunter switch" (python/
#     vamputil.py copies "<file> - hunter/vampire" variants over these base files
#     according to the character's clan); the list is every copyfile() destination
#     in patch 12.0.
_MUTABLE_PATCH_FILES = (
    "cfg/config.cfg",
    "vdata/hackterminals/*.txt",
    "materials/hud/disciplines/bloodheal.tth",
    "materials/hud/disciplines/bloodheal.ttz",
    "materials/hud/disciplines/bloodheal_base.tth",
    "materials/hud/disciplines/bloodheal_base.ttz",
    "materials/hud/new_ui/bloodbar.vmt",
    "materials/hud/new_ui/bloodbar/bloodbar.vmt",
    "materials/hud/new_ui/bloodbarbottom.vmt",
    "materials/hud/new_ui/bloodbarfill.vmt",
    "materials/hud/new_ui/blooduseglow.vmt",
    "materials/hud/new_ui/blooduseiconglow.vmt",
    "materials/interface/charactermaintenance/cm_topbar.tth",
    "materials/interface/charactermaintenance/cm_topbar.ttz",
    "scripts/kb_act.lst",
    "sound/interface/infobar/need_more_blood.wav",
    "vdata/items/item_a_body_armor.txt",
    "vdata/items/item_a_hvy_cloth.txt",
    "vdata/items/item_a_hvy_leather.txt",
    "vdata/items/item_a_lt_leather.txt",
    "vdata/signs/death.txt",
    "vdata/system/credits.txt",
    "vdata/system/stats.txt",
    "vdata/system/strings.txt",
    "vdata/system/traiteffects000.txt",
)
_PRIVATE_MANIFEST = "private_copies.json"     # in the profile dir; rel -> sha1 at copy time
# Folders of per-profile game state (saves, per-map HL1-3 state). Restore moves
# them to Overwrite; because the filemap is not rebuilt between that Restore and
# the next Deploy, they are re-linked from Overwrite on every deploy.
_STATE_DIRS = ("save",)

_STEAM_ID = "2600"
_VANILLA_DIR = "Vampire"
_PATCH_DIR = "Unofficial_Patch"
_MOD_DIR_PREFIX = "Mosaic_"


def _mod_dir_name(profile: str) -> str:
    """Return the ``-game`` folder name for a profile (no spaces allowed)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", profile or "default").strip("_")
    return _MOD_DIR_PREFIX + (safe or "default")


class VampireBloodlines(BaseGame):

    def __init__(self) -> None:
        super().__init__()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Vampire: The Masquerade - Bloodlines"

    @property
    def game_id(self) -> str:
        return "vampire_bloodlines"

    @property
    def exe_name(self) -> str:
        # Steam installs it lowercase; Linux paths are case-sensitive.
        return "vampire.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["Vampire.exe"]

    @property
    def steam_id(self) -> str:
        return _STEAM_ID

    @property
    def nexus_game_domain(self) -> str:
        return "vampirebloodlines"

    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    @property
    def loot_game_type(self) -> str:
        return ""

    @property
    def loot_masterlist_url(self) -> str:
        return ""

    # -----------------------------------------------------------------------
    # Mod archive layout
    # -----------------------------------------------------------------------

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        # Pre-Loader mods ship a renamed copy of the vanilla "Vampire" tree.
        return {"vampire", _PATCH_DIR.lower()}

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {
            "cfg", "cl_dlls", "dlg", "dlls", "maps", "materials", "media",
            "models", "particles", "python", "resource", "scripts", "sound",
            "textures", "vdata",
        }

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        # The Unofficial Patch locks config.cfg on purpose; mods must not fight over it.
        return {"config.cfg"}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def _active_profile_name(self) -> str:
        pdir = self._active_profile_dir
        return pdir.name if pdir is not None else "default"

    def get_mod_dir_name(self, profile: str | None = None) -> str:
        return _mod_dir_name(profile or self._active_profile_name())

    def get_patch_path(self) -> Path | None:
        """The Unofficial Patch base layer (sibling of Vampire/)."""
        if self._game_path is None:
            return None
        return self._game_path / _PATCH_DIR

    def get_mod_data_path(self) -> Path | None:
        """Deploy target: the active profile's own -game folder."""
        if self._game_path is None:
            return None
        return self._game_path / self.get_mod_dir_name()

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
    # Launch
    # -----------------------------------------------------------------------

    @property
    def frameworks(self) -> dict[str, str]:
        """The Unofficial Patch, so Nexus requirements naming it count as met and
        the framework banner reports it. It is a base layer installed by the
        wizard (or prepatched, e.g. GOG), not a Mosaic mod, so detection is by
        its version file — in Unofficial_Patch/ or, if that is where it was
        found, the vanilla Vampire/ folder."""
        from Utils.modding_tools.vtmb_patch import read_installed_version
        folder = _PATCH_DIR
        if self._game_path is not None:
            found = read_installed_version(self._game_path)
            if found:
                folder = found[1]
        return {"Unofficial Patch": f"{folder}/resource/gameui_english.txt"}

    def offsite_mod_satisfied(self, name: str) -> bool:
        """A collection that lists the Unofficial Patch installer (VTMBup…exe) as an
        off-site download doesn't need it when this patch or a newer one is already
        installed — the patch is a base layer, not a mod, and collections often pin
        an older one."""
        from Utils.modding_tools.vtmb_patch import (
            installed_version_satisfies, version_from_installer_name)
        wanted = version_from_installer_name(name)
        return (wanted is not None and self._game_path is not None
                and installed_version_satisfies(self._game_path, wanted))

    @property
    def framework_launch_exes(self) -> dict[str, str]:
        # The Loader's own picker lists every mod folder in the game directory
        # (Vanilla, Unofficial_Patch, Mosaic_<profile>, total conversions such
        # as The Final Nights Reborn) and starts the chosen one. No -game here.
        return {"VTMB Loader": "Loader.exe"}

    def get_launch_command(self) -> "list[str] | None":
        """Steam with ``-game`` (steam://rungameid drops args)."""
        import shutil
        cmd = ["steam", "-applaunch", _STEAM_ID, "-game", self.get_mod_dir_name()]
        if Path("/.flatpak-info").exists() and shutil.which("flatpak-spawn"):
            return ["flatpak-spawn", "--host", *cmd]
        return cmd

    # -----------------------------------------------------------------------
    # Wizards
    # -----------------------------------------------------------------------

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_unofficial_patch",
                label="Install Unofficial Patch",
                description=(
                    "Download the Unofficial Patch (Nexus mod 80) and unpack "
                    "Unofficial_Patch/, the Loader and the 4 GB-patched "
                    "Vampire.exe. Never touches Vampire/ or Steam's exe."
                ),
                dialog_class_path="wizards.vtmb_patch.VtmbPatchWizard",
            ),
        ]

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def _check_target(self, target: Path) -> None:
        """Hard guard: never write into the vanilla Vampire/ folder."""
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")
        vanilla = (self._game_path / _VANILLA_DIR).resolve()
        resolved = target.resolve()
        if resolved == vanilla or vanilla in resolved.parents:
            raise RuntimeError(
                f"Refusing to deploy into the vanilla {_VANILLA_DIR}/ folder: {target}"
            )

    @staticmethod
    def _mutable_rels(*layers: "Path | None") -> list[str]:
        """Paths (relative to a layer root) matching _MUTABLE_PATCH_FILES in any of
        *layers* — the patch layer, the saved copies in Overwrite, the profile folder."""
        out: set[str] = set()
        for root in layers:
            if root is None or not root.is_dir():
                continue
            for pattern in _MUTABLE_PATCH_FILES:
                if any(c in pattern for c in "*?["):
                    out.update(p.relative_to(root).as_posix()
                               for p in root.glob(pattern) if p.is_file())
                elif (root / pattern).is_file():
                    out.add(pattern)
        return sorted(out)

    @staticmethod
    def _sha1(path: Path) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _load_manifest(profile_dir: "Path | None") -> dict:
        try:
            return json.loads((profile_dir / _PRIVATE_MANIFEST).read_text("utf-8"))
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    @staticmethod
    def _save_manifest(profile_dir: "Path | None", data: dict) -> None:
        if profile_dir is None:
            return
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / _PRIVATE_MANIFEST).write_text(json.dumps(data, indent=0), "utf-8")
        except OSError:
            pass

    @staticmethod
    def _drop_unchanged_private_copies(mod_dir: Path, profile_dir: "Path | None", log) -> int:
        """Before Restore: delete private copies still identical to what they were
        when made, so only real changes are rescued to Overwrite (an unchanged copy
        saved there would later shadow an updated patch or mod file)."""
        manifest = VampireBloodlines._load_manifest(profile_dir)
        dropped = 0
        for rel, original in manifest.items():
            dest = mod_dir / rel
            try:
                if (dest.is_file() and not dest.is_symlink()
                        and VampireBloodlines._sha1(dest) == original):
                    dest.unlink()
                    dropped += 1
            except OSError as exc:
                log(f"  WARN: could not check {rel}: {exc}")
        return dropped

    @staticmethod
    def _materialize_mutable_files(mod_dir: Path, patch_dir: Path,
                                   overwrite_dir: "Path | None",
                                   profile_dir: "Path | None", log) -> int:
        """Give runtime-written files a private, real copy in the profile folder,
        so the engine never writes into a shared layer (the patch or a mod).

        The copy comes from the user's rescued copy in Overwrite when there is
        one (Restore moves runtime files there, but the filemap is not
        necessarily rebuilt before the next deploy, so it can't be relied on to
        redeploy them); otherwise from whatever the deploy linked there — the
        patch default or a mod that overrides it (e.g. the HD overhaul). That also
        covers a file nothing ships at all (a pristine patch has no
        cfg/config.cfg): the game creates it, Restore saves it, and it comes back.
        A file already placed from Overwrite is left alone.
        """
        done = from_saved = 0
        manifest: dict[str, str] = {}
        overwrite_real = Path(os.path.realpath(overwrite_dir)) if overwrite_dir else None
        # The profile folder itself is a source too: a mod may ship one of these
        # files even when the patch doesn't.
        for rel in VampireBloodlines._mutable_rels(patch_dir, overwrite_dir, mod_dir):
            dest = mod_dir / rel
            saved = overwrite_dir / rel if overwrite_dir is not None else None
            has_saved = saved is not None and saved.is_file()
            try:
                linked = dest.is_symlink() or (dest.is_file() and dest.stat().st_nlink > 1)
                if dest.is_symlink() or dest.exists():
                    if not linked:
                        continue                          # already a private real file
                    target = Path(os.path.realpath(dest))
                    if overwrite_real is not None and overwrite_real in target.parents:
                        continue                          # placed from Overwrite: that IS the saved copy
                    if not target.is_file():
                        continue
                    src = saved if has_saved else target
                elif has_saved:
                    src = saved
                else:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".mm-private")
                shutil.copy2(src, tmp)
                os.chmod(tmp, 0o644)
                os.replace(tmp, dest)                     # replaces a symlink itself, not its target
                manifest[rel] = VampireBloodlines._sha1(dest)
                done += 1
                from_saved += src == saved
            except OSError as exc:
                log(f"  WARN: could not make {rel} a private copy: {exc}")
        VampireBloodlines._save_manifest(profile_dir, manifest)
        if done:
            log(f"  Made {done} runtime-written file(s) private to this profile "
                f"({from_saved} from your saved copy, {done - from_saved} from the layer below).")
        return done

    @staticmethod
    def _relink_saved_state(mod_dir: Path, overwrite_dir: "Path | None", log) -> int:
        """Bring saves back from Overwrite after a Restore.

        Links (never copies, never replaces) every file under overwrite/<state
        dir> that the deploy hasn't already placed. A file the filemap placed
        from Overwrite is already there and is skipped; a real file or the
        patch's own placeholder is never replaced.
        """
        if overwrite_dir is None:
            return 0
        done = 0
        for folder in _STATE_DIRS:
            root = overwrite_dir / folder
            if not root.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    src = Path(dirpath) / fn
                    dest = mod_dir / src.relative_to(overwrite_dir)
                    if dest.exists() or dest.is_symlink():
                        continue
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.symlink_to(src)
                        done += 1
                    except OSError as exc:
                        log(f"  WARN: could not restore {dest.name}: {exc}")
        if done:
            log(f"  Restored {done} saved game file(s) from Overwrite.")
        return done

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into <game root>/Mosaic_<profile>/.

        Workflow:
          1. Move any existing contents of the profile folder → <folder>_Core/
          2. Transfer mod files listed in filemap.txt into the profile folder
          3. Fill gaps with Unofficial_Patch/ files (the patch base layer)
        Vampire/ is never written to.
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        mod_dir = self._game_path / self.get_mod_dir_name(profile)
        self._check_target(mod_dir)
        filemap = self.get_effective_filemap_path()
        staging = self.get_effective_mod_staging_path()

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        from Utils.modding_tools.vtmb_patch import find_game_exe, is_large_address_aware
        game_exe = find_game_exe(self._game_path)
        if game_exe is not None and not is_large_address_aware(game_exe):
            self.add_deploy_warning(
                f"{game_exe.name} is Steam's stock exe (no 4 GB patch) and tends to "
                "crash with mods. Run Wizard → Install Unofficial Patch to install "
                "the patched exe."
            )
        if not (self._game_path / "Loader.exe").is_file() and \
                not (self._game_path / "Bin" / "loader").is_dir():
            self.add_deploy_warning(
                "The Bloodlines Advanced EXE Loader (Loader.exe, Bin/loader) was not "
                "found. Stock Vampire.exe cannot be relied on to honour -game; install "
                "the Unofficial Patch (it bundles the Loader) or the standalone Loader."
            )

        _log(f"Step 1: Moving {mod_dir.name}/ → {mod_dir.name}_Core/ ...")
        move_to_core(mod_dir, log_fn=_log)
        mod_dir.mkdir(parents=True, exist_ok=True)

        _log(f"Step 2: Transferring mod files into {mod_dir.name}/ ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(
            filemap, mod_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            log_fn=_log,
            progress_fn=progress_fn,
            core_dir=mod_dir.parent / (mod_dir.name + "_Core"),
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        patch_dir = self.get_patch_path()
        linked_patch = 0
        if patch_dir is not None and patch_dir.is_dir():
            _log(f"Step 3: Filling gaps with {_PATCH_DIR}/ files ...")
            # Patch files are always linked (never copied) so restore can tell
            # them apart from runtime-created files.
            patch_mode = LinkMode.SYMLINK if mode == LinkMode.COPY else mode
            linked_patch = deploy_core(mod_dir, placed, core_dir=patch_dir,
                                       mode=patch_mode, log_fn=_log)
            _log(f"  Transferred {linked_patch} patch file(s).")
        else:
            _log(f"Step 3: {_PATCH_DIR}/ not found — skipping base patch layer.")
        self._materialize_mutable_files(
            mod_dir, self._game_path / _PATCH_DIR, self.get_effective_overwrite_path(),
            profile_dir, _log)
        self._relink_saved_state(mod_dir, self.get_effective_overwrite_path(), _log)

        _log(
            f"Deploy complete. {linked_mod} mod + {linked_patch} patch "
            f"= {linked_mod + linked_patch} file(s) in {mod_dir.name}/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Clear the profile folder (runtime files are rescued to overwrite/)."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        mod_dir = self._game_path / self.get_mod_dir_name()
        self._check_target(mod_dir)

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        self._drop_unchanged_private_copies(mod_dir, self._active_profile_dir, _log)

        _log(f"Restore: clearing {mod_dir.name}/ ...")
        restored = restore_data_core(
            mod_dir, overwrite_dir=self.get_effective_overwrite_path(), log_fn=_log
        )
        if restored > 0:
            _log(f"  Restored {restored} file(s).")

        _log("Restore complete.")

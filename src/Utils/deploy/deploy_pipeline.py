"""
Shared deploy orchestration used by the Deploy button, Run EXE (Play),
the BodySlide / DynDOLOD wizards, and the CLI.

`run_deploy_pipeline` performs the full restore → build_filemap → deploy →
wine-dll → root-folder → root-flagged → swap_launcher sequence. UI-specific
concerns (button enable/disable, status bar, mod panel reload) stay at the
call site.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Callable, Optional

from Utils.deploy.deploy import (
    LinkMode,
    deploy_root_folder,
    deploy_root_flagged_mods,
    load_per_mod_strip_prefixes,
    restore_root_folder,
)
from Utils.deploy.deploy_shared import _FILEMAP_SNAPSHOT_NAME
from Utils.filemap import build_filemap
from Utils.profile.profile_backup import create_backup
from Utils.profile.profile_state import read_excluded_mod_files
from Utils.ui_config import load_normalize_folder_case
from Utils.wine_proton.protontricks import ensure_prefix_deps
from Utils.wine_proton.wine_dll_config import deploy_game_wine_dll_overrides


LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, Optional[str]], None]


def check_paths_mounted(game) -> "str | None":
    """Return an error message if the game or staging drive looks unmounted.

    Guards against deploying into (or restoring under) a dead mountpoint:
    mkdir(parents=True) would silently recreate the game tree on the root
    filesystem and every file would land on the wrong drive.
    """
    import os

    game_root = _safe(game.get_game_path)
    if game_root:
        p = Path(game_root)
        if not p.is_dir():
            return (f"game folder not found: {p} — is the drive mounted?")
        try:
            with os.scandir(p) as it:
                if next(it, None) is None:
                    return (f"game folder is empty: {p} — is the drive mounted?")
        except OSError as exc:
            return f"game folder not accessible: {p} ({exc})"

    profile_root = _safe(game.get_profile_root)
    if profile_root is not None:
        pr = Path(profile_root)
        if not pr.is_dir():
            return (f"mod staging/profile folder not found: {pr} — "
                    f"is the drive mounted?")
        try:
            with os.scandir(pr) as it:
                if next(it, None) is None:
                    return (f"mod staging/profile folder is empty: {pr} — "
                            f"is the drive mounted?")
        except OSError as exc:
            return f"mod staging/profile folder not accessible: {pr} ({exc})"

    return None


def _fs_id(path: Path) -> "int | None":
    """Return the device id for *path* (or its nearest existing parent).

    Used to detect up-front when the game directory and the mod staging live
    on different filesystems — the single most common cause of hardlink
    deploys silently falling back to copy/symlink.
    """
    p = path
    for _ in range(40):
        try:
            return p.stat().st_dev
        except OSError:
            if p.parent == p:
                return None
            p = p.parent
    return None


def _count_enabled_mods(profile_dir: Path) -> "tuple[int, int]":
    """Return (enabled_mods, separators) from the profile's modlist.txt."""
    try:
        from Utils.mods.modlist import read_modlist
        entries = read_modlist(profile_dir / "modlist.txt")
    except Exception:
        return (0, 0)
    enabled = sum(1 for e in entries if e.enabled and not e.is_separator)
    seps = sum(1 for e in entries if e.is_separator)
    return (enabled, seps)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _missing_req_pairs(raw: str) -> "list[tuple[int, str]]":
    """(modId, name) pairs from a meta.ini `missing_requirements` value
    (semicolon-separated `modId:name` entries — the name half may be blank
    for locally-seeded requirements, e.g. the TTW installer)."""
    pairs: list[tuple[int, str]] = []
    for part in (raw or "").split(";"):
        raw_id, _, name = part.partition(":")
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            pairs.append((int(raw_id), name.strip()))
        except ValueError:
            pass
    return pairs


def _warn_missing_requirements(game, profile_dir: Path, *, log_fn: LogFn) -> None:
    """Queue a deploy warning for every enabled mod with an un-ignored Nexus
    "missing requirement" that is STILL actually missing — the same
    yellow-exclamation flag already shown in the Mods tab (FLAG_MISSING_REQS),
    but previously only a passive icon: deploy proceeded silently even when a
    mod's declared framework/requirement isn't installed and the mod likely
    won't work at all. Game-agnostic — every game shares the same meta.ini
    field and the same add_deploy_warning()/toast mechanism.

    The stored `missing_requirements` string is a snapshot from whenever it
    was last checked (install time / Check Updates) — it goes stale, so it is
    NOT trusted verbatim. Three live cross-checks first rule out anything no
    longer actually missing:
      1. Another currently-enabled mod's own recorded Nexus mod_id matches —
         the requirement was installed later and the cached flag just never
         got refreshed.
      2. The requirement names a native framework (Script Extender, Native
         Mod Loader, ...) that this game handler verifies itself via
         `game.frameworks` (the same check driving the green/red Plugins-tab
         banner) — Nexus's site-side "requirements" list still names these
         even though they are never installed as a staged mod with their own
         meta.ini, so the mod_id check above can never satisfy them.
      3. BG3 only: an enabled mod's pak provides the UUID that the
         requirement's name maps to (``Utils.mods.bg3_requirements``) — a
         fork on its own Nexus page, e.g. "ImpUI P8 Fork" for ImpUI.
    """
    if not hasattr(game, "add_deploy_warning"):
        return
    try:
        from Nexus.nexus_meta import read_meta
        from Utils.mods.modlist import read_modlist
        from Utils.profile.profile_state import read_ignored_missing_requirements
        staging = game.get_effective_mod_staging_path()
        entries = read_modlist(profile_dir / "modlist.txt")
        ignored = read_ignored_missing_requirements(profile_dir)
    except Exception as exc:
        log_fn(f"  Missing-requirements check skipped: {exc}")
        return

    enabled_entries = [e for e in entries if e.enabled and not e.is_separator]

    metas_by_name = {}
    installed_mod_ids: set[int] = set()
    for e in enabled_entries:
        meta_path = staging / e.name / "meta.ini"
        if not meta_path.is_file():
            continue
        try:
            meta = read_meta(meta_path)
        except Exception:
            continue
        metas_by_name[e.name] = meta
        mod_id = getattr(meta, "mod_id", 0) or 0
        if mod_id:
            installed_mod_ids.add(mod_id)

    present_frameworks: list[str] = []
    frameworks = getattr(game, "frameworks", None) or {}
    if frameworks:
        game_root = _safe(game.get_game_path)
        if game_root:
            game_root = Path(game_root)
            for fw_name, rel_exe in frameworks.items():
                if (game_root / rel_exe).is_file():
                    present_frameworks.append(fw_name.lower())

    # BG3 only: a requirement met by a pak UUID from a mod under a different
    # Nexus id (e.g. a fork) — scanned lazily, only if something is missing.
    from Utils.mods.bg3_requirements import LazyPakRequirementCheck
    pak_satisfied = LazyPakRequirementCheck(
        _safe(lambda: game.nexus_game_domain, "") or "", staging,
        [e.name for e in enabled_entries])

    # Either/or requirements and external tools from updatefilter.txt (e.g.
    # Unique Tav *or* KAVT) — local copies only, no network at deploy.
    game_domain = _safe(lambda: game.nexus_game_domain, "") or ""
    try:
        from Nexus.nexus_requirements import (
            _alternative_satisfied_for_game, _is_external_for_game,
            load_requirement_filter_offline,
        )
        external_set, alternatives = load_requirement_filter_offline()
    except Exception:
        external_set, alternatives = set(), {}
        _alternative_satisfied_for_game = _is_external_for_game = None

    def filtered_out(mod_id: int) -> bool:
        if not mod_id or _is_external_for_game is None:
            return False
        return (_is_external_for_game(game_domain, mod_id, external_set)
                or _alternative_satisfied_for_game(
                    game_domain, mod_id, installed_mod_ids, alternatives))

    affected: "list[tuple[str, list[str]]]" = []
    for e in enabled_entries:
        if e.name in ignored:
            continue
        meta = metas_by_name.get(e.name)
        if meta is None:
            continue
        raw = getattr(meta, "missing_requirements", "") or ""
        if not raw:
            continue
        # Per-requirement ignores (meta.ini ignoredRequirements), the same
        # ones the Mods-tab flag already honours.
        ignored_ids = {mid for mid, _n in _missing_req_pairs(
            getattr(meta, "ignored_requirements", "") or "")}
        still_missing: list[str] = []
        for mod_id, name in _missing_req_pairs(raw):
            if mod_id and mod_id in ignored_ids:
                continue    # the user dismissed this requirement
            if mod_id and mod_id in installed_mod_ids:
                continue    # satisfied by another currently-enabled mod
            if any(fw in (name or "").lower() for fw in present_frameworks):
                continue    # a native framework, already verified present
            if pak_satisfied(name):
                continue    # same pak UUID provided by another enabled mod
            if filtered_out(mod_id):
                continue    # either/or alternative installed, or external tool
            still_missing.append(name or str(mod_id))
        if still_missing:
            affected.append((e.name, still_missing))

    for name, names in affected:
        log_fn(f"  WARNING: {name} is missing required mod(s): "
               f"{', '.join(names)}")
    if len(affected) == 1:
        name, names = affected[0]
        game.add_deploy_warning(
            f"{name} is missing required mod(s): {', '.join(names)}")
    elif affected:
        game.add_deploy_warning(
            f"{len(affected)} mod(s) have missing requirements: "
            f"{', '.join(name for name, _ in affected)}")


def _log_deploy_context(game, profile: str, profile_dir: Path,
                        deploy_mode: "LinkMode", *, log_fn: LogFn) -> None:
    """Emit a diagnostic header describing the full deploy environment.

    Logged once at the start of every deploy (all games) so a saved log
    contains everything needed to diagnose a failure without re-running:
    app version, game + paths, prefix, staging, deploy mode, profile, mod
    counts, and a same-filesystem check for hardlink viability.
    """
    try:
        from version import __version__ as app_version
    except Exception:
        app_version = "?"

    import platform

    game_root  = _safe(game.get_game_path)
    staging    = _safe(game.get_effective_mod_staging_path)
    filemap    = _safe(game.get_effective_filemap_path)
    data_path  = _safe(game.get_mod_data_path)
    prefix     = _safe(game.get_prefix_path)
    last_dep   = _safe(game.get_last_deployed_profile)
    enabled, seps = _count_enabled_mods(profile_dir)

    log_fn("=" * 60)
    log_fn(f"Deploy: {game.name} — profile '{profile}'")
    log_fn(f"  Mod Manager {app_version} on {platform.system()} "
           f"{platform.release()}")
    log_fn(f"  Deploy mode:   {deploy_mode.name}")
    log_fn(f"  Game path:     {game_root or '(not set)'}")
    if data_path is not None and data_path != game_root:
        log_fn(f"  Mod data dir:  {data_path}")
    if prefix:
        log_fn(f"  Proton prefix: {prefix}")
    log_fn(f"  Staging:       {staging or '(unknown)'}")
    log_fn(f"  Filemap:       {filemap or '(unknown)'}")
    log_fn(f"  Enabled mods:  {enabled}" +
           (f"  ({seps} separator(s))" if seps else ""))
    if last_dep and last_dep != profile:
        log_fn(f"  Last deployed: profile '{last_dep}'")

    # Hardlink viability: compare the filesystem of the deploy destination
    # against the staging folder. Different devices ⇒ hardlinks will fall
    # back to symlink/copy. Warn proactively rather than after-the-fact.
    if deploy_mode is LinkMode.HARDLINK and staging is not None:
        dest = data_path or game_root
        if dest is not None:
            dev_dest = _fs_id(Path(dest))
            dev_stg  = _fs_id(Path(staging))
            if dev_dest is not None and dev_stg is not None and dev_dest != dev_stg:
                log_fn("  WARNING: game and mod staging are on DIFFERENT "
                       "filesystems — hardlinks will fall back to "
                       "symlink/copy (uses extra disk space; symlinks can "
                       "break some games).")

    # Flatpak-sandboxed launchers can't read symlink targets outside their
    # own sandbox — symlinks into host-home staging look broken to the game.
    if deploy_mode is LinkMode.SYMLINK and game_root:
        _app = flatpak_runtime_app(Path(game_root))
        if _app and (staging is None or flatpak_runtime_app(Path(staging)) != _app):
            log_fn(f"  NOTE: game runs inside the {_app} flatpak — sandbox "
                   f"access to the staging/profile folders is granted "
                   f"automatically so symlinked mods resolve. If mods still "
                   f"don't load, restart the launcher or run: flatpak "
                   f"override --user {_app} --filesystem='{staging}'")
    log_fn("=" * 60)


def flatpak_runtime_app(path: Path) -> "str | None":
    """Return the flatpak app id whose sandbox data dir contains *path*."""
    var_app = Path.home() / ".var" / "app"
    try:
        rel = path.relative_to(var_app)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def _make_ue5_conflict_key_fn(game, index_path: Path):
    """Build a (mod_name, rel_key) → ck callback for UE5 conflict detection.

    Uses _resolve_filemap_entries (whole-mod resolve) so include_siblings drag
    is honoured. Per-entry _resolve_entry can't see siblings, which gives the
    wrong destination for companion files like enabled.txt.

    ``index_path`` must point at the ``modindex.bin`` that sits next to the
    filemap being built (NOT next to modlist.txt, which lives in a profile
    subfolder).
    """
    from Utils.filemap import read_mod_index

    cache: dict[str, dict[str, str]] = {}
    index = None

    def _load(mod_name: str) -> dict[str, str]:
        nonlocal index
        if index is None:
            try:
                index = read_mod_index(index_path) or {}
            except Exception:
                index = {}
        entry = index.get(mod_name)
        if not entry:
            return {}
        normal, _ = entry
        # Build (staged_rel, mod_name) pairs from the raw on-disk paths.
        pairs = [(rel_str, mod_name) for _rk, rel_str in normal.items()]
        try:
            resolved = game._resolve_filemap_entries(pairs)
        except Exception:
            return {}
        out: dict[str, str] = {}
        for staged_rel, _mn, dest, final in resolved:
            rk = staged_rel.replace("\\", "/").lower()
            ck = (dest + "/" + final) if dest else final
            out[rk] = ck
        return out

    def _ck(mod_name: str, rel_key: str) -> str:
        m = cache.get(mod_name)
        if m is None:
            m = _load(mod_name)
            cache[mod_name] = m
        ck = m.get(rel_key)
        if ck is not None:
            return ck
        # Fallback to per-entry resolution (rare — entry not in cached mod map).
        dest, final = game._resolve_entry(rel_key)
        return (dest + "/" + final) if dest else final

    return _ck


def _make_custom_routing_conflict_key_fn(game):
    """Build a (mod_name, rel_key) → ck callback from a custom game's routing
    rules, or ``None`` if the game has no rules.

    Custom routing rules decide a file's *actual* deploy destination at deploy
    time; without this, conflict detection keys files by their staged path and
    misses two mods that route to the same game location under different staged
    prefixes (e.g. FF12's ``gamedata/…`` and ``ff12data/gamedata/…`` both
    flatten to ``mods/deploy/ff12data/gamedata/…``). Feeding the effective
    destination back as the conflict key makes build_filemap flag them.
    """
    rules = getattr(game, "custom_routing_rules", None)
    if not rules:
        return None
    from Utils.deploy.deploy_custom_rules import compute_routed_dest, normalise_rules

    norm = normalise_rules(rules)
    strip = {p.lower() for p in (getattr(game, "mod_folder_strip_prefixes", None) or ())}
    # Non-routed files deploy under mod_data_path (standard games) — anchor the
    # verbatim fallback to the same game-root frame as the routed dests. Root
    # and UE5 games have no data path (files land at the game root).
    _defn = getattr(game, "_defn", None)
    data_prefix = (_defn.get("mod_data_path", "") if isinstance(_defn, dict) else "") or ""
    cache: dict[str, str] = {}

    def _ck(_mod_name: str, rel_key: str) -> str:
        ck = cache.get(rel_key)
        if ck is None:
            ck = compute_routed_dest(rel_key, norm, strip, data_prefix)
            cache[rel_key] = ck
        return ck

    return _ck


def _build_filemap_for_game(game, profile, *, log_fn: LogFn,
                            rescan_index: bool = False):
    """Rebuild filemap.txt + filemap_root.txt for *profile* of *game*.

    Mirrors the call in top_bar._run_deploy: pulls excluded-files, root-flagged
    mods (Nexus), folder-case normalization toggle, UE5 conflict-key resolver.
    Errors are logged but not raised — partial filemap is still useful.

    When ``rescan_index`` is True the mod index is fully rescanned from disk
    first (the slow Refresh path) so newly added/removed files inside existing
    mod folders are picked up; otherwise the cached index fast-path is used.

    Returns the build_filemap result tuple
    ``(count, conflict_map, overrides, overridden_by)`` so callers that need the
    conflict data (e.g. the Qt modlist) can use it without re-reading filemap.txt.
    Returns None if the modlist is missing or the build fails.
    """
    profile_root = game.get_profile_root()
    staging = game.get_effective_mod_staging_path()
    modlist_path = profile_root / "profiles" / profile / "modlist.txt"
    filemap_out = staging.parent / "filemap.txt"
    if not modlist_path.is_file():
        return None

    try:
        from Nexus.nexus_meta import collect_root_flagged_mods
        from Games.ue5_game import UE5Game

        from Utils.perftrace import span

        exc_raw = read_excluded_mod_files(modlist_path.parent, None)
        exc = {k: set(v) for k, v in exc_raw.items()} if exc_raw else None
        with span("collect_root_flagged_mods"):
            rf_mods = collect_root_flagged_mods(modlist_path, staging, log_fn=log_fn)

        if rescan_index:
            # Sweep stray Tk-era per-profile indexes. The old Tk install path
            # wrote modindex.bin/bsa_index.bin into the PROFILE folder
            # (profiles/<name>/) even for shared-mods profiles, whose real
            # index lives next to the shared mods/ folder (staging parent) and
            # is valid for every profile sharing it. Those strays are never
            # updated by this codebase, so they only mislead users debugging
            # index staleness ("I have two modindex.bin files"). Only applies
            # when the profile dir is NOT the index home — for
            # profile-specific-mods profiles the two coincide and nothing is
            # ever removed.
            try:
                _prof_dir = modlist_path.parent.resolve()
                if _prof_dir != filemap_out.parent.resolve():
                    for _stray_name in ("modindex.bin", "bsa_index.bin"):
                        _stray = modlist_path.parent / _stray_name
                        if _stray.is_file():
                            _stray.unlink()
                            log_fn(f"Removed stray legacy {_stray_name} from "
                                   f"profile folder ({_stray}) — the real index "
                                   f"lives next to the shared mods folder.")
            except OSError as _sw_err:
                log_fn(f"Stray index sweep warning: {_sw_err}")
            # Heal mods already on disk that carry a non-UTF-8 (legacy Windows
            # code page) file name — rebuild_mod_index would otherwise SKIP the
            # whole mod (no index → no filemap → no conflicts/plugins/deploy).
            # New installs are repaired at extract time; this covers mods
            # installed before that existed, on the user's next Refresh.
            try:
                from Utils.filemap import repair_nonutf8_names
                repair_nonutf8_names(staging, log_fn=log_fn)
            except Exception as _rp_err:
                log_fn(f"Non-UTF-8 name repair warning: {_rp_err}")
            # Full rescan of every mod folder → rewrite modindex.bin from disk
            # (Refresh button). Uses the same game-derived params build_filemap
            # would, so the cached index stays consistent.
            try:
                from Utils.filemap import rebuild_mod_index
                rebuild_mod_index(
                    filemap_out.parent / "modindex.bin", staging,
                    strip_prefixes=set(game.mod_folder_strip_prefixes or ()) or None,
                    per_mod_strip_prefixes=load_per_mod_strip_prefixes(
                        modlist_path.parent),
                    allowed_extensions=set(game.mod_install_extensions or ()) or None,
                    root_folder_mods=set(rf_mods or ()) or None,
                    log_fn=log_fn,
                )
            except Exception as idx_err:
                log_fn(f"Index rescan warning: {idx_err}")
        norm_case = (
            getattr(game, "normalize_folder_case", True)
            and load_normalize_folder_case()
        )
        if isinstance(game, UE5Game):
            conflict_key_fn = _make_ue5_conflict_key_fn(
                game, filemap_out.parent / "modindex.bin",
            )
        else:
            _legacy = getattr(game, "filemap_conflict_key_fn", None)
            if _legacy is not None:
                def conflict_key_fn(_mod: str, rk: str, _f=_legacy) -> str:
                    return _f(rk)
            else:
                # Custom games with routing rules: key conflicts by effective
                # deploy path so mods routing to the same location under
                # different staged prefixes are flagged (e.g. FF12 gamedata/
                # vs ff12data/gamedata/).
                conflict_key_fn = _make_custom_routing_conflict_key_fn(game)

        with span("build_filemap"):
            result = build_filemap(
                modlist_path, staging, filemap_out,
                strip_prefixes=game.mod_folder_strip_prefixes or None,
                per_mod_strip_prefixes=load_per_mod_strip_prefixes(modlist_path.parent),
                allowed_extensions=game.mod_install_extensions or None,
                root_deploy_folders=game.mod_root_deploy_folders or None,
                excluded_mod_files=exc,
                conflict_ignore_filenames=getattr(game, "conflict_ignore_filenames", None) or None,
                conflict_ignore_foldernames=getattr(game, "conflict_ignore_foldernames", None) or None,
                excluded_loose_filenames=getattr(game, "excluded_loose_filenames", None) or None,
                allowed_top_level_folders=(
                    getattr(game, "mod_required_top_level_folders", None) or None
                    if getattr(game, "filemap_exclude_unknown_top_level", False)
                    else None
                ),
                exclude_dirs=getattr(game, "filemap_exclude_dirs", None) or None,
                normalize_folder_case=norm_case,
                filemap_casing=getattr(game, "filemap_casing", "upper"),
                filemap_casing_pins=getattr(game, "filemap_casing_pins", None),
                conflict_key_fn=conflict_key_fn,
                root_folder_mods=rf_mods or None,
            )
        # Game-specific filemap rewrite (e.g. Witcher 3 routes staging paths
        # like TrueFires_v1.01/modTrueFires/… to mods/modTrueFires/… so the
        # Data tab and conflicts match the deployed game-root layout).
        try:
            with span("post_build_filemap"):
                game.post_build_filemap(filemap_out, staging)
        except Exception as pb_err:
            log_fn(f"post_build_filemap warning: {pb_err}")
        return result
    except Exception as fm_err:
        log_fn(f"Filemap rebuild warning: {fm_err}")
        return None


def run_deploy_pipeline(
    game,
    profile: str,
    *,
    log_fn: LogFn,
    progress_fn: Optional[ProgressFn] = None,
    root_folder_enabled: bool = True,
    confirm_cet: Optional[Callable[[], bool]] = None,
    do_backup: bool = True,
    on_pre_filemap: Optional[Callable[[], None]] = None,
) -> bool:
    """Run the standard deploy sequence for *game* / *profile*.

    Parameters
    ----------
    log_fn / progress_fn
        Sinks for human-readable log lines and progress ticks. Callers supply
        thread-safe wrappers when invoked from a worker thread.
    root_folder_enabled
        Honors the Mod List panel's Root_Folder toggle; always True off the GUI.
    confirm_cet
        Optional blocking confirmation prompt (Cyberpunk CET symlink check).
        Return False to abort the deploy. None means "always proceed".
    do_backup
        If True, run `create_backup` for the profile dir before deploy.
    on_pre_filemap
        Optional hook fired *after* the profile switch but *before* the
        filemap rebuild. Used by wizards (e.g. BodySlide output redirect)
        to materialize a placeholder mod that needs to be in the filemap.

    Returns True on success, False on user-cancel / error. The active profile
    is always reset to *profile* before returning, even on error.
    """
    game_root = game.get_game_path()

    mount_err = check_paths_mounted(game)
    if mount_err:
        log_fn(f"Deploy aborted: {mount_err}")
        return False

    import time as _time
    _t_start = _time.perf_counter()

    try:
        from Utils.deploy import deploy_incremental as _incr
        from Utils.deploy.deploy_incremental import IncrementalFallback

        # Restore against the last-deployed profile so runtime files (saves,
        # ShaderCache, etc.) land in *that* profile's overwrite/ folder.
        last_deployed = game.get_last_deployed_profile()
        if last_deployed:
            game.set_active_profile_dir(
                game.get_profile_root() / "profiles" / last_deployed
            )
            # Reload so per-profile path overrides apply to the restore (the
            # last-deployed profile may target a different game folder/prefix).
            game.load_paths()
            game_root = game.get_game_path()

        # Incremental fast path: redeploying the profile that is already
        # deployed with the same link mode → skip the restore and let the
        # standard primitives diff against the previous deploy instead.
        incr_plan = None
        if last_deployed == profile:
            _probe_mode = (
                game.get_deploy_mode()
                if hasattr(game, "get_deploy_mode")
                else LinkMode.HARDLINK
            )
            incr_plan = _incr.plan_incremental(game, profile, _probe_mode,
                                               log_fn=log_fn)
        if incr_plan is not None:
            log_fn("Incremental deploy: existing deployment reused — "
                   "skipping restore.")
            # swap_launcher (end of pipeline) backs up the *current* launcher
            # over <stem>.bak.  Without the full restore that current file is
            # the script-extender copy from the last deploy, which would
            # clobber the vanilla backup.  Undo the swap now; it is re-applied
            # after the deploy as usual.
            if hasattr(game, "_restore_launcher"):
                try:
                    game._restore_launcher(log_fn)
                except Exception as exc:
                    log_fn(f"  WARN: launcher un-swap failed: {exc}")
        elif getattr(game, "restore_before_deploy", True) and hasattr(game, "restore"):
            try:
                if progress_fn is not None:
                    game.restore(log_fn=log_fn, progress_fn=progress_fn)
                else:
                    game.restore(log_fn=log_fn)
            except RuntimeError as restore_err:
                # Expected on first deploy / unconfigured paths; the deploy
                # steps have their own leftover-deploy guards, so continue —
                # but never hide the failure from the log.
                log_fn(f"Restore before deploy failed: {restore_err} — continuing.")
        last_root_folder_dir = game.get_effective_root_folder_path()
        if last_root_folder_dir.is_dir() and game_root:
            restore_root_folder(
                last_root_folder_dir, game_root, log_fn=log_fn,
                data_deploy_dirs=(
                    game.root_restore_protect_dirs()
                    if hasattr(game, "root_restore_protect_dirs") else None
                ),
            )

        # Switch to the target profile before filemap + deploy.
        game.set_active_profile_dir(
            game.get_profile_root() / "profiles" / profile
        )
        # Reload so the deploy uses the target profile's path overrides.
        game.load_paths()
        game_root = game.get_game_path()

        _warn_missing_requirements(
            game, game.get_profile_root() / "profiles" / profile,
            log_fn=log_fn)

        if on_pre_filemap is not None:
            on_pre_filemap()

        _build_filemap_for_game(game, profile, log_fn=log_fn)

        if confirm_cet is not None and not confirm_cet():
            log_fn("Deploy: cancelled — CET requires Hardlink mode.")
            return False

        profile_dir = game.get_profile_root() / "profiles" / profile
        if do_backup:
            try:
                create_backup(profile_dir, log_fn)
            except Exception as backup_err:
                log_fn(f"Backup skipped: {backup_err}")

        deploy_mode = (
            game.get_deploy_mode()
            if hasattr(game, "get_deploy_mode")
            else LinkMode.HARDLINK
        )
        if incr_plan is not None and incr_plan.mode is not deploy_mode:
            # Should be unreachable (the probe read the same config), but the
            # restore was skipped on the strength of that probe — recover.
            log_fn("Incremental deploy: link mode changed after the profile "
                   "switch — running the full path.")
            incr_plan = None
            try:
                if progress_fn is not None:
                    game.restore(log_fn=log_fn, progress_fn=progress_fn)
                else:
                    game.restore(log_fn=log_fn)
            except RuntimeError as restore_err:
                log_fn(f"Restore before deploy failed: {restore_err} — continuing.")
        # Games launched by a flatpak launcher (Heroic flatpak et al.) run in
        # its sandbox and can't follow symlinks whose targets aren't mounted
        # there — grant staging/profile access up front (GH#275).
        try:
            from Utils.wine_proton.flatpak_sandbox import ensure_symlink_target_access
            ensure_symlink_target_access(
                game,
                game_root=Path(game_root) if game_root else None,
                staging=_safe(game.get_effective_mod_staging_path),
                profile_dir=profile_dir,
                log_fn=log_fn,
            )
        except Exception as exc:
            log_fn(f"  WARN: flatpak sandbox access check failed: {exc}")

        _log_deploy_context(game, profile, profile_dir, deploy_mode,
                            log_fn=log_fn)

        def _run_game_deploy():
            if progress_fn is not None:
                game.deploy(log_fn=log_fn, profile=profile,
                            progress_fn=progress_fn, mode=deploy_mode)
            else:
                game.deploy(log_fn=log_fn, profile=profile, mode=deploy_mode)

        # Defer the handler's end-of-deploy game-root snapshot: the pipeline
        # writes it once after the root-folder files land (below), instead of
        # the handler walking the game root now and the pipeline walking it
        # again for the refresh.
        game.begin_deferred_runtime_snapshot()
        try:
            if incr_plan is not None:
                _incr.activate(incr_plan)
                try:
                    _run_game_deploy()
                except IncrementalFallback as fb:
                    _incr.deactivate()
                    incr_plan = None
                    log_fn(f"Incremental deploy fell back to the full path: {fb}")
                    # restore_data_core recovers any partially-diffed Data/,
                    # then the classic full deploy runs.  Same profile, so no
                    # profile switch is needed around the restore.
                    try:
                        if progress_fn is not None:
                            game.restore(log_fn=log_fn, progress_fn=progress_fn)
                        else:
                            game.restore(log_fn=log_fn)
                    except RuntimeError as restore_err:
                        log_fn(f"Restore before deploy failed: {restore_err} "
                               f"— continuing.")
                    _run_game_deploy()
                finally:
                    _incr.deactivate()
            else:
                _run_game_deploy()
        finally:
            snapshot_requested = game.end_deferred_runtime_snapshot()

        pfx = game.get_prefix_path()
        if pfx and pfx.is_dir():
            deploy_game_wine_dll_overrides(
                game.name, pfx, game.wine_dll_overrides, log_fn=log_fn
            )
            # Same "reapply, no-op if already correct" treatment as the DLL
            # overrides just above — a Proton prefix recreated after the game
            # was first configured in Mosaic (a fresh Steam prefix, "Clear
            # local Proton data", a manually deleted compatdata folder)
            # otherwise silently loses vcredist with no prompt to reinstall
            # it, since ensure_prefix_deps used to only run once, at
            # configure-game-save time. Surfaced as RED4ext/Cyber Engine
            # Tweaks crashing deep inside a ~6-year-old msvcp140.dll that
            # Proton's own fresh-prefix defaults ship, with no obvious link
            # back to "reinstall vcredist" — see ensure_prefix_deps's own
            # docstring for the full story.
            try:
                ensure_prefix_deps(game, pfx, log_fn=log_fn)
            except Exception as exc:
                log_fn(f"Prefix dependency check failed: {exc}")

        game.save_last_deployed_profile(profile, deploy_mode=deploy_mode.name)

        target_rf = game.get_effective_root_folder_path()
        rf_allowed = getattr(game, "root_folder_deploy_enabled", True)

        # Step A: shared Root_Folder must run first — its log file is what
        # Step B's root-flagged-mods deploy merges into.
        if rf_allowed and root_folder_enabled and target_rf.is_dir() and game_root:
            count = deploy_root_folder(
                target_rf, game_root, mode=deploy_mode, log_fn=log_fn
            )
            if count:
                log_fn("Root Folder: transferred files to game root.")

        if game_root:
            filemap_root_path = (
                game.get_effective_filemap_path().parent / "filemap_root.txt"
            )
            staging = game.get_effective_mod_staging_path()
            strip = getattr(game, "mod_folder_strip_prefixes", None)
            per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
            rf_count = deploy_root_flagged_mods(
                filemap_root_path, game_root, staging,
                mode=deploy_mode, strip_prefixes=strip,
                per_mod_strip_prefixes=per_mod_strip or None,
                log_fn=log_fn,
            )
            if rf_count:
                log_fn(f"Root-flagged mods: {rf_count} file(s) deployed to game root.")

            snapshot_path = (
                game.get_effective_filemap_path().parent / _FILEMAP_SNAPSHOT_NAME
            )
            # Write the (single) runtime snapshot now that root files landed.
            # `snapshot_requested` covers standard games whose handler call was
            # deferred above; the is_file() check keeps the refresh for games
            # that write the snapshot directly inside deploy (Witcher 3, UE5,
            # game-root mode) exactly as before.
            if snapshot_requested or snapshot_path.is_file():
                try:
                    game.snapshot_root_for_runtime_capture(log_fn=log_fn)
                except Exception as exc:
                    log_fn(f"WARN: could not refresh deploy snapshot: {exc}")

        # Launcher swap last so SE/SKSE/etc. dlls are present first.
        if hasattr(game, "swap_launcher"):
            game.swap_launcher(log_fn)

        _tag = " (incremental)" if incr_plan is not None else ""
        log_fn(f"Deploy finished OK in {_time.perf_counter() - _t_start:.1f}s "
               f"— profile '{profile}'.{_tag}")
        return True
    except Exception as e:
        log_fn(f"Deploy FAILED after {_time.perf_counter() - _t_start:.1f}s: "
               f"{e}\n{traceback.format_exc()}")
        return False
    finally:
        game.set_active_profile_dir(
            game.get_profile_root() / "profiles" / profile
        )
        game.load_paths()

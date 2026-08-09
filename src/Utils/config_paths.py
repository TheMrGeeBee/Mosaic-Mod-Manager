"""
config_paths.py
Central helpers for resolving user-writable config directories.

Follows the XDG Base Directory Specification:
  Config lives in $XDG_CONFIG_HOME/MosaicModManager  (default: ~/.config/MosaicModManager)

This is required for AppImage packaging — the AppImage mount is read-only,
so all user config must be written outside the app bundle.
"""

import os
import shutil
from pathlib import Path

APP_NAME = "MosaicModManager"
# Pre-rename config dir name (the app was "Amethyst Mod Manager" until the
# 2026-07-29 rename). get_config_dir() migrates it once on first launch
# under the new name — see the migration note there.
_LEGACY_APP_NAME = "AmethystModManager"


def get_config_dir() -> Path:
    """Return the app config directory, creating it if it doesn't exist.

    Respects $XDG_CONFIG_HOME; falls back to ~/.config/MosaicModManager.

    One-time migration: this app is a rename of Amethyst Mod Manager, not a
    fresh product, so an existing ``~/.config/AmethystModManager`` (games,
    profiles, downloads, logs, ...) is moved wholesale to the new path the
    first time it's found, rather than leaving Mosaic pointed at an empty
    directory. A failed move (e.g. cross-device, permissions) just falls
    through to creating a fresh dir; migration is retried on the next launch
    since the legacy dir is left in place until the move actually succeeds.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    config_dir = base / APP_NAME
    if not config_dir.exists():
        legacy_dir = base / _LEGACY_APP_NAME
        if legacy_dir.is_dir():
            try:
                shutil.move(str(legacy_dir), str(config_dir))
            except OSError:
                pass
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_game_config_path(game_name: str) -> Path:
    """Return the paths.json path for a given game, creating parent dirs as needed.

    Result: ~/.config/MosaicModManager/games/<game_name>/paths.json
    """
    path = get_config_dir() / "games" / game_name / "paths.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_game_config_dir(game_name: str) -> Path:
    """Return the config directory for a given game, creating it if needed.

    Result: ~/.config/MosaicModManager/games/<game_name>/
    """
    d = get_config_dir() / "games" / game_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_loot_data_dir() -> Path:
    """Return the LOOT masterlist data directory, creating it if needed.

    Result: ~/.config/MosaicModManager/LOOT/data/
    """
    d = get_config_dir() / "LOOT" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_loot_game_dir(game_id: str) -> Path:
    """Return the per-game LOOT directory, creating it if needed.

    Result: ~/.config/MosaicModManager/LOOT/<game_id>/
    A global userlist.yaml placed here applies to all profiles of this game.
    """
    d = get_config_dir() / "LOOT" / game_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_default_staging_root() -> Path:
    """Return the built-in default mod-staging root: ``~/Games/Mosaic``.

    Mod staging must live on the *same filesystem* as the game install so that
    deployed files can be hardlinked.  Under Flatpak the app's config dir lives
    in ``~/.var/app/<id>/config`` — a separate namespace mount from the games
    exposed via ``--filesystem=home`` — so hardlinking staged files into the
    game silently falls back to symlink/copy (``os.link`` → ``EXDEV``).  Placing
    the default staging in the user's real home (``~/Games/Mosaic``) keeps it
    on the ``--filesystem=home`` mount alongside the games, so hardlinks work in
    the Flatpak, AppImage and native installs alike.

    This is only the suggested default for *new* installs / newly-added games
    (see get_default_game_staging_root below) — existing installs already
    have an explicit ``_staging_path`` stored in their game config, which is
    never re-derived from this constant, so a pre-rename install's mods stay
    exactly where they are (``~/Games/Amethyst/...``) after upgrading.
    """
    return Path.home() / "Games" / "Mosaic"


def get_default_game_staging_root(game_name: str) -> Path:
    """Return the preferred staging *root* for a newly-added game.

    ``~/Games/Mosaic/<game>`` — the per-game folder that holds ``mods/``,
    ``profiles/``, ``overwrite/`` and ``filemap.txt`` (i.e. what the backend
    stores as a game's custom ``_staging_path``).  Used by the add-game / reset
    UI to seed the staging field so new games land beside the game installs on
    the same filesystem (hardlink-friendly) — including on *existing* installs,
    whose already-staged games keep resolving via get_profiles_dir() below.
    """
    return get_default_staging_root() / game_name


def get_profiles_dir() -> Path:
    """Return the legacy root Profiles directory for default-staged games.

    Resolution order:
      1. $MOD_MANAGER_PROFILES_DIR — explicit override (set by AppImage AppRun).
      2. Legacy <config>/Profiles — if it already exists, existing users keep it
         so upgrades never relocate an in-use staging tree.  Games with an empty
         (default) staging_path resolve their mods dir from here, so this MUST
         stay stable for existing installs.
      3. Fresh install → ~/Games/Mosaic/Profiles.  Unlike the config dir (which
         Flatpak redirects into ~/.var/app), the user's home is exposed on the
         same mount as the game installs, so deployed files can be hardlinked
         instead of falling back to symlink/copy.

    NB: new games no longer default here — the add-game UI seeds a per-game
    custom root via get_default_game_staging_root() so their layout is
    ~/Games/Mosaic/<game>/mods (no Profiles/ segment).  This function only
    resolves games that already carry an empty staging_path.

    The fresh-install path is returned WITHOUT being created: this is called at
    import time by every game handler (and re-run by discover_games on each
    add-game reload), so an eager mkdir here dropped ~/Games/Mosaic into the
    user's home on first startup and after every game add — even when nothing
    ever stages there.  Anything that actually uses the path creates it with
    mkdir(parents=True) at that point.
    """
    env = os.environ.get("MOD_MANAGER_PROFILES_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    legacy = get_config_dir() / "Profiles"
    if legacy.exists():
        return legacy
    return get_default_staging_root() / "Profiles"


def get_exe_args_path() -> Path:
    """Return the path to exe_args.json in the config directory.

    Result: ~/.config/MosaicModManager/exe_args.json
    """
    return get_config_dir() / "exe_args.json"


def get_profile_exe_args_path(profile_dir: Path) -> Path:
    """Return the per-profile exe_args.json path inside a profile directory.

    Result: <profile_dir>/exe_args.json
    """
    return profile_dir / "exe_args.json"


def get_fomod_selections_path(game_name: str, mod_name: str) -> Path:
    """Return the path to a saved FOMOD selection file for a given game and mod.

    Result: ~/.config/MosaicModManager/games/<game_name>/fomod_selections/<mod_name>.json
    """
    path = get_config_dir() / "games" / game_name / "fomod_selections" / f"{mod_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_bain_selections_path(game_name: str, mod_name: str) -> Path:
    """Return the path to a saved BAIN selection file for a given game and mod.

    Result: ~/.config/MosaicModManager/games/<game_name>/bain_selections/<mod_name>.json
    """
    path = get_config_dir() / "games" / game_name / "bain_selections" / f"{mod_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_nexus_config_dir() -> Path:
    """Return the Nexus Mods config directory, creating it if needed.

    Result: ~/.config/MosaicModManager/Nexus/
    """
    d = get_config_dir() / "Nexus"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_last_game_path() -> Path:
    """Return the path to the last-opened game state file.

    Result: ~/.config/MosaicModManager/last_game.json
    """
    return get_config_dir() / "last_game.json"


def get_logs_dir() -> Path:
    """Return the logs directory, creating it if it doesn't exist.

    Result: ~/.config/MosaicModManager/logs/
    """
    d = get_config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_requirement_external_tool_mod_ids_path() -> Path:
    """Return the path to the cached requirement filter (external tool mod IDs).

    Fetched from GitHub and merged with user additions. Users can edit this file
    to add mod IDs; new IDs from the remote are appended on the next fetch.

    Result: ~/.config/MosaicModManager/requirement_external_tool_mod_ids.txt
    """
    return get_config_dir() / "requirement_external_tool_mod_ids.txt"


def get_custom_games_dir() -> Path:
    """Return the directory where user-defined custom game JSON files are stored.

    Users drop one JSON file per game here to add support for games not built
    into the application.

    Result: ~/.config/MosaicModManager/custom_games/
    """
    d = get_config_dir() / "custom_games"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_vcredist_cache_path() -> Path:
    """Return the path where the VC++ Redistributable installer is cached.

    Result: ~/.config/MosaicModManager/vcredist/vc_redist.x64.exe
    """
    path = get_config_dir() / "vcredist" / "vc_redist.x64.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_dotnet_cache_dir() -> Path:
    """Return the directory where .NET runtime installers are cached.

    Result: ~/.config/MosaicModManager/dotnet/
    """
    path = get_config_dir() / "dotnet"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_custom_game_images_dir() -> Path:
    """Return the directory where downloaded custom game banner images are cached.

    When a user provides an image URL in the custom game definition, the image
    is downloaded once and stored here so the game picker can display it offline.

    Result: ~/.config/MosaicModManager/custom_game_images/
    """
    d = get_config_dir() / "custom_game_images"
    d.mkdir(parents=True, exist_ok=True)
    return d


_CACHE_ROOT_RESERVED: set[str] = set()


def get_wine_prefixes_dir() -> Path:
    """Return the directory holding Wine prefixes for VRAMr/Bendr/ParallaxR.

    Lives in the app config (not the download cache) so "Clear Cache" never
    blows it away and a user-relocated download cache stays archive-only.

    Result: ~/.config/MosaicModManager/wine_prefixes/

    Migrates an existing ``<download_cache>/wine_prefixes`` directory on first
    access — best-effort: if migration fails the old location is left alone.
    """
    d = get_config_dir() / "wine_prefixes"
    if not d.exists():
        legacy = get_config_dir() / "download_cache" / "wine_prefixes"
        try:
            from Utils.ui_config import load_download_cache_path  # lazy: avoid cycles
            custom = load_download_cache_path().strip()
        except Exception:
            custom = ""
        if custom:
            try:
                legacy_custom = Path(custom).expanduser() / "wine_prefixes"
                if legacy_custom.is_dir():
                    legacy = legacy_custom
            except Exception:
                pass
        if legacy.is_dir():
            try:
                legacy.rename(d)
            except OSError:
                pass
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_download_cache_dir() -> Path:
    """Return the download cache root directory, creating it if it doesn't exist.

    Honours the user-configured path from ``[paths] download_cache_path`` in
    mosaic.ini.  When unset (or unwritable) falls back to
    ``~/.config/MosaicModManager/download_cache/``.

    The setting is read on every call so a path change in the Settings panel
    takes effect without restarting.
    """
    try:
        from Utils.ui_config import load_download_cache_path  # lazy: avoid cycles
        custom = load_download_cache_path().strip()
    except Exception:
        custom = ""
    if custom:
        d = Path(custom).expanduser()
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            pass  # fall through to default
    d = get_config_dir() / "download_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_download_cache_dir_for_game(game_name: str | None) -> Path:
    """Per-game cache subfolder under :func:`get_download_cache_dir`.

    Falls back to the cache root when *game_name* is empty.  Game name is
    used as a directory component verbatim, matching the convention used
    elsewhere (e.g. :func:`get_game_config_path`).
    """
    root = get_download_cache_dir()
    if not game_name:
        return root
    d = root / game_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_all_cache_dirs(active_game_name: str | None = None) -> list[Path]:
    """Cache directories to scan for already-downloaded archives.

    Returns ``[active-game folder, cache root]``, de-duplicated by resolved
    path.  Other games' subfolders are intentionally NOT included: file_id
    is not unique across mods/games, so cross-game scans risk matching an
    unrelated archive whose ``.fileid`` sidecar happens to share a value
    with the requested file (e.g. Darktide mod 373 file 2964 colliding with
    Palworld mod 678 file 2964).  The cache root is still included so
    legacy archives placed there before per-game subdirs existed remain
    discoverable.
    """
    root = get_download_cache_dir()
    out: list[Path] = []
    seen: set[Path] = set()
    if active_game_name:
        active = root / active_game_name
        if active.is_dir():
            out.append(active)
            seen.add(active.resolve())
    root_resolved = root.resolve()
    if root_resolved not in seen:
        out.append(root)
        seen.add(root_resolved)
    return out


def get_plugins_dir() -> Path:
    """Return the directory where external wizard plugin scripts are stored.

    Users drop Python scripts here to add custom wizard tools without modifying
    the application source.

    Result: ~/.config/MosaicModManager/Plugins/
    """
    d = get_config_dir() / "Plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_languages_dir() -> Path:
    """Return the directory where downloaded / user-added UI translations live.

    Compiled Qt translation files (``mosaic_<code>.qm``, or the pre-rename
    ``amethyst_<code>.qm`` — both are recognized, see gui_qt.i18n) are stored
    here. They are synced from the Resources branch on startup (see
    Utils.gh_sync) and users can drop their own ``.qm`` in to add a language
    without an app update. The built-in source-tree English is always
    available regardless of this folder.

    Result: ~/.config/MosaicModManager/languages/
    """
    d = get_config_dir() / "languages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_instance_lock_path() -> Path:
    """Return the path to the single-instance advisory lock file.

    Result: ~/.config/MosaicModManager/mosaic.lock

    Not created here — get_config_dir() already guarantees the parent
    directory exists; the lock file itself is opened with O_CREAT on
    first acquire (see Utils.instance_lock).
    """
    return get_config_dir() / "mosaic.lock"


def get_download_locations_path() -> Path:
    """Return the path to the extra download scan locations config file.

    Users can add custom folders to scan for archives in addition to ~/Downloads.
    Stored as JSON array of path strings.

    Result: ~/.config/MosaicModManager/download_locations.json
    """
    return get_config_dir() / "download_locations.json"

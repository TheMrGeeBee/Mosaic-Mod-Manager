"""Whether Steam Cloud keeps a game's saves in sync -- which defeats per-profile
saves.

Each launch, Steam downloads a game's cloud saves into its save folder; with
per-profile saves that folder is the profile's own, so every profile collects
every save (real case, 2026-11-25: Fallout New Vegas, 17 cloud saves copied into
a new profile six seconds before the game started). MO2's profile saves have the
same conflict.

Steam keeps, per user, under ``<Steam>/userdata/<id>/``:
  * ``<appid>/remotecache.vdf`` -- the files it syncs for the game, keyed by
    path (``My Games/FalloutNV/Saves/autosave.fos``);
  * ``7/remote/sharedconfig.vdf`` -- Settings ▸ Cloud's global switch, at
    ``UserRoamingConfigStore/Software/Valve/Steam/CloudEnabled`` ("0" = off;
    verified 2026-11-25 by turning it off).
A per-game switch (Properties ▸ General) may sit under ``.../Steam/apps/<appid>``
as ``CloudEnabled`` too; that location is unverified, so it is honoured when
present but never relied on.
"""
from __future__ import annotations

from pathlib import Path


def _user_dirs(steam_roots: "list[Path]") -> "list[Path]":
    found = []
    for root in steam_roots:
        try:
            found.extend(p for p in Path(root).glob("userdata/*") if p.is_dir())
        except OSError:
            continue
    return sorted(set(found))


def _cloud_off(user_dir: Path, app: str) -> bool:
    from Utils.steam.acf import VdfError, get_ci, read_vdf
    try:
        data = read_vdf(user_dir / "7" / "remote" / "sharedconfig.vdf")
    except (OSError, VdfError):
        return False
    steam = get_ci(data, "UserRoamingConfigStore", "Software", "Valve", "Steam")
    if not isinstance(steam, dict):
        return False
    if str(get_ci(steam, "CloudEnabled", default="")).strip() == "0":
        return True
    entry = get_ci(steam, "apps", app)
    return (isinstance(entry, dict)
            and str(get_ci(entry, "CloudEnabled", default="")).strip() == "0")


def cloud_save_count(app_id: "str | int",
                     steam_roots: "list[Path] | None" = None) -> int:
    """How many save files Steam Cloud syncs for *app_id*, for the Steam users
    who have cloud saves on (the most any one of them has). 0 when none."""
    from Utils.steam.acf import VdfError, get_ci, read_vdf
    if steam_roots is None:
        from Utils.steam.depot_versions import steam_roots as _roots
        steam_roots = _roots()
    app = str(app_id).strip()
    if not app:
        return 0
    most = 0
    for user in _user_dirs(steam_roots):
        try:
            data = read_vdf(user / app / "remotecache.vdf")
        except (OSError, VdfError):
            continue
        files = get_ci(data, app)
        if not isinstance(files, dict):
            continue
        saves = sum(1 for k, v in files.items()
                    if isinstance(v, dict) and "/saves/" in "/" + k.replace("\\", "/").lower())
        if saves and not _cloud_off(user, app):
            most = max(most, saves)
    return most


def cloud_saves_warning(game) -> "str | None":
    """What to tell the user when *game* keeps per-profile saves but Steam Cloud
    syncs its saves, or None."""
    try:
        app = getattr(game, "steam_id", "") or ""
        n = cloud_save_count(app) if app else 0
    except Exception:
        return None
    if not n:
        return None
    return (f"Steam Cloud syncs this game's saves ({n} in the cloud), so Steam "
            "copies every save into each profile when the game starts. Turn it "
            "off in Steam: the game's Properties ▸ General ▸ \"Keep game saves "
            "in the Steam Cloud\", or Settings ▸ Cloud for every game.")

"""The launch options a Steam user has set for a game (Properties → Launch Options).

Steam keeps them per user in ``<Steam>/userdata/<id>/config/localconfig.vdf``,
under ``UserLocalConfigStore/Software/Valve/Steam/apps/<appid>/LaunchOptions``.
Mosaic needs them when it starts a game itself instead of through Steam -- a
profile running on its own game copy -- so the options the user set in Steam
(env vars, wrappers like mangohud, game arguments) still apply.
"""
from __future__ import annotations

from pathlib import Path


def _localconfigs(steam_roots: "list[Path]") -> "list[Path]":
    found = []
    for root in steam_roots:
        try:
            found.extend(Path(root).glob("userdata/*/config/localconfig.vdf"))
        except OSError:
            continue
    # newest first: the account that used Steam last is the one playing
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(set(found), key=mtime, reverse=True)


def steam_launch_options(app_id: "str | int",
                         steam_roots: "list[Path] | None" = None) -> "str | None":
    """The launch options set in Steam for *app_id*: ``""`` when Steam has the
    game but no options, ``None`` when no Steam config could be read or none of
    the accounts knows the game."""
    from Utils.steam.acf import VdfError, get_ci, read_vdf
    if steam_roots is None:
        from Utils.steam.depot_versions import steam_roots as _roots
        steam_roots = _roots()
    app = str(app_id)
    for cfg in _localconfigs(steam_roots):
        try:
            data = read_vdf(cfg)
        except (OSError, VdfError):
            continue
        apps = get_ci(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps")
        if not isinstance(apps, dict):
            continue
        entry = get_ci(apps, app)
        if not isinstance(entry, dict):
            continue
        opts = get_ci(entry, "LaunchOptions", default="")
        return opts.strip() if isinstance(opts, str) else ""
    return None

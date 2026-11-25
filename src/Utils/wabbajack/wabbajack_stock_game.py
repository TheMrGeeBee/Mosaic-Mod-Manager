"""A Wabbajack list's own game folder ("Stock Game"), built as a Mosaic game copy.

MO2 lists run from a private copy of the game next to the list (LoreRim:
``Stock Game/``, the VtMB list: ``StockGame/``) instead of the Steam install.
It isn't a plain copy: LoreRim's holds 33 files, including a patched
``SkyrimSE.exe`` (1.7.104 -> 1.6.1170, the version its SKSE needs), cleaned
master ESMs and rebuilt vanilla BSAs, and no Textures BSAs or Creation Club
content (its mods supply those). For a game that supports game copies
(``BaseGame.supports_game_copy``), Mosaic builds exactly that folder from the
list's directives, next to the list like MO2 (``<Modlists>/<list>/Stock
Game``), and points every profile the install creates at it (see
``Utils.game_copy``). Pure logic, no Qt.
"""
from __future__ import annotations

import re
from pathlib import Path

from .wabbajack_manifest import ModList


def stock_game_dir(modlist: ModList, exe_name: "str | None") -> "str | None":
    """The top-level folder holding the list's own copy of the game: the one
    with the game's exe directly in it. ``None`` when the list has none."""
    if not exe_name:
        return None
    exe = exe_name.replace("\\", "/").split("/")[-1].lower()
    for d in modlist.directives:
        parts = [p for p in (getattr(d, "to", "") or "").replace("\\", "/").split("/") if p]
        if (len(parts) == 2 and parts[1].lower() == exe
                and parts[0].lower() not in ("mods", "profiles")):
            return parts[0]
    return None


def game_stock_dir(modlist: ModList, game) -> "str | None":
    """:func:`stock_game_dir` for ``game``: by the exe that names its build
    (``SkyrimSE.exe``), then by the exe Mosaic launches."""
    from Utils import game_copy as gc
    for exe in (gc.version_exe(game), getattr(game, "exe_name", None)):
        found = stock_game_dir(modlist, exe)
        if found:
            return found
    return None


def partial_stock_dirs(modlist: ModList, game) -> "dict[str, str]":
    """A list's game folder that ships only the files the list adds to the
    game -- no game exe, the user copies their game in by hand -- mapped to the
    name of the root-folder mod Mosaic installs it as. Found by the game's
    script extender loader directly in it. Real case: Nuclear Sunset's
    ``[NoDelete] Stock New Vegas`` (xNVSE, 4GB patchers, DXVK).
    Empty when the list has none, or a full game copy :func:`game_stock_dir`
    finds."""
    loader = getattr(game, "_script_extender_exe", None)
    if not isinstance(loader, str) or not loader or game_stock_dir(modlist, game):
        return {}
    loader = loader.lower()
    # A full copy has the game's own master (Data/FalloutNV.esm); a list may
    # name the copy by an exe Mosaic doesn't look for (New Vegas: Mosaic
    # launches FalloutNVLauncher.exe, the copy holds FalloutNV.exe).
    masters = [m.lower() for m in getattr(game, "vanilla_plugins", None) or []][:1]
    found: "dict[str, str]" = {}
    full: "set[str]" = set()
    for d in modlist.directives:
        parts = [p for p in (getattr(d, "to", "") or "").replace("\\", "/").split("/") if p]
        if (len(parts) == 3 and masters and parts[1].lower() == "data"
                and parts[2].lower() == masters[0]):
            full.add(parts[0])
        if (len(parts) == 2 and parts[1].lower() == loader
                and parts[0].lower() not in ("mods", "profiles")):
            name = re.sub(r"^(\s*\[[^\]]*\])+\s*", "", parts[0]).strip() or parts[0]
            found[parts[0]] = name
    return {k: v for k, v in found.items() if k not in full}


def copy_label(modlist: ModList) -> str:
    """What a copy built from this list version records as its build."""
    return f"Wabbajack: {modlist.name} {modlist.version}".strip()


def stock_game_destination(game, modlist: ModList) -> Path:
    """Where Mosaic builds the list's game copy: ``<Modlists>/<list>/Stock Game``.

    ``<Modlists>`` is where the last game copy's modlist folder went, else
    ``Modlists`` next to the Steam library (same filesystem as the game, so
    game files can be cloned). A folder there that Mosaic didn't make (e.g. the
    list's own MO2 install, which uses the same name) is never touched: the
    copy then goes to ``<list> (Mosaic)`` instead. A copy Mosaic made for this
    list before is reused or updated in place."""
    from Utils import game_copy as gc
    from Utils.wine_proton.steam_finder import library_steamapps_for
    parent = gc.last_parent(game.name)
    if parent is None:
        lib = library_steamapps_for(game.get_steam_install_path())
        parent = (lib.parent / "Modlists") if lib is not None else Path.home() / "Games" / "Modlists"
    name = (modlist.name or "Modlist").strip()
    for folder in (name, f"{name} (Mosaic)", f"{name} (Mosaic 2)"):
        dest = gc.default_destination(parent, folder)
        if not dest.exists() or gc.read_marker(dest) is not None or not any(dest.iterdir()):
            return dest
    return gc.default_destination(parent, f"{name} (Mosaic 3)")

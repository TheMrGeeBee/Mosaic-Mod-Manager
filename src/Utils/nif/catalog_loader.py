"""Build an AssetCatalog from Mosaic's on-disk state for the active game/profile.

Reads the same indexes the modlist and Mod Files tabs use (modindex.bin,
bsa_index.bin, filemap.txt, modlist.txt, loadorder.txt) — nothing is scanned
from disk except the base game's archive names.
"""

from __future__ import annotations

import os
from pathlib import Path

from Utils.nif.asset_catalog import AssetCatalog, is_viewable, norm_key
from Utils.nif.nif_reader import SKYRIM_SE_FORMAT

# Games whose meshes the NIF reader understands (Skyrim SE: NIF 20.2.0.7, BS 100).
NIF_VIEWER_GAME_IDS = frozenset({"skyrim_se"})

# The NIF (version, BS version) each viewer game's meshes are expected to have.
EXPECTED_NIF_FORMAT = {"skyrim_se": SKYRIM_SE_FORMAT}


def vanilla_archives(game, data_dir: "Path | None", mod_archives: "set[str]",
                     staging_dir: "Path | None" = None) -> list[Path]:
    """The game's own BSAs in data_dir, lowest priority first.

    The engine pairs a BSA with a plugin when its name is ``<plugin>.bsa`` or
    ``<plugin> - <suffix>.bsa`` (``Skyrim - Meshes0.bsa`` → Skyrim.esm,
    ``Update.bsa`` → Update.esm) and loads them in plugin order: the game's own
    vanilla list, then the Creation Club plugins named in its .ccc file.

    Mosaic keeps the pristine game files in ``<Data>_Core`` and fills ``Data``
    with links to them and to the deployed mods, so ``Data_Core`` is the source
    of truth when it exists. Otherwise ``Data`` is scanned, and never counted as
    base game are archives a mod ships (``mod_archives``, lowercase names) and
    anything that resolves into the mod staging area (a symlink or hardlink
    deployed from a mod)."""
    if data_dir is None or not data_dir.is_dir():
        return []
    core = data_dir.parent / (data_dir.name + "_Core")
    if core.is_dir():
        data_dir, staging_dir, mod_archives = core, None, set()
    stems = [Path(p).stem.lower() for p in getattr(game, "vanilla_plugins", [])]
    ccc = getattr(game, "vanilla_ccc_filename", "") or ""
    for base in (data_dir, data_dir.parent):
        f = base / ccc if ccc else None
        if f is not None and f.is_file():
            try:
                stems += [Path(ln.strip()).stem.lower()
                          for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()
                          if ln.strip()]
            except OSError:
                pass
            break
    found: list[tuple[int, str, Path]] = []
    try:
        names = os.listdir(data_dir)
    except OSError:
        return []
    for name in names:
        low = name.lower()
        if not low.endswith(".bsa") or low in mod_archives:
            continue
        rank = next((i for i, stem in enumerate(stems)
                     if low == stem + ".bsa" or low.startswith(stem + " - ")), None)
        if rank is None:
            continue
        path = data_dir / name
        try:
            st = path.lstat()
        except OSError:
            continue
        if staging_dir is not None:
            try:
                if st.st_nlink > 1 or path.resolve().is_relative_to(staging_dir.resolve()):
                    continue                          # deployed from a mod
            except (OSError, ValueError):
                continue
        found.append((rank, low, path))
    found.sort(key=lambda t: (t[0], t[1]))
    return [p for _r, _n, p in found]


def build_catalog(game, profile_dir: "Path | None", staging_dir: "Path | None") -> AssetCatalog:
    """Catalog for *game* with the mods of the active profile.

    Mod order is the profile's modlist, enabled mods only, LOWEST priority
    first (the modlist file lists highest priority first)."""
    from Utils.archives.bsa_filemap import compute_bsa_winner_map, read_bsa_index
    from Utils.filemap import read_mod_index
    from Utils.mods import mod_files as mflogic
    from Utils.mods.modlist import read_modlist

    enabled: list[str] = []
    if profile_dir is not None and (profile_dir / "modlist.txt").is_file():
        enabled = [e.name for e in read_modlist(profile_dir / "modlist.txt")
                   if not e.is_separator and e.enabled]
    mod_order = list(reversed(enabled))               # → lowest priority first

    index_path = staging_dir.parent / "modindex.bin" if staging_dir is not None else None
    bsa_index_path = staging_dir.parent / "bsa_index.bin" if staging_dir is not None else None
    full_index = (read_mod_index(index_path) if index_path is not None else None) or {}
    bsa_index = (read_bsa_index(bsa_index_path) if bsa_index_path is not None else None) or {}

    loose: dict[str, dict[str, str]] = {}
    for m in mod_order:
        entry = full_index.get(m)
        if entry:
            files = {k: rs for k, rs in entry[0].items() if is_viewable(k)}
            if files:
                loose[m] = files
    bsas: dict[str, list[tuple[str, list[str]]]] = {}
    mod_archive_names: set[str] = set()
    for m, archives in bsa_index.items():
        for arch, _mt, _paths in archives:
            mod_archive_names.add(arch.lower())
        if m not in mod_order:
            continue
        keep = []
        for arch, _mt, paths in archives:
            v = [norm_key(p) for p in paths if is_viewable(norm_key(p))]
            if v:
                keep.append((arch, v))
        if keep:
            bsas[m] = keep

    loose_winner: dict[str, str] = {}
    if index_path is not None:
        _contested, fm = mflogic.build_conflict_cache(index_path, profile_dir, full_index)
        loose_winner = dict(fm)

    bsa_winner: dict[str, str] = {}
    if bsa_index and mod_order:
        plugin_order = plugin_exts = None
        if getattr(game, "archive_plugin_ordering", True) and profile_dir is not None:
            from Utils.plugins.plugins import read_loadorder
            plugin_order = read_loadorder(profile_dir / "loadorder.txt")
            plugin_exts = frozenset(getattr(game, "plugin_extensions", []) or [])
        win, _lose = compute_bsa_winner_map(
            bsa_index, mod_order, plugin_order or None, plugin_exts or None,
            index_path, False)
        for p, m in win.items():
            k = norm_key(p)
            if is_viewable(k):
                bsa_winner[k] = m

    data_dir = None
    try:
        data_dir = game.get_mod_data_path()
    except Exception:
        pass

    authoritative_slots: dict = {}
    if getattr(game, "game_id", None) in EXPECTED_NIF_FORMAT:
        try:
            from Utils.plugins.armor_records import build_slot_index
            authoritative_slots = build_slot_index(game, profile_dir)
        except Exception:
            pass                                   # best-effort: never fail a catalog build over plugin data

    return AssetCatalog(
        base_name=getattr(game, "name", "Game"),
        base_archives=vanilla_archives(game, data_dir, mod_archive_names, staging_dir),
        mod_order=mod_order, loose=loose, bsas=bsas,
        loose_winner=loose_winner, bsa_winner=bsa_winner,
        mod_dir_for=lambda m: mflogic._mod_dir_for(game, m),
        strips_for=lambda m: mflogic.read_strip_prefixes(profile_dir, m),
        expected_nif_format=EXPECTED_NIF_FORMAT.get(getattr(game, "game_id", None)),
        authoritative_slots=authoritative_slots)

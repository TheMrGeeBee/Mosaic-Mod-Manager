"""
armor_records.py
Authoritative worn-armor slot data straight from ARMA (Armor Addon) records,
to replace the NIF Viewer/Character tab's mesh-partition-derived slot guess
wherever a real plugin actually declares it.

An ARMA's BOD2/BODT slot bitmask is the exact same 30-61 numbering the mesh's
own NiSkinPartition dismember slots use (Bethesda aligned the two on purpose),
so no translation table is needed — see `Utils/nif/character.py`'s GROUPS.
Verified against Skyrim.esm: IronCuirassAA -> {32, 34, 38}, IronBootsAA ->
{37, 38}, IronHelmetAA -> {31, 43} (helmet occupies the hair+ears slots, not
the head slot — real engine behaviour, not a Mosaic guess).
"""
from __future__ import annotations

import struct
from pathlib import Path

from Utils.plugins.esp_records import Record, read_records

# ARMA's four gender/person-view model subrecords, in field order.
_MODEL_SUBS = ("MOD2", "MOD3", "MOD4", "MOD5")


def _normalize_mesh_path(p: str) -> str:
    """Lowercase, forward slashes, rooted at ``meshes/`` ("" stays "") — mirrors
    `nif_reader.normalize_texture_path` but rooted at meshes, not textures."""
    p = p.replace("\\", "/").strip().lower()
    while p.startswith("/"):
        p = p[1:]
    if not p:
        return ""
    return p if p.startswith("meshes/") else "meshes/" + p


def _slot_mask(rec: Record) -> "int | None":
    """The BOD2 (8 bytes: mask, skill) or BODT (8 or 12 bytes: mask, ...)
    slot bitmask, whichever subrecord is present. None if malformed/missing."""
    blob = rec.sub("BOD2") or rec.sub("BODT")
    if not blob or len(blob) < 4:
        return None
    return struct.unpack_from("<I", blob, 0)[0]


def _slots_from_mask(mask: int) -> frozenset:
    return frozenset(30 + i for i in range(32) if mask & (1 << i))


def parse_arma(rec: Record) -> "tuple[frozenset, list[str]] | None":
    """(slots, model_paths) for one ARMA record, or None if it has neither a
    readable slot mask nor any model path (nothing usable)."""
    mask = _slot_mask(rec)
    if mask is None:
        return None
    slots = _slots_from_mask(mask)
    if not slots:
        return None
    paths = []
    for sig in _MODEL_SUBS:
        raw = rec.sub(sig)
        if raw:
            path = _normalize_mesh_path(raw.rstrip(b"\x00").decode("utf-8", errors="replace"))
            if path:
                paths.append(path)
    return (slots, paths) if paths else None


def active_plugin_paths(game, profile_dir: "Path | None") -> list:
    """Enabled plugins in load order (lowest priority first), resolved to an
    on-disk path — mirrors `catalog_loader.build_catalog`'s own mod/BSA
    resolution so this index sees exactly the same load order it does.

    Resolution uses modindex.bin (already built and cached for the Mod Files
    tab), a single {plugin_name.lower(): (mod, rel_path)} pass over the active
    modlist — not a per-plugin filesystem scan, which over a large profile
    (thousands of mods x thousands of plugins) would be quadratic."""
    if profile_dir is None:
        return []
    from Utils.filemap import read_mod_index
    from Utils.mods.mod_files import _mod_dir_for
    from Utils.mods.modlist import read_modlist
    from Utils.plugins.plugins import read_loadorder, read_plugins

    pl_path = profile_dir / "plugins.txt"
    if not pl_path.is_file():
        return []
    enabled = {e.name for e in read_plugins(pl_path) if e.enabled}

    # Games where plugins_include_vanilla is False (Fallout 4 confirmed: a
    # real profile's plugins.txt had 713 lines, none of them Fallout4.esm or
    # any DLC master) never write the base game's own masters into
    # plugins.txt at all — the engine force-loads them regardless, and every
    # tool (MO2/Vortex/libloadorder) omits them the same way (see
    # gui_qt.plugin.plugin_state.save_plugins for the write-side of this same
    # convention). Skipping them here silently dropped Fallout4.esm's own
    # ARMA records (including the vanilla Pip-Boy's) from the slot index.
    # loadorder.txt carries the full order including these; only fall back to
    # plugins.txt's own order if it's missing.
    order_path = profile_dir / "loadorder.txt"
    if order_path.is_file() and not getattr(game, "plugins_include_vanilla", True):
        vanilla = set(getattr(game, "vanilla_plugins", ()))
        ccc = getattr(game, "vanilla_ccc_filename", "") or ""
        data_dir_for_ccc = None
        try:
            data_dir_for_ccc = game.get_mod_data_path()
        except Exception:
            pass
        if ccc and data_dir_for_ccc is not None:
            for base in (data_dir_for_ccc, data_dir_for_ccc.parent):
                f = base / ccc
                if f.is_file():
                    try:
                        vanilla |= {ln.strip() for ln in
                                   f.read_text(encoding="utf-8", errors="replace").splitlines()
                                   if ln.strip()}
                    except OSError:
                        pass
                    break
        full_order = [ln.split("#")[0].strip() for ln in
                      order_path.read_text(encoding="utf-8", errors="replace").splitlines()]
        names = [p for p in full_order if p and (p in vanilla or p in enabled)]
    else:
        names = [e.name for e in read_plugins(pl_path) if e.enabled]

    enabled_mods = []
    if (profile_dir / "modlist.txt").is_file():
        enabled_mods = [e.name for e in read_modlist(profile_dir / "modlist.txt")
                         if not e.is_separator and e.enabled]
    mod_order = list(reversed(enabled_mods))          # lowest priority first, like build_catalog

    full_index = read_mod_index(profile_dir / "modindex.bin") or {}
    by_name: dict = {}                                # plugin_name.lower() -> (mod, rel_path)
    for m in mod_order:
        entry = full_index.get(m)
        if not entry:
            continue
        for files in entry:                           # (normal_files, root_files) — plugins seen in either
            for key, rel in files.items():
                if key.endswith((".esp", ".esm", ".esl")):
                    by_name[key] = (m, rel)

    data_dir = None
    try:
        data_dir = game.get_mod_data_path()
    except Exception:
        pass
    core = (data_dir.parent / (data_dir.name + "_Core")) if data_dir is not None else None
    base_dirs = [d for d in (core, data_dir) if d is not None and d.is_dir()]

    out = []
    for name in names:
        found = None
        hit = by_name.get(name.lower())
        if hit is not None:
            mod_dir = _mod_dir_for(game, hit[0])
            if mod_dir is not None:
                cand = mod_dir / hit[1]
                if cand.is_file():
                    found = cand
        if found is None:
            for d in base_dirs:
                cand = d / name
                if cand.is_file():
                    found = cand
                    break
        if found is not None:
            out.append(found)
    return out


def build_slot_index(game, profile_dir: "Path | None") -> "dict[str, frozenset]":
    """{normalized mesh path: slots} unioned across every enabled plugin's ARMA
    records, in load order. Later plugins only add slots for a path already
    seen — real conflicting declarations for the exact same mesh path are rare
    enough (and harmless for a "does this fit slot X" filter) that they are
    merged rather than tracked separately."""
    index: "dict[str, frozenset]" = {}
    for path in active_plugin_paths(game, profile_dir):
        try:
            records = read_records(path, {"ARMA"})
        except Exception:
            continue                              # malformed plugin: skip it, never abort the whole index
        for rec in records:
            try:
                parsed = parse_arma(rec)
            except (struct.error, IndexError):
                continue
            if parsed is None:
                continue
            slots, model_paths = parsed
            for mp in model_paths:
                index[mp] = index.get(mp, frozenset()) | slots
    return index

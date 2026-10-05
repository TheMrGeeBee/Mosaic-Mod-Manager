"""
item_record_details.py
Per-type record parsing for every handled type OTHER than ARMO (which lives
in armor_record_details.py, since it uniquely needs ARMA indirection), plus
the single-pass index builder that ties everything together for the NIF
Viewer/Character tab's info card.

Unlike ARMO, every type here names its own mesh directly via its own MODL
subrecord (a plain model-path string) -- no linked "addon" record, no join
step. Per-type DATA layouts were verified directly against real Skyrim.esm/
Fallout4.esm records while building this (see the plan file / commit
message for the exact items checked); they genuinely differ enough that one
generic decoder can't cover them:

  WEAP  Skyrim: <I value, f weight, H damage> (10B).
        Fallout 4: NO DATA subrecord at all for WEAP -- value/weight/damage
        moved into a restructured DNAM/Object-Template system, not
        reverse-engineered here. Shown as a dash, a documented limitation,
        not guessed.
  AMMO  Skyrim: <FormID projectile, I flags, f damage, I value, f weight>
        (20B, value/weight NOT first -- confirmed via IronArrow: damage 8.0,
        value 1, weight 0.1, decoded then cross-checked as plausible).
        Fallout 4: <I value, f weight> (8B) -- no damage field.
  BOOK  Skyrim: <B flags, 3x pad, I teaches(union: skill id or spell
        FormID), I value, f weight> (16B) -- "teaches" is skipped, real
        complexity for little display value. Fallout 4: <I value, f weight>
        (8B) -- already simpler, no "teaches" field to skip.
  ALCH/INGR  DATA is weight only (f, 4B) in both games; value comes from
        ENIT's own first 4 bytes (I value, ENIT 20B total) instead --
        confirmed in both Skyrim (FoodHoney) and Fallout 4 (Stimpak: ENIT
        value=48, DATA weight=0.1).
  SLGM  <I value, f weight> (8B) + separate SOUL/SLCP 1-byte enums (soul
        size/capacity, standard CK enum: 0 None/1 Petty/2 Lesser/3 Common/
        4 Greater/5 Grand). Skyrim-only record type -- Fallout 4 has zero
        SLGM records, needs no special-casing, the index simply never gets one.
  KEYM/MISC  <I value, f weight> (8B), confirmed identical in both games.

Weapon Type is read off a WeapType* keyword on Skyrim (WeapTypeSword,
WeapTypeDagger, WeapTypeWarAxe, ... -- confirmed against real IronX items)
or a WeaponType* keyword on Fallout 4 (WeaponTypePistol, WeaponTypeBallistic,
... -- confirmed against a real Fallout4.esm weapon; FO4 does NOT use
Skyrim's abbreviated "WeapType" prefix despite looking like it should), via
the same classify_by_keyword_prefix() helper the FO4 Armor Type fallback
already uses, tried both ways.

A handful of common world-placement types (STAT/FURN/CONT/TREE/FLOR/MSTT --
all confirmed to carry a direct MODL string the same way; STAT alone has
9720 records in Skyrim.esm, the dominant "why doesn't this mesh show
anything" case for non-inventory props) get a minimal generic-fallback
RecordInfo: identity only (Name/Editor ID/Base Form ID/Base Type/Is
Enabled), no value/weight/keywords -- these types don't have an inventory
concept to report.
"""
from __future__ import annotations

import struct

from Utils.nif.character import profile_for_game, slot_label_map
from Utils.plugins import armor_record_details, armor_records
from Utils.plugins.esp_records import Record, read_records
from Utils.plugins.formid_resolve import build_display_order, display_formid, resolve
from Utils.plugins.plugin_parser import is_esl_flagged, read_masters
from Utils.plugins.record_info import RecordInfo, classify_by_keyword_prefix, read_edid_full
from Utils.plugins.string_table import is_localized, load_strings_table

_DELETED_FLAG = 0x00000020
_DASH = "—"

_SIMPLE_DATA = struct.Struct("<If")          # value: u32, weight: f32 -- SLGM/KEYM/MISC, and the
                                              # FO4-simplified AMMO/BOOK variants
_WEAP_DATA_SE = struct.Struct("<IfH")        # value, weight, damage -- Skyrim WEAP only
_AMMO_DATA_SE = struct.Struct("<IIfIf")      # projectile FormID, flags, damage, value, weight -- Skyrim AMMO

_SOUL_SIZE = {0: "None", 1: "Petty", 2: "Lesser", 3: "Common", 4: "Greater", 5: "Grand"}

_FALLBACK_LABELS = {
    "STAT": "Static (STAT)", "FURN": "Furniture (FURN)", "CONT": "Container (CONT)",
    "TREE": "Tree (TREE)", "FLOR": "Flora (FLOR)", "MSTT": "Movable Static (MSTT)",
}


def _mesh_path(rec: Record) -> str:
    raw = rec.sub("MODL")
    if not raw:
        return ""
    return armor_records._normalize_mesh_path(raw.rstrip(b"\x00").decode("utf-8", errors="replace"))


def _resolve_keywords(rec: Record, masters: list, self_name: str, kywd_label_map: dict) -> list:
    kwda_raw = rec.sub("KWDA")
    keyword_labels: list = []
    if kwda_raw:
        for i in range(len(kwda_raw) // 4):
            raw_fid = struct.unpack_from("<I", kwda_raw, i * 4)[0]
            label = kywd_label_map.get(resolve(raw_fid, masters, self_name))
            if label:
                keyword_labels.append(label)
    return keyword_labels


def _base_info(rec: Record, masters: list, self_name: str, localized: bool, strings: dict,
               keyword_labels: list, full_pos: dict, esl_pos: dict, sig: str,
               base_type_label: str, value: "int | None", weight: "float | None",
               extra_fields: list) -> RecordInfo:
    editor_id, name = read_edid_full(rec, localized, strings)
    own_plugin, own_local = resolve(rec.formid, masters, self_name)
    return RecordInfo(
        sig=sig, base_type_label=base_type_label,
        plugin=own_plugin, local_formid=own_local,
        editor_id=editor_id, name=name, value=value, weight=weight,
        keyword_labels=keyword_labels,
        enabled=not (rec.flags & _DELETED_FLAG),
        display_formid=display_formid(own_plugin, own_local, full_pos, esl_pos),
        extra_fields=extra_fields)


# -- per-type parsers -----------------------------------------------------------------------------
# Every parser has the same signature -- (rec, masters, self_name, localized,
# strings, kywd_label_map, full_pos, esl_pos) -> (RecordInfo, mesh_path) | None
# -- so the index builder can dispatch on record signature alone.

def _parse_weap(rec, masters, self_name, localized, strings, kywd_label_map, full_pos, esl_pos):
    mesh_path = _mesh_path(rec)
    if not mesh_path:
        return None
    keyword_labels = _resolve_keywords(rec, masters, self_name, kywd_label_map)
    data_raw = rec.sub("DATA")
    if data_raw and len(data_raw) >= _WEAP_DATA_SE.size:
        value, weight, damage = _WEAP_DATA_SE.unpack(data_raw[:_WEAP_DATA_SE.size])
        damage_text = str(damage)
    else:
        # Fallout 4: no DATA subrecord here at all -- see module docstring.
        value, weight, damage_text = None, None, _DASH
    # Skyrim uses the abbreviated "WeapType*" (WeapTypeSword, ...); Fallout 4
    # uses the full word "WeaponType*" (WeaponTypePistol, WeaponTypeBallistic,
    # ... -- confirmed against a real Fallout4.esm weapon, NOT the same
    # convention as Skyrim despite looking like it should be).
    weapon_type = (classify_by_keyword_prefix(keyword_labels, "WeapType")
                   or classify_by_keyword_prefix(keyword_labels, "WeaponType")
                   or _DASH)
    info = _base_info(rec, masters, self_name, localized, strings, keyword_labels, full_pos, esl_pos,
                       "WEAP", "Weapon (WEAP)", value, weight,
                       [("Weapon Type", weapon_type), ("Damage", damage_text)])
    return info, mesh_path


def _parse_ammo(rec, masters, self_name, localized, strings, kywd_label_map, full_pos, esl_pos):
    mesh_path = _mesh_path(rec)
    if not mesh_path:
        return None
    keyword_labels = _resolve_keywords(rec, masters, self_name, kywd_label_map)
    data_raw = rec.sub("DATA")
    value = weight = None
    damage_text = _DASH
    if data_raw and len(data_raw) >= _AMMO_DATA_SE.size:
        _proj, _flags, damage, value, weight = _AMMO_DATA_SE.unpack(data_raw[:_AMMO_DATA_SE.size])
        damage_text = f"{damage:g}"
    elif data_raw and len(data_raw) >= _SIMPLE_DATA.size:
        value, weight = _SIMPLE_DATA.unpack(data_raw[:_SIMPLE_DATA.size])
    info = _base_info(rec, masters, self_name, localized, strings, keyword_labels, full_pos, esl_pos,
                       "AMMO", "Ammunition (AMMO)", value, weight, [("Damage", damage_text)])
    return info, mesh_path


def _parse_book(rec, masters, self_name, localized, strings, kywd_label_map, full_pos, esl_pos):
    mesh_path = _mesh_path(rec)
    if not mesh_path:
        return None
    keyword_labels = _resolve_keywords(rec, masters, self_name, kywd_label_map)
    data_raw = rec.sub("DATA")
    value = weight = None
    if data_raw and len(data_raw) >= 16:
        value, weight = struct.unpack_from("<If", data_raw, 8)       # Skyrim: value/weight after flags+teaches
    elif data_raw and len(data_raw) >= _SIMPLE_DATA.size:
        value, weight = _SIMPLE_DATA.unpack(data_raw[:_SIMPLE_DATA.size])   # Fallout 4
    info = _base_info(rec, masters, self_name, localized, strings, keyword_labels, full_pos, esl_pos,
                       "BOOK", "Book (BOOK)", value, weight, [])
    return info, mesh_path


def _parse_alch_ingr(rec, masters, self_name, localized, strings, kywd_label_map, full_pos, esl_pos,
                      sig, base_type_label):
    mesh_path = _mesh_path(rec)
    if not mesh_path:
        return None
    keyword_labels = _resolve_keywords(rec, masters, self_name, kywd_label_map)
    data_raw = rec.sub("DATA")
    weight = struct.unpack_from("<f", data_raw, 0)[0] if data_raw and len(data_raw) >= 4 else None
    enit_raw = rec.sub("ENIT")
    value = struct.unpack_from("<I", enit_raw, 0)[0] if enit_raw and len(enit_raw) >= 4 else None
    info = _base_info(rec, masters, self_name, localized, strings, keyword_labels, full_pos, esl_pos,
                       sig, base_type_label, value, weight, [])
    return info, mesh_path


def _parse_alch(*args):
    return _parse_alch_ingr(*args, "ALCH", "Potion (ALCH)")


def _parse_ingr(*args):
    return _parse_alch_ingr(*args, "INGR", "Ingredient (INGR)")


def _parse_slgm(rec, masters, self_name, localized, strings, kywd_label_map, full_pos, esl_pos):
    mesh_path = _mesh_path(rec)
    if not mesh_path:
        return None
    keyword_labels = _resolve_keywords(rec, masters, self_name, kywd_label_map)
    data_raw = rec.sub("DATA")
    value, weight = (_SIMPLE_DATA.unpack(data_raw[:_SIMPLE_DATA.size])
                      if data_raw and len(data_raw) >= _SIMPLE_DATA.size else (None, None))
    soul_raw, slcp_raw = rec.sub("SOUL"), rec.sub("SLCP")
    soul = _SOUL_SIZE.get(soul_raw[0], _DASH) if soul_raw else _DASH
    capacity = _SOUL_SIZE.get(slcp_raw[0], _DASH) if slcp_raw else _DASH
    info = _base_info(rec, masters, self_name, localized, strings, keyword_labels, full_pos, esl_pos,
                       "SLGM", "Soul Gem (SLGM)", value, weight,
                       [("Soul Size", soul), ("Capacity", capacity)])
    return info, mesh_path


def _parse_simple(rec, masters, self_name, localized, strings, kywd_label_map, full_pos, esl_pos,
                   sig, base_type_label):
    mesh_path = _mesh_path(rec)
    if not mesh_path:
        return None
    keyword_labels = _resolve_keywords(rec, masters, self_name, kywd_label_map)
    data_raw = rec.sub("DATA")
    value, weight = (_SIMPLE_DATA.unpack(data_raw[:_SIMPLE_DATA.size])
                      if data_raw and len(data_raw) >= _SIMPLE_DATA.size else (None, None))
    info = _base_info(rec, masters, self_name, localized, strings, keyword_labels, full_pos, esl_pos,
                       sig, base_type_label, value, weight, [])
    return info, mesh_path


def _parse_keym(*args):
    return _parse_simple(*args, "KEYM", "Key (KEYM)")


def _parse_misc(*args):
    return _parse_simple(*args, "MISC", "Misc Item (MISC)")


def _parse_fallback(rec, masters, self_name, localized, strings, _kywd_label_map, full_pos, esl_pos):
    """STAT/FURN/CONT/TREE/FLOR/MSTT: identity only, no inventory concept."""
    mesh_path = _mesh_path(rec)
    if not mesh_path:
        return None
    editor_id, name = read_edid_full(rec, localized, strings)
    own_plugin, own_local = resolve(rec.formid, masters, self_name)
    info = RecordInfo(
        sig=rec.sig, base_type_label=_FALLBACK_LABELS.get(rec.sig, rec.sig),
        plugin=own_plugin, local_formid=own_local,
        editor_id=editor_id, name=name, value=None, weight=None,
        keyword_labels=[], enabled=not (rec.flags & _DELETED_FLAG),
        display_formid=display_formid(own_plugin, own_local, full_pos, esl_pos),
        extra_fields=[])
    return info, mesh_path


_FULL_PARSERS = {
    "WEAP": _parse_weap, "AMMO": _parse_ammo, "BOOK": _parse_book,
    "ALCH": _parse_alch, "INGR": _parse_ingr, "SLGM": _parse_slgm,
    "KEYM": _parse_keym, "MISC": _parse_misc,
}
_FALLBACK_SIGS = frozenset(_FALLBACK_LABELS)
_ALL_SIGS = frozenset({"ARMO", "ARMA", "KYWD"} | set(_FULL_PARSERS) | _FALLBACK_SIGS)


def build_item_index(game, profile_dir) -> dict:
    """mesh path -> every RecordInfo (resolved, load-order winner per
    logical record) referencing that mesh, across all enabled plugins, in
    load order -- ARMO via its ARMA link, every other handled type directly
    via its own MODL."""
    paths = armor_records.active_plugin_paths(game, profile_dir)
    if not paths:
        return {}

    names = [p.name for p in paths]
    esl_flags = {p.name.lower(): is_esl_flagged(p) for p in paths}
    full_pos, esl_pos = build_display_order(names, lambda n: esl_flags.get(n.lower(), False))
    slot_labels = slot_label_map(profile_for_game(getattr(game, "game_id", None)))

    arma_mesh_map: dict = {}        # GlobalKey -> list[mesh_path]  (ARMO's own linkage)
    kywd_label_map: dict = {}       # GlobalKey -> editor_id
    raw_armo: list = []             # [(rec, masters, self_name, localized, strings)], load order
    raw_direct: list = []           # [(parser, rec, masters, self_name, localized, strings)]

    for path in paths:
        try:
            masters = read_masters(path)
            records = read_records(path, _ALL_SIGS)
        except Exception:
            continue
        if not records:
            continue
        self_name = path.name
        localized = is_localized(path)
        strings = load_strings_table(path) if localized else {}

        for rec in records:
            try:
                if rec.sig == "ARMA":
                    parsed = armor_records.parse_arma(rec)
                    if parsed is None:
                        continue
                    _slots, model_paths = parsed
                    key = resolve(rec.formid, masters, self_name)
                    for mp in model_paths:
                        arma_mesh_map.setdefault(key, []).append(mp)
                elif rec.sig == "KYWD":
                    edid_raw = rec.sub("EDID")
                    if edid_raw:
                        editor_id = edid_raw.rstrip(b"\x00").decode("utf-8", errors="replace")
                        if editor_id:
                            kywd_label_map[resolve(rec.formid, masters, self_name)] = editor_id
                elif rec.sig == "ARMO":
                    raw_armo.append((rec, masters, self_name, localized, strings))
                elif rec.sig in _FULL_PARSERS:
                    raw_direct.append((_FULL_PARSERS[rec.sig], rec, masters, self_name, localized, strings))
                elif rec.sig in _FALLBACK_SIGS:
                    raw_direct.append((_parse_fallback, rec, masters, self_name, localized, strings))
            except (struct.error, IndexError):
                continue                                  # malformed record: skip it, never abort

    armo_by_key: dict = {}
    for rec, masters, self_name, localized, strings in raw_armo:
        try:
            parsed = armor_record_details.parse_armo_record(
                rec, masters, self_name, localized, strings, kywd_label_map,
                full_pos, esl_pos, slot_labels)
        except (struct.error, IndexError):
            continue
        if parsed is None:
            continue
        armo_by_key[resolve(rec.formid, masters, self_name)] = parsed   # later plugin overwrites

    direct_by_key: dict = {}
    for parser, rec, masters, self_name, localized, strings in raw_direct:
        try:
            parsed = parser(rec, masters, self_name, localized, strings, kywd_label_map, full_pos, esl_pos)
        except (struct.error, IndexError):
            continue
        if parsed is None:
            continue
        direct_by_key[resolve(rec.formid, masters, self_name)] = parsed

    index: dict = {}
    for info, arma_keys in armo_by_key.values():
        for arma_key in arma_keys:
            for mp in arma_mesh_map.get(arma_key, ()):
                index.setdefault(mp, []).append(info)
    for info, mesh_path in direct_by_key.values():
        index.setdefault(mesh_path, []).append(info)
    return index

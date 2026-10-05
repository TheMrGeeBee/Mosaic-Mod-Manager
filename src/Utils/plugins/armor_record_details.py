"""
armor_record_details.py
ARMO (Armor) record parsing and mesh -> ArmorInfo index, built on top of the
generic esp_records.py reader and armor_records.py's existing ARMA/slot/
plugin-resolution machinery.

Subrecord layout (verified directly against a real Skyrim.esm ARMO record,
ArmorStuddedCuirass/0001B3A2 -- cross-checked against the user's own in-game
reference screenshot: Value 75, Weight 6.0, Light armor, 1 equip slot):
  EDID        zstring                         Editor ID
  FULL        lstring (localized) or zstring   Name
  DATA        8 bytes: u32 value, f32 weight   Value, Weight
  KSIZ/KWDA   u32 count, then u32[count]       Keyword FormIDs
  BOD2/BODT   bitmask (see armor_records)      Equip Slots
  MODL        u32 FormID, repeatable           linked Armor Addon FormID(s) --
              NOT a subrecord literally named "ARMA" (that would collide with
              the ARMA *record* signature); ARMO reuses the MODL tag (its
              usual meaning elsewhere is a model-filename string) to instead
              hold a FormID here. Confirmed by resolving ArmorStuddedCuirass's
              own MODL (0001B398) directly to its ARMA record (EDID
              HideCuirass01AA) in the real file.

KYWD records only have EDID (+CNAM colour, unused) -- the EditorID *is* the
keyword's identity, there's no separate display name.

A record's own `formid` header field is NOT always "defined in this file" --
when a plugin overrides (edits) a record originally defined in one of its
masters, the high byte of that record's own formid points back to the
defining master, same as any cross-plugin reference. So an ARMO's logical
identity (and therefore its correct Base Form ID display and its place in
the "later plugin wins" conflict resolution) is resolved the same way as any
other FormID reference -- via formid_resolve.resolve(), not assumed to
always be "self".

Build order matters: a plugin can only reference a keyword/armature defined
in itself or one of its own masters (masters always load first -- enforced
by plugin_parser.check_late_masters elsewhere), so walking every plugin in
load order and resolving references after the whole load order has been
scanned is always safe, regardless of GRUP order within a single file.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from Utils.plugins import armor_records
from Utils.plugins.esp_records import Record, read_records
from Utils.plugins.formid_resolve import build_display_order, display_formid, resolve
from Utils.plugins.plugin_parser import is_esl_flagged, read_masters
from Utils.plugins.string_table import is_localized, load_strings_table

_DATA = struct.Struct("<If")          # value: u32, weight: f32
_DELETED_FLAG = 0x00000020

# Skyrim's confirmed weight-class keywords (exact EditorID match, case-insensitive).
_SKYRIM_ARMOR_TYPE = {
    "armorlight": "Light",
    "armorheavy": "Heavy",
    "armorclothing": "Clothing",
}
# Fallout 4 has no mechanical weight-class equivalent (the skill was removed);
# fall back to any "ArmorType..." keyword, humanized, rather than guessing a
# name we can't cite. "-" if nothing matches either rule.
_FO4_ARMOR_TYPE_RE = re.compile(r"^ArmorType(.+)$", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_UNKNOWN_ARMOR_TYPE = "—"  # em dash


def _humanize(suffix: str) -> str:
    """CamelCase/PascalCase -> "Title Case With Spaces"."""
    words = _WORD_RE.findall(suffix)
    return " ".join(w.capitalize() for w in words) if words else suffix


@dataclass
class ArmorInfo:
    plugin: str                        # defining plugin filename (the resolved owner, not
                                        # necessarily whichever plugin's copy last parsed it)
    local_formid: int                  # this record's own local id (low 24 bits)
    editor_id: str
    name: str                          # "" if unresolved
    value: int
    weight: float
    armor_type: str                    # "Light"/"Heavy"/"Clothing"/humanized FO4 label/"-"
    keyword_labels: list = field(default_factory=list)   # resolved keyword EditorIDs
    slots: frozenset = frozenset()
    arma_keys: list = field(default_factory=list)         # GlobalKey list (resolved)
    enabled: bool = True
    display_formid: str = ""           # precomputed "062A1B3C" / "FE0021A3" style string


def _read_edid_full(rec: Record, localized: bool, strings: dict) -> "tuple[str, str]":
    edid_raw = rec.sub("EDID")
    editor_id = edid_raw.rstrip(b"\x00").decode("utf-8", errors="replace") if edid_raw else ""
    full_raw = rec.sub("FULL")
    name = ""
    if full_raw:
        if localized:
            if len(full_raw) >= 4:
                sid = struct.unpack_from("<I", full_raw, 0)[0]
                name = strings.get(sid, "")
        else:
            name = full_raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    return editor_id, name


def _classify_armor_type(keyword_editor_ids: list) -> str:
    for eid in keyword_editor_ids:
        label = _SKYRIM_ARMOR_TYPE.get(eid.lower())
        if label:
            return label
    for eid in keyword_editor_ids:
        m = _FO4_ARMOR_TYPE_RE.match(eid)
        if m:
            return _humanize(m.group(1))
    return _UNKNOWN_ARMOR_TYPE


def _parse_armo_record(rec: Record, masters: list, self_name: str, localized: bool,
                        strings: dict, kywd_label_map: dict, full_pos: dict,
                        esl_pos: dict) -> "ArmorInfo | None":
    editor_id, name = _read_edid_full(rec, localized, strings)

    data_raw = rec.sub("DATA")
    value, weight = _DATA.unpack(data_raw[:8]) if data_raw and len(data_raw) >= _DATA.size else (0, 0.0)

    mask = armor_records._slot_mask(rec)
    slots = armor_records._slots_from_mask(mask) if mask else frozenset()

    kwda_raw = rec.sub("KWDA")
    keyword_labels: list = []
    if kwda_raw:
        for i in range(len(kwda_raw) // 4):
            raw_fid = struct.unpack_from("<I", kwda_raw, i * 4)[0]
            label = kywd_label_map.get(resolve(raw_fid, masters, self_name))
            if label:
                keyword_labels.append(label)

    arma_keys = [resolve(struct.unpack("<I", raw)[0], masters, self_name)
                 for raw in rec.all_subs("MODL") if len(raw) == 4]
    if not arma_keys:
        return None   # nothing to link this record to any mesh

    own_plugin, own_local = resolve(rec.formid, masters, self_name)
    return ArmorInfo(
        plugin=own_plugin, local_formid=own_local,
        editor_id=editor_id, name=name, value=value, weight=weight,
        armor_type=_classify_armor_type(keyword_labels),
        keyword_labels=keyword_labels, slots=slots, arma_keys=arma_keys,
        enabled=not (rec.flags & _DELETED_FLAG),
        display_formid=display_formid(own_plugin, own_local, full_pos, esl_pos))


def build_armor_index(game, profile_dir) -> dict:
    """mesh path -> every ARMO (resolved, highest-priority plugin wins per
    logical record) whose ARMA declares that mesh, across all enabled
    plugins, in load order."""
    paths = armor_records.active_plugin_paths(game, profile_dir)
    if not paths:
        return {}

    names = [p.name for p in paths]
    esl_flags = {p.name.lower(): is_esl_flagged(p) for p in paths}
    full_pos, esl_pos = build_display_order(names, lambda n: esl_flags.get(n.lower(), False))

    arma_mesh_map: dict = {}        # GlobalKey -> list[mesh_path]
    kywd_label_map: dict = {}       # GlobalKey -> editor_id
    raw_armo: list = []             # [(rec, masters, self_name, localized, strings)], load order

    for path in paths:
        try:
            masters = read_masters(path)
            records = read_records(path, {"ARMO", "ARMA", "KYWD"})
        except Exception:
            continue
        if not records:
            continue
        self_name = path.name
        has_armo = any(r.sig == "ARMO" for r in records)
        localized = is_localized(path) if has_armo else False
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
            except (struct.error, IndexError):
                continue                                  # malformed record: skip it, never abort

    armo_by_key: dict = {}
    for rec, masters, self_name, localized, strings in raw_armo:
        try:
            info = _parse_armo_record(rec, masters, self_name, localized, strings,
                                       kywd_label_map, full_pos, esl_pos)
        except (struct.error, IndexError):
            continue
        if info is None:
            continue
        armo_by_key[resolve(rec.formid, masters, self_name)] = info   # later plugin overwrites

    index: dict = {}
    for info in armo_by_key.values():
        for arma_key in info.arma_keys:
            for mp in arma_mesh_map.get(arma_key, ()):
                index.setdefault(mp, []).append(info)
    return index

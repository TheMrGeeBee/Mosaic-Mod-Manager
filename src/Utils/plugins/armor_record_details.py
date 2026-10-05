"""
armor_record_details.py
ARMO (Armor) record parsing -- the one handled record type that needs real
indirection: a mesh isn't named on the ARMO record itself, it's named on the
linked ARMA (Armor Addon) record(s), via armor_records.py's existing
ARMA/slot/plugin-resolution machinery. Every other handled type
(item_record_details.py) references its own mesh directly and needs none of
this.

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

import struct

from Utils.plugins import armor_records
from Utils.plugins.esp_records import Record
from Utils.plugins.formid_resolve import display_formid, resolve
from Utils.plugins.record_info import (
    RecordInfo, classify_by_keyword_prefix, format_slots, read_edid_full,
)

_DATA = struct.Struct("<If")          # value: u32, weight: f32
_DELETED_FLAG = 0x00000020

# Skyrim's confirmed weight-class keywords (exact EditorID match, case-insensitive).
_SKYRIM_ARMOR_TYPE = {
    "armorlight": "Light",
    "armorheavy": "Heavy",
    "armorclothing": "Clothing",
}
_UNKNOWN_ARMOR_TYPE = "—"  # em dash


def _classify_armor_type(keyword_editor_ids: list) -> str:
    for eid in keyword_editor_ids:
        label = _SKYRIM_ARMOR_TYPE.get(eid.lower())
        if label:
            return label
    # Fallout 4 has no mechanical weight-class equivalent (the skill was
    # removed); fall back to any "ArmorType..." keyword, humanized, rather
    # than guessing a name we can't cite.
    fo4 = classify_by_keyword_prefix(keyword_editor_ids, "ArmorType")
    return fo4 if fo4 is not None else _UNKNOWN_ARMOR_TYPE


def parse_armo_record(rec: Record, masters: list, self_name: str, localized: bool,
                       strings: dict, kywd_label_map: dict, full_pos: dict,
                       esl_pos: dict, slot_labels: "dict[int, str] | None") -> "tuple | None":
    """(RecordInfo, arma_keys) -- arma_keys is the resolved GlobalKey list
    this ARMO links to, for the caller to join against its own ARMA mesh-path
    map (ARMO itself names no mesh). None if the record has no armature link
    at all (nothing to ever attach to a mesh)."""
    editor_id, name = read_edid_full(rec, localized, strings)

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
    info = RecordInfo(
        sig="ARMO", base_type_label="Armor (ARMO)",
        plugin=own_plugin, local_formid=own_local,
        editor_id=editor_id, name=name, value=value, weight=weight,
        keyword_labels=keyword_labels,
        enabled=not (rec.flags & _DELETED_FLAG),
        display_formid=display_formid(own_plugin, own_local, full_pos, esl_pos),
        extra_fields=[
            ("Armor Type", _classify_armor_type(keyword_labels)),
            ("Equip Slots", format_slots(slots, slot_labels)),
            ("Armor Addon", str(len(arma_keys))),
        ])
    return info, arma_keys

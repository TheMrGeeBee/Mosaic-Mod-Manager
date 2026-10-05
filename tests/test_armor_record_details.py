"""ARMO (Armor) record parsing -- the one handled type that needs ARMA
indirection (a mesh isn't named on ARMO itself). Field values confirmed
against a real Skyrim.esm record (ArmorStuddedCuirass, FormID 0001B3A2)
while building this -- see item_record_details.py / the plan file for the
other 8 handled types and the generic fallback, which don't need this
indirection and live there instead."""
from __future__ import annotations

import struct

import pytest

from Utils.plugins.armor_record_details import parse_armo_record
from Utils.plugins.esp_records import Record
from Utils.plugins.formid_resolve import build_display_order

_NO_POS = ({}, {})            # (full_position, esl_position) for a single-plugin, non-ESL test
_NO_SLOTS = None               # no slot_labels -- format_slots() falls back to bare numbers


def _armo(formid: int = 1, edid: "bytes | None" = b"TestArmor\0", full: "bytes | None" = None,
          value: int = 0, weight: float = 0.0, keywords: "list[int] | None" = None,
          bod2_slots: "tuple[int, ...] | None" = None, modl: "list[int] | None" = None,
          flags: int = 0) -> Record:
    subs: dict = {}
    if edid is not None:
        subs["EDID"] = [edid]
    if full is not None:
        subs["FULL"] = [full]
    subs["DATA"] = [struct.pack("<If", value, weight)]
    if keywords is not None:
        subs["KSIZ"] = [struct.pack("<I", len(keywords))]
        subs["KWDA"] = [b"".join(struct.pack("<I", k) for k in keywords)]
    if bod2_slots is not None:
        mask = 0
        for s in bod2_slots:
            mask |= 1 << (s - 30)
        subs["BOD2"] = [struct.pack("<II", mask, 1)]
    if modl is not None:
        subs["MODL"] = [struct.pack("<I", m) for m in modl]
    return Record("ARMO", formid, subs, flags)


def test_parses_edid_value_weight_slots_and_armature_link():
    rec = _armo(formid=0x1B3A2, edid=b"ArmorStuddedCuirass\0", value=75, weight=6.0,
                bod2_slots=(32,), modl=[0x1B398])
    parsed = parse_armo_record(rec, [], "skyrim.esm", False, {}, {}, *_NO_POS, _NO_SLOTS)
    assert parsed is not None
    info, arma_keys = parsed
    assert info.editor_id == "ArmorStuddedCuirass"
    assert info.sig == "ARMO"
    assert info.base_type_label == "Armor (ARMO)"
    assert info.value == 75
    assert info.weight == pytest.approx(6.0)
    assert arma_keys == [("skyrim.esm", 0x1B398)]
    assert info.plugin == "skyrim.esm"
    assert info.local_formid == 0x1B3A2
    assert info.display_formid == "01B3A2"   # no load-order map given -> bare local id
    extra = dict(info.extra_fields)
    assert extra["Equip Slots"] == "32"
    assert extra["Armor Addon"] == "1"


def test_no_armature_link_means_the_record_cant_be_attached_to_any_mesh():
    rec = _armo(bod2_slots=(32,))   # a DATA-only/BOD2-only record, no MODL at all
    assert parse_armo_record(rec, [], "mod.esp", False, {}, {}, *_NO_POS, _NO_SLOTS) is None


def test_inline_full_name_used_when_plugin_is_not_localized():
    rec = _armo(full=b"Iron Sword\0", modl=[1])
    info, _ = parse_armo_record(rec, [], "mod.esp", False, {}, {}, *_NO_POS, _NO_SLOTS)
    assert info.name == "Iron Sword"


def test_localized_full_name_resolved_through_the_strings_table():
    sid = 43380
    rec = _armo(full=struct.pack("<I", sid), modl=[1])
    info, _ = parse_armo_record(rec, [], "skyrim.esm", True, {sid: "Studded Armor"}, {}, *_NO_POS, _NO_SLOTS)
    assert info.name == "Studded Armor"


def test_localized_full_name_blank_when_string_id_is_unresolved():
    rec = _armo(full=struct.pack("<I", 999), modl=[1])
    info, _ = parse_armo_record(rec, [], "skyrim.esm", True, {}, {}, *_NO_POS, _NO_SLOTS)
    assert info.name == ""


def test_keywords_resolved_to_labels_via_the_kywd_map():
    rec = _armo(keywords=[0x6BBD3, 0x6BBDF], modl=[1])
    kywd_map = {("skyrim.esm", 0x6BBD3): "ArmorLight", ("skyrim.esm", 0x6BBDF): "ArmorMaterialStudded"}
    info, _ = parse_armo_record(rec, [], "skyrim.esm", False, {}, kywd_map, *_NO_POS, _NO_SLOTS)
    assert info.keyword_labels == ["ArmorLight", "ArmorMaterialStudded"]


def test_armor_type_extra_field_reflects_skyrim_keyword():
    rec = _armo(keywords=[0x6BBD3], modl=[1])
    kywd_map = {("skyrim.esm", 0x6BBD3): "ArmorLight"}
    info, _ = parse_armo_record(rec, [], "skyrim.esm", False, {}, kywd_map, *_NO_POS, _NO_SLOTS)
    assert dict(info.extra_fields)["Armor Type"] == "Light"


def test_armor_type_falls_back_to_humanized_fo4_armortype_keyword():
    # Confirmed against a real Fallout4.esm Power Armor piece: keyword
    # "ArmorTypePower" with no ArmorLight/Heavy/Clothing present at all.
    rec = _armo(keywords=[1, 2], modl=[1])
    kywd_map = {("fallout4.esm", 1): "ma_PA_Torso", ("fallout4.esm", 2): "ArmorTypePower"}
    info, _ = parse_armo_record(rec, [], "fallout4.esm", False, {}, kywd_map, *_NO_POS, _NO_SLOTS)
    assert dict(info.extra_fields)["Armor Type"] == "Power"


def test_armor_type_is_a_dash_when_nothing_classifies_it():
    rec = _armo(keywords=[1], modl=[1])
    kywd_map = {("mod.esp", 1): "VendorItemArmor"}
    info, _ = parse_armo_record(rec, [], "mod.esp", False, {}, kywd_map, *_NO_POS, _NO_SLOTS)
    assert dict(info.extra_fields)["Armor Type"] == "—"


def test_is_enabled_reflects_the_deleted_flag():
    rec = _armo(modl=[1], flags=0x00000020)   # Deleted
    info, _ = parse_armo_record(rec, [], "mod.esp", False, {}, {}, *_NO_POS, _NO_SLOTS)
    assert info.enabled is False
    rec2 = _armo(modl=[1], flags=0)
    info2, _ = parse_armo_record(rec2, [], "mod.esp", False, {}, {}, *_NO_POS, _NO_SLOTS)
    assert info2.enabled is True


def test_display_formid_uses_the_resolved_owning_plugin_not_the_scanning_plugin():
    # A record whose formid's high byte points at a master -- this copy is an
    # OVERRIDE of a record actually defined in that master, so its identity
    # (and therefore its Base Form ID) belongs to the master, not to whoever
    # is overriding it.
    rec = _armo(formid=0x0000ABCD, modl=[1])   # high byte 0 == masters[0]
    full_pos = {"skyrim.esm": 0, "overridingmod.esp": 3}
    info, _ = parse_armo_record(rec, ["Skyrim.esm"], "OverridingMod.esp", False, {}, {},
                                 full_pos, {}, _NO_SLOTS)
    assert info.plugin == "skyrim.esm"
    assert info.display_formid == "0000ABCD"   # skyrim.esm's position (0), not OverridingMod's


def test_equip_slots_uses_friendly_labels_when_given():
    rec = _armo(bod2_slots=(32, 34), modl=[1])
    info, _ = parse_armo_record(rec, [], "mod.esp", False, {}, {}, *_NO_POS, {32: "Body", 34: "Forearms"})
    assert dict(info.extra_fields)["Equip Slots"] == "32 (Body), 34 (Forearms)"


# -- optional: a slice of the real game ---------------------------------------------
from pathlib import Path

_SKYRIM_DATA = Path.home() / "games/steamapps/common/Skyrim Special Edition/Data"


@pytest.mark.skipif(not (_SKYRIM_DATA / "Skyrim.esm").is_file(), reason="needs a Skyrim SE install")
def test_real_vanilla_studded_cuirass_matches_the_in_game_reference():
    """Values cross-checked against the real in-game item inspection card for
    Studded Armor / ArmorStuddedCuirass: Base Form ID 0001B3A2, Value 75,
    Weight 6, Armor Type Light, 1 equip slot, 1 Armor Addon."""
    from Utils.plugins.esp_records import read_records
    from Utils.plugins.formid_resolve import resolve
    from Utils.plugins.plugin_parser import read_masters
    from Utils.plugins.string_table import is_localized, load_strings_table

    p = _SKYRIM_DATA / "Skyrim.esm"
    masters = read_masters(p)
    localized = is_localized(p)
    strings = load_strings_table(p) if localized else {}
    records = read_records(p, {"ARMO", "KYWD"})

    kywd_map = {}
    target = None
    for r in records:
        if r.sig == "KYWD":
            edid_raw = r.sub("EDID")
            if edid_raw:
                eid = edid_raw.rstrip(b"\0").decode("utf-8", "replace")
                if eid:
                    kywd_map[resolve(r.formid, masters, p.name)] = eid
        elif r.sig == "ARMO":
            edid_raw = r.sub("EDID")
            if edid_raw and edid_raw.rstrip(b"\0").decode("utf-8", "replace") == "ArmorStuddedCuirass":
                target = r

    assert target is not None
    full_pos, esl_pos = build_display_order([p.name], lambda n: False)
    info, arma_keys = parse_armo_record(target, masters, p.name, localized, strings, kywd_map,
                                         full_pos, esl_pos, _NO_SLOTS)
    assert info.name == "Studded Armor"
    assert info.value == 75
    assert info.weight == pytest.approx(6.0)
    assert dict(info.extra_fields)["Armor Type"] == "Light"
    assert dict(info.extra_fields)["Equip Slots"] == "32"
    assert dict(info.extra_fields)["Armor Addon"] == "1"
    assert len(arma_keys) == 1
    assert info.display_formid == "0001B3A2"

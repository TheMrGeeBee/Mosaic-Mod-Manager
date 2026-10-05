"""ARMO (Armor) record parsing and the mesh -> ArmorInfo index. Byte layout
and field values confirmed against a real Skyrim.esm (ArmorStuddedCuirass,
FormID 0001B3A2) and a real Fallout4.esm (cc_Armor_Power_X01_Torso) while
building this -- in particular that the armature link lives in ARMO's MODL
subrecord (not a subrecord literally named "ARMA", which would collide with
the ARMA record signature), and that DATA is (value: u32, weight: f32)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from Utils.plugins.armor_record_details import (
    _classify_armor_type, _parse_armo_record, build_armor_index,
)
from Utils.plugins.esp_records import Record
from Utils.plugins.formid_resolve import build_display_order


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


def _kywd(formid: int, edid: bytes) -> Record:
    return Record("KYWD", formid, {"EDID": [edid]})


_NO_POS = ({}, {})  # (full_position, esl_position) for a single-plugin, non-ESL test


def test_parses_edid_value_weight_slots_and_armature_link():
    rec = _armo(formid=0x1B3A2, edid=b"ArmorStuddedCuirass\0", value=75, weight=6.0,
                bod2_slots=(32,), modl=[0x1B398])
    info = _parse_armo_record(rec, [], "skyrim.esm", False, {}, {}, *_NO_POS)
    assert info is not None
    assert info.editor_id == "ArmorStuddedCuirass"
    assert info.value == 75
    assert info.weight == pytest.approx(6.0)
    assert info.slots == frozenset({32})
    assert info.arma_keys == [("skyrim.esm", 0x1B398)]
    assert info.plugin == "skyrim.esm"
    assert info.local_formid == 0x1B3A2
    assert info.display_formid == "01B3A2"   # no load-order map given -> bare local id


def test_no_armature_link_means_the_record_cant_be_attached_to_any_mesh():
    rec = _armo(bod2_slots=(32,))   # a DATA-only/BOD2-only record, no MODL at all
    assert _parse_armo_record(rec, [], "mod.esp", False, {}, {}, *_NO_POS) is None


def test_inline_full_name_used_when_plugin_is_not_localized():
    rec = _armo(full=b"Iron Sword\0", modl=[1])
    info = _parse_armo_record(rec, [], "mod.esp", False, {}, {}, *_NO_POS)
    assert info.name == "Iron Sword"


def test_localized_full_name_resolved_through_the_strings_table():
    sid = 43380
    rec = _armo(full=struct.pack("<I", sid), modl=[1])
    info = _parse_armo_record(rec, [], "skyrim.esm", True, {sid: "Studded Armor"}, {}, *_NO_POS)
    assert info.name == "Studded Armor"


def test_localized_full_name_blank_when_string_id_is_unresolved():
    rec = _armo(full=struct.pack("<I", 999), modl=[1])
    info = _parse_armo_record(rec, [], "skyrim.esm", True, {}, {}, *_NO_POS)
    assert info.name == ""


def test_keywords_resolved_to_labels_via_the_kywd_map():
    rec = _armo(keywords=[0x6BBD3, 0x6BBDF], modl=[1])
    kywd_map = {("skyrim.esm", 0x6BBD3): "ArmorLight", ("skyrim.esm", 0x6BBDF): "ArmorMaterialStudded"}
    info = _parse_armo_record(rec, [], "skyrim.esm", False, {}, kywd_map, *_NO_POS)
    assert info.keyword_labels == ["ArmorLight", "ArmorMaterialStudded"]


def test_is_enabled_reflects_the_deleted_flag():
    rec = _armo(modl=[1], flags=0x00000020)   # Deleted
    info = _parse_armo_record(rec, [], "mod.esp", False, {}, {}, *_NO_POS)
    assert info.enabled is False
    rec2 = _armo(modl=[1], flags=0)
    info2 = _parse_armo_record(rec2, [], "mod.esp", False, {}, {}, *_NO_POS)
    assert info2.enabled is True


def test_display_formid_uses_the_resolved_owning_plugin_not_the_scanning_plugin():
    # A record whose formid's high byte points at a master -- this copy is an
    # OVERRIDE of a record actually defined in that master, so its identity
    # (and therefore its Base Form ID) belongs to the master, not to whoever
    # is overriding it.
    rec = _armo(formid=0x0000ABCD, modl=[1])   # high byte 0 == masters[0]
    full_pos = {"skyrim.esm": 0, "overridingmod.esp": 3}
    info = _parse_armo_record(rec, ["Skyrim.esm"], "OverridingMod.esp", False, {}, {},
                               full_pos, {})
    assert info.plugin == "skyrim.esm"
    assert info.display_formid == "0000ABCD"   # skyrim.esm's position (0), not OverridingMod's


# -- armor type classification -----------------------------------------------------------
def test_skyrim_weight_class_keywords_classify_exactly():
    assert _classify_armor_type(["ArmorLight"]) == "Light"
    assert _classify_armor_type(["ArmorHeavy"]) == "Heavy"
    assert _classify_armor_type(["ArmorClothing"]) == "Clothing"
    assert _classify_armor_type(["armorlight"]) == "Light"          # case-insensitive


def test_fallout4_falls_back_to_a_humanized_armortype_keyword():
    # Confirmed against a real Fallout4.esm Power Armor piece: keyword
    # "ArmorTypePower" with no ArmorLight/Heavy/Clothing present at all.
    assert _classify_armor_type(["ArmorTypePower"]) == "Power"
    assert _classify_armor_type(["ma_PA_Torso", "ArmorTypePower"]) == "Power"


def test_unclassifiable_keywords_give_a_dash():
    assert _classify_armor_type(["VendorItemArmor", "SomeOtherTag"]) == "—"
    assert _classify_armor_type([]) == "—"


# -- build_armor_index: a fabricated profile end to end -----------------------------------
def _record_bytes(rec_type: str, formid: int, subs: dict, flags: int = 0) -> bytes:
    from test_esp_records import _rec, _sub
    body = b"".join(_sub(sig, data) for sig, values in subs.items() for data in values)
    return _rec(rec_type, formid, body, flags)


def _armo_bytes(formid: int, edid: bytes, value: int, weight: float,
                 slots: "tuple[int, ...]", modl: int, keywords: "list[int]" = ()) -> bytes:
    subs: dict = {"EDID": [edid], "DATA": [struct.pack("<If", value, weight)],
                  "MODL": [struct.pack("<I", modl)]}
    if slots:
        mask = 0
        for s in slots:
            mask |= 1 << (s - 30)
        subs["BOD2"] = [struct.pack("<II", mask, 1)]
    if keywords:
        subs["KSIZ"] = [struct.pack("<I", len(keywords))]
        subs["KWDA"] = [b"".join(struct.pack("<I", k) for k in keywords)]
    return _record_bytes("ARMO", formid, subs)


def _arma_bytes(formid: int, slots: "tuple[int, ...]", model: bytes) -> bytes:
    mask = 0
    for s in slots:
        mask |= 1 << (s - 30)
    subs = {"BOD2": [struct.pack("<II", mask, 1)], "MOD2": [model]}
    return _record_bytes("ARMA", formid, subs)


def _kywd_bytes(formid: int, edid: bytes) -> bytes:
    return _record_bytes("KYWD", formid, {"EDID": [edid]})


def _write_full_plugin(path: Path, armo: bytes = b"", arma: bytes = b"", kywd: bytes = b"") -> None:
    from test_esp_records import _grup, _plugin
    groups = b""
    if armo:
        groups += _grup("ARMO", armo)
    if arma:
        groups += _grup("ARMA", arma)
    if kywd:
        groups += _grup("KYWD", kywd)
    path.write_bytes(_plugin(groups))


def _fake_game(data_dir: Path, mods_dir: Path):
    class FakeGame:
        game_id = "skyrim_se"

        def get_mod_data_path(self):
            return data_dir

        def get_effective_mod_staging_path(self):
            return mods_dir

    return FakeGame()


def test_build_armor_index_links_armo_to_its_meshes_via_arma(tmp_path):
    from Utils.filemap import _write_mod_index

    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "Studded Mod"
    mod_dir.mkdir(parents=True)
    armo = _armo_bytes(1, b"ArmorStudded\0", 75, 6.0, (32,), modl=2,
                        keywords=[100])
    arma = _arma_bytes(2, (32,), b"Armor\\studded.nif\0")
    kywd = _kywd_bytes(100, b"ArmorLight\0")
    _write_full_plugin(mod_dir / "Studded.esp", armo=armo, arma=arma, kywd=kywd)

    (tmp_path / "plugins.txt").write_text("*Studded.esp\n", encoding="utf-8")
    (tmp_path / "modlist.txt").write_text("*Studded Mod\n", encoding="utf-8")
    _write_mod_index(tmp_path / "modindex.bin",
                     {"Studded Mod": ({"studded.esp": "Studded.esp"}, {})})

    index = build_armor_index(_fake_game(tmp_path / "Data", mods_dir), tmp_path)
    assert "meshes/armor/studded.nif" in index
    infos = index["meshes/armor/studded.nif"]
    assert len(infos) == 1
    assert infos[0].editor_id == "ArmorStudded"
    assert infos[0].armor_type == "Light"
    assert infos[0].value == 75


def test_build_armor_index_later_plugin_overrides_the_same_logical_record(tmp_path):
    """Two plugins both touch the SAME armor (via an override -- the second
    plugin's ARMO formid points back at the first plugin as its master), the
    later one in load order should win, and the mesh should list ONE entry,
    not two duplicate ones for the same logical item."""
    from Utils.filemap import _write_mod_index

    mods_dir = tmp_path / "mods"
    base_dir = mods_dir / "Base Mod"
    base_dir.mkdir(parents=True)
    armo_base = _armo_bytes(1, b"ArmorX\0", 10, 1.0, (32,), modl=2)
    arma = _arma_bytes(2, (32,), b"x.nif\0")
    _write_full_plugin(base_dir / "Base.esp", armo=armo_base, arma=arma)

    patch_dir = mods_dir / "Patch Mod"
    patch_dir.mkdir(parents=True)
    # Overriding copy: high byte 0 -> master index 0 -> Base.esp, same local id 1.
    armo_patch = _armo_bytes(0x00000001, b"ArmorX\0", 999, 1.0, (32,), modl=2)
    from test_esp_records import _grup, _plugin, _rec, _sub
    tes4 = _rec("TES4", 0, _sub("MAST", b"Base.esp\0"))
    patch_data = tes4 + _grup("ARMO", armo_patch)
    (patch_dir / "Patch.esp").write_bytes(patch_data)

    (tmp_path / "plugins.txt").write_text("*Base.esp\n*Patch.esp\n", encoding="utf-8")
    (tmp_path / "modlist.txt").write_text("*Patch Mod\n*Base Mod\n", encoding="utf-8")  # Patch wins
    _write_mod_index(tmp_path / "modindex.bin", {
        "Base Mod": ({"base.esp": "Base.esp"}, {}),
        "Patch Mod": ({"patch.esp": "Patch.esp"}, {}),
    })

    index = build_armor_index(_fake_game(tmp_path / "Data", mods_dir), tmp_path)
    infos = index["meshes/x.nif"]
    assert len(infos) == 1
    assert infos[0].value == 999          # the override's value won
    assert infos[0].plugin == "base.esp"  # identity still belongs to the original definer


def test_build_armor_index_with_no_profile_dir_is_empty():
    assert build_armor_index(object(), None) == {}


# -- optional: a slice of the real game ---------------------------------------------
_SKYRIM_DATA = Path.home() / "games/steamapps/common/Skyrim Special Edition/Data"
_FO4_DATA = Path.home() / "games/steamapps/common/Fallout 4/Data"


@pytest.mark.skipif(not (_SKYRIM_DATA / "Skyrim.esm").is_file(), reason="needs a Skyrim SE install")
def test_real_vanilla_studded_cuirass_matches_the_in_game_reference():
    """Values cross-checked against the real in-game item inspection card for
    Studded Armor / ArmorStuddedCuirass: Base Form ID 0001B3A2, Value 75,
    Weight 6, Armor Type Light, 1 equip slot, 1 Armor Addon."""
    from Utils.plugins.esp_records import read_records
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
                    from Utils.plugins.formid_resolve import resolve
                    kywd_map[resolve(r.formid, masters, p.name)] = eid
        elif r.sig == "ARMO":
            edid_raw = r.sub("EDID")
            if edid_raw and edid_raw.rstrip(b"\0").decode("utf-8", "replace") == "ArmorStuddedCuirass":
                target = r

    assert target is not None
    full_pos, esl_pos = build_display_order([p.name], lambda n: False)
    info = _parse_armo_record(target, masters, p.name, localized, strings, kywd_map, full_pos, esl_pos)
    assert info.name == "Studded Armor"
    assert info.value == 75
    assert info.weight == pytest.approx(6.0)
    assert info.armor_type == "Light"
    assert info.slots == frozenset({32})
    assert len(info.arma_keys) == 1
    assert info.display_formid == "0001B3A2"


@pytest.mark.skipif(not (_FO4_DATA / "Fallout4.esm").is_file(), reason="needs a Fallout 4 install")
def test_real_vanilla_fallout4_power_armor_piece_parses():
    from Utils.plugins.esp_records import read_records
    from Utils.plugins.plugin_parser import read_masters
    from Utils.plugins.string_table import is_localized, load_strings_table

    p = _FO4_DATA / "Fallout4.esm"
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
                    from Utils.plugins.formid_resolve import resolve
                    kywd_map[resolve(r.formid, masters, p.name)] = eid
        elif r.sig == "ARMO":
            edid_raw = r.sub("EDID")
            if edid_raw and edid_raw.rstrip(b"\0").decode("utf-8", "replace") == "cc_Armor_Power_X01_Torso":
                target = r

    assert target is not None
    full_pos, esl_pos = build_display_order([p.name], lambda n: False)
    info = _parse_armo_record(target, masters, p.name, localized, strings, kywd_map, full_pos, esl_pos)
    assert info.name == "X-01e Torso"
    assert info.value == 280
    assert info.weight == pytest.approx(20.0)
    assert info.armor_type == "Power"

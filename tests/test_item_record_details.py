"""Per-type record parsing for every handled type other than ARMO (WEAP,
AMMO, BOOK, ALCH, INGR, SLGM, KEYM, MISC, plus the generic STAT/FURN/CONT/
TREE/FLOR/MSTT fallback), and the unified build_item_index(). Byte layouts
confirmed directly against real Skyrim.esm/Fallout4.esm records while
building this -- see item_record_details.py's own module docstring for the
exact items checked and the cross-game differences found (FO4 WEAP has no
DATA subrecord at all; FO4 AMMO/BOOK use shorter DATA layouts than Skyrim;
ALCH/INGR's value lives in ENIT, not DATA, in both games)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from Utils.plugins.esp_records import Record
from Utils.plugins.formid_resolve import build_display_order
from Utils.plugins.item_record_details import (
    _parse_alch, _parse_ammo, _parse_book, _parse_fallback, _parse_ingr, _parse_keym,
    _parse_misc, _parse_slgm, _parse_weap, build_item_index,
)

_NO_POS = ({}, {})
_ARGS = ([], "mod.esp", False, {}, {})   # masters, self_name, localized, strings, kywd_label_map


def _rec(sig: str, formid: int, subs: dict, flags: int = 0) -> Record:
    return Record(sig, formid, subs, flags)


def test_no_modl_means_nothing_to_attach_to_any_mesh():
    for parser in (_parse_weap, _parse_ammo, _parse_book, _parse_alch, _parse_ingr,
                   _parse_slgm, _parse_keym, _parse_misc, _parse_fallback):
        rec = _rec("WEAP", 1, {"EDID": [b"X\0"]})
        assert parser(rec, *_ARGS, *_NO_POS) is None


# -- WEAP -----------------------------------------------------------------------------------------
def test_weap_skyrim_layout_decodes_value_weight_damage_and_type():
    data = struct.pack("<IfH", 25, 9.0, 7)   # IronSword's real bytes
    rec = _rec("WEAP", 1, {"MODL": [b"weapons\\iron.nif\0"], "DATA": [data],
                            "KWDA": [struct.pack("<I", 1)]})
    kywd_map = {("mod.esp", 1): "WeapTypeSword"}
    info, mesh_path = _parse_weap(rec, [], "mod.esp", False, {}, kywd_map, *_NO_POS)
    assert mesh_path == "meshes/weapons/iron.nif"
    assert info.value == 25
    assert info.weight == pytest.approx(9.0)
    extra = dict(info.extra_fields)
    assert extra["Damage"] == "7"
    assert extra["Weapon Type"] == "Sword"


def test_weap_classifies_fo4_weapontype_prefix_not_skyrims_weaptype():
    rec = _rec("WEAP", 1, {"MODL": [b"weapons\\pistol.nif\0"], "KWDA": [struct.pack("<I", 1)]})
    kywd_map = {("mod.esp", 1): "WeaponTypePistol"}
    info, _ = _parse_weap(rec, [], "mod.esp", False, {}, kywd_map, *_NO_POS)
    assert dict(info.extra_fields)["Weapon Type"] == "Pistol"


def test_weap_missing_data_subrecord_gives_dash_not_a_guess():
    # Fallout 4: WEAP has no DATA subrecord at all.
    rec = _rec("WEAP", 1, {"MODL": [b"weapons\\10mm.nif\0"]})
    info, _ = _parse_weap(rec, *_ARGS, *_NO_POS)
    assert info.value is None
    assert info.weight is None
    assert dict(info.extra_fields)["Damage"] == "—"


# -- AMMO -----------------------------------------------------------------------------------------
def test_ammo_skyrim_layout_includes_damage():
    # IronArrow's real bytes: projectile FormID, flags=4, damage=8.0, value=1, weight=0.1
    data = struct.pack("<IIfIf", 0x0003be11, 4, 8.0, 1, 0.1)
    rec = _rec("AMMO", 1, {"MODL": [b"weapons\\arrow.nif\0"], "DATA": [data]})
    info, mesh_path = _parse_ammo(rec, *_ARGS, *_NO_POS)
    assert info.value == 1
    assert info.weight == pytest.approx(0.1)
    assert dict(info.extra_fields)["Damage"] == "8"


def test_ammo_fallout4_layout_has_no_damage():
    data = struct.pack("<If", 3, 0.0)
    rec = _rec("AMMO", 1, {"MODL": [b"ammo\\45cal.nif\0"], "DATA": [data]})
    info, _ = _parse_ammo(rec, *_ARGS, *_NO_POS)
    assert info.value == 3
    assert info.weight == pytest.approx(0.0)
    assert dict(info.extra_fields)["Damage"] == "—"


# -- BOOK -----------------------------------------------------------------------------------------
def test_book_skyrim_layout_value_after_flags_and_teaches():
    # byte flags, 3 bytes padding, u32 teaches(skill id or spell FormID,
    # skipped -- not decoded), u32 value, f32 weight (16 bytes total).
    data = struct.pack("<B3xIIf", 4, 0x0001C789, 345, 1.0)
    rec = _rec("BOOK", 1, {"MODL": [b"clutter\\book.nif\0"], "DATA": [data]})
    info, _ = _parse_book(rec, *_ARGS, *_NO_POS)
    assert info.value == 345
    assert info.weight == pytest.approx(1.0)


def test_book_fallout4_layout_is_just_value_and_weight():
    data = struct.pack("<If", 10, 0.5)
    rec = _rec("BOOK", 1, {"MODL": [b"props\\note.nif\0"], "DATA": [data]})
    info, _ = _parse_book(rec, *_ARGS, *_NO_POS)
    assert info.value == 10
    assert info.weight == pytest.approx(0.5)


# -- ALCH / INGR ------------------------------------------------------------------------------------
def test_alch_value_comes_from_enit_weight_from_data():
    rec = _rec("ALCH", 1, {"MODL": [b"clutter\\honey.nif\0"], "DATA": [struct.pack("<f", 0.1)],
                            "ENIT": [struct.pack("<I", 2) + b"\x00" * 16]})
    info, _ = _parse_alch(rec, *_ARGS, *_NO_POS)
    assert info.value == 2
    assert info.weight == pytest.approx(0.1)
    assert info.base_type_label == "Potion (ALCH)"


def test_ingr_same_layout_as_alch():
    rec = _rec("INGR", 1, {"MODL": [b"clutter\\flower.nif\0"], "DATA": [struct.pack("<f", 0.1)],
                            "ENIT": [struct.pack("<I", 5) + b"\x00" * 16]})
    info, _ = _parse_ingr(rec, *_ARGS, *_NO_POS)
    assert info.value == 5
    assert info.base_type_label == "Ingredient (INGR)"


# -- SLGM -----------------------------------------------------------------------------------------
def test_slgm_decodes_value_weight_and_soul_enums():
    rec = _rec("SLGM", 1, {"MODL": [b"clutter\\gem.nif\0"], "DATA": [struct.pack("<If", 25, 0.2)],
                            "SOUL": [b"\x00"], "SLCP": [b"\x02"]})
    info, _ = _parse_slgm(rec, *_ARGS, *_NO_POS)
    assert info.value == 25
    assert info.weight == pytest.approx(0.2)
    extra = dict(info.extra_fields)
    assert extra["Soul Size"] == "None"
    assert extra["Capacity"] == "Lesser"


def test_slgm_missing_soul_subrecords_give_a_dash():
    rec = _rec("SLGM", 1, {"MODL": [b"clutter\\gem.nif\0"], "DATA": [struct.pack("<If", 1, 0.1)]})
    info, _ = _parse_slgm(rec, *_ARGS, *_NO_POS)
    extra = dict(info.extra_fields)
    assert extra["Soul Size"] == "—"
    assert extra["Capacity"] == "—"


# -- KEYM / MISC ------------------------------------------------------------------------------------
def test_keym_and_misc_simple_value_weight():
    rec1 = _rec("KEYM", 1, {"MODL": [b"clutter\\key.nif\0"], "DATA": [struct.pack("<If", 0, 0.0)]})
    info1, _ = _parse_keym(rec1, *_ARGS, *_NO_POS)
    assert info1.value == 0 and info1.weight == 0.0 and info1.base_type_label == "Key (KEYM)"

    rec2 = _rec("MISC", 1, {"MODL": [b"clutter\\ingot.nif\0"], "DATA": [struct.pack("<If", 7, 1.0)]})
    info2, _ = _parse_misc(rec2, *_ARGS, *_NO_POS)
    assert info2.value == 7 and info2.base_type_label == "Misc Item (MISC)"


# -- generic fallback ---------------------------------------------------------------------------
def test_fallback_is_identity_only_no_value_or_weight():
    rec = _rec("STAT", 1, {"EDID": [b"SomeStatic\0"], "MODL": [b"architecture\\wall.nif\0"]})
    info, mesh_path = _parse_fallback(rec, *_ARGS, *_NO_POS)
    assert info.sig == "STAT"
    assert info.base_type_label == "Static (STAT)"
    assert info.editor_id == "SomeStatic"
    assert info.value is None
    assert info.weight is None
    assert info.extra_fields == []
    assert mesh_path == "meshes/architecture/wall.nif"


def test_fallback_unknown_sig_uses_its_own_signature_as_the_label():
    rec = _rec("FURN", 1, {"MODL": [b"furniture\\chair.nif\0"]})
    info, _ = _parse_fallback(rec, *_ARGS, *_NO_POS)
    assert info.base_type_label == "Furniture (FURN)"


# -- build_item_index: a fabricated profile end to end, multiple types sharing one scan -----------
def _record_bytes(rec_type: str, formid: int, subs: dict, flags: int = 0) -> bytes:
    from test_esp_records import _rec as rec_bytes, _sub
    body = b"".join(_sub(sig, data) for sig, values in subs.items() for data in values)
    return rec_bytes(rec_type, formid, body, flags)


def _write_full_plugin(path: Path, groups: "dict[str, bytes]") -> None:
    from test_esp_records import _grup, _plugin
    data = b"".join(_grup(sig, body) for sig, body in groups.items())
    path.write_bytes(_plugin(data))


def _fake_game(data_dir: Path, mods_dir: Path):
    class FakeGame:
        game_id = "skyrim_se"

        def get_mod_data_path(self):
            return data_dir

        def get_effective_mod_staging_path(self):
            return mods_dir
    return FakeGame()


def test_build_item_index_covers_armo_and_a_direct_type_in_one_pass(tmp_path):
    from Utils.filemap import _write_mod_index

    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "Mixed Mod"
    mod_dir.mkdir(parents=True)

    armo = _record_bytes("ARMO", 1, {
        "EDID": [b"TestArmor\0"], "DATA": [struct.pack("<If", 10, 5.0)], "MODL": [struct.pack("<I", 2)],
    })
    arma = _record_bytes("ARMA", 2, {
        "BOD2": [struct.pack("<II", 1 << 2, 1)], "MOD2": [b"armor\\test.nif\0"],   # slot 32
    })
    weap = _record_bytes("WEAP", 3, {
        "EDID": [b"TestSword\0"], "DATA": [struct.pack("<IfH", 20, 8.0, 6)],
        "MODL": [b"weapons\\test.nif\0"],
    })
    stat = _record_bytes("STAT", 4, {"EDID": [b"TestStatic\0"], "MODL": [b"architecture\\test.nif\0"]})

    _write_full_plugin(mod_dir / "Mixed.esp", {"ARMO": armo, "ARMA": arma, "WEAP": weap, "STAT": stat})

    (tmp_path / "plugins.txt").write_text("*Mixed.esp\n", encoding="utf-8")
    (tmp_path / "modlist.txt").write_text("*Mixed Mod\n", encoding="utf-8")
    _write_mod_index(tmp_path / "modindex.bin", {"Mixed Mod": ({"mixed.esp": "Mixed.esp"}, {})})

    index = build_item_index(_fake_game(tmp_path / "Data", mods_dir), tmp_path)

    assert index["meshes/armor/test.nif"][0].sig == "ARMO"
    assert index["meshes/armor/test.nif"][0].editor_id == "TestArmor"
    assert index["meshes/weapons/test.nif"][0].sig == "WEAP"
    assert index["meshes/weapons/test.nif"][0].editor_id == "TestSword"
    assert index["meshes/architecture/test.nif"][0].sig == "STAT"
    assert index["meshes/architecture/test.nif"][0].editor_id == "TestStatic"
    assert index["meshes/architecture/test.nif"][0].value is None


def test_build_item_index_with_no_profile_dir_is_empty():
    assert build_item_index(object(), None) == {}


# -- optional: a slice of the real game ---------------------------------------------
_SKYRIM_DATA = Path.home() / "games/steamapps/common/Skyrim Special Edition/Data"
_FO4_DATA = Path.home() / "games/steamapps/common/Fallout 4/Data"


def _solo_profile(tmp_path: Path, plugin_name: str) -> Path:
    (tmp_path / "plugins.txt").write_text("", encoding="utf-8")
    (tmp_path / "loadorder.txt").write_text(f"{plugin_name}\n", encoding="utf-8")
    (tmp_path / "modlist.txt").write_text("", encoding="utf-8")
    return tmp_path


class _RealGame:
    def __init__(self, game_id, data_dir, vanilla_plugins):
        self.game_id = game_id
        self._data_dir = data_dir
        self.plugins_include_vanilla = False
        self.vanilla_plugins = vanilla_plugins
        self.vanilla_ccc_filename = ""

    def get_mod_data_path(self):
        return self._data_dir


@pytest.mark.skipif(not (_SKYRIM_DATA / "Skyrim.esm").is_file(), reason="needs a Skyrim SE install")
def test_real_skyrim_items_match_hand_decoded_values(tmp_path):
    """Cross-checked against this module's own hand decode of the real
    bytes while researching the per-type DATA layouts."""
    profile = _solo_profile(tmp_path, "Skyrim.esm")
    index = build_item_index(_RealGame("skyrim_se", _SKYRIM_DATA, ["Skyrim.esm"]), profile)

    by_edid = {i.editor_id: i for infos in index.values() for i in infos}

    sword = by_edid["IronSword"]
    assert sword.value == 25 and sword.weight == pytest.approx(9.0)
    assert dict(sword.extra_fields)["Damage"] == "7"
    assert dict(sword.extra_fields)["Weapon Type"] == "Sword"

    dagger = by_edid["IronDagger"]
    assert dagger.value == 10
    assert dict(dagger.extra_fields)["Weapon Type"] == "Dagger"

    arrow = by_edid["IronArrow"]
    assert arrow.value == 1 and arrow.weight == pytest.approx(0.1, abs=1e-3)
    assert dict(arrow.extra_fields)["Damage"] == "8"

    book = by_edid["SpellTomeFireball"]
    assert book.value == 345 and book.weight == pytest.approx(1.0)

    honey = by_edid["FoodHoney"]
    assert honey.value == 2 and honey.weight == pytest.approx(0.1, abs=1e-3)

    ingot = by_edid["IngotIron"]
    assert ingot.value == 7 and ingot.weight == pytest.approx(1.0)

    gem = by_edid["WhiterunSoulGem"]
    assert gem.value == 25
    assert dict(gem.extra_fields)["Soul Size"] == "None"
    assert dict(gem.extra_fields)["Capacity"] == "Lesser"

    key = by_edid["MorthalGuardhouseKey"]
    assert key.value == 0 and key.weight == pytest.approx(0.0)


@pytest.mark.skipif(not (_FO4_DATA / "Fallout4.esm").is_file(), reason="needs a Fallout 4 install")
def test_real_fallout4_items_match_hand_decoded_values(tmp_path):
    profile = _solo_profile(tmp_path, "Fallout4.esm")
    index = build_item_index(_RealGame("Fallout4", _FO4_DATA, ["Fallout4.esm"]), profile)

    by_edid = {i.editor_id: i for infos in index.values() for i in infos}

    pin = by_edid["BobbyPin"]
    assert pin.value == 1 and pin.weight == pytest.approx(0.0)

    tape = by_edid["DuctTape01"]
    assert tape.value == 12 and tape.weight == pytest.approx(0.1, abs=1e-3)

    ammo = by_edid["CompanionAmmo45Caliber"]
    assert ammo.value == 3 and ammo.weight == pytest.approx(0.0)
    assert dict(ammo.extra_fields)["Damage"] == "—"   # FO4 AMMO has no damage field

    stim = by_edid["Stimpak"]
    assert stim.value == 48 and stim.weight == pytest.approx(0.1, abs=1e-3)

    key = by_edid["PrydwenArmoryKey"]
    assert key.value == 1 and key.weight == pytest.approx(0.0)

    weap = by_edid["VRWorkshopShared_10mm_NonPlayable"]
    assert weap.value is None and weap.weight is None   # documented FO4 WEAP limitation
    assert dict(weap.extra_fields)["Weapon Type"] == "Pistol"

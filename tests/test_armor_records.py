"""ARMA (Armor Addon) record extraction: the authoritative BOD2/BODT slot
bitmask + model paths, and the profile-wide index built from every enabled
plugin's load order. Real-data checks are pinned to values confirmed by hand
against a real Skyrim.esm while building this (IronCuirassAA -> body+forearms
+calves, IronBootsAA -> feet+calves, IronHelmetAA -> hair+ears — a helmet
occupies the hair/ears slots, not the head slot; that is real engine
behaviour, not a bug in this reader)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from Utils.plugins.armor_records import build_slot_index, parse_arma
from Utils.plugins.esp_records import Record


def _arma(bod2: "bytes | None" = None, bodt: "bytes | None" = None,
          mod2: "bytes | None" = None, mod3: "bytes | None" = None) -> Record:
    subs: dict = {}
    if bod2 is not None:
        subs["BOD2"] = [bod2]
    if bodt is not None:
        subs["BODT"] = [bodt]
    if mod2 is not None:
        subs["MOD2"] = [mod2]
    if mod3 is not None:
        subs["MOD3"] = [mod3]
    return Record("ARMA", 1, subs)


def _mask(*slots: int) -> int:
    m = 0
    for s in slots:
        m |= 1 << (s - 30)
    return m


def test_bod2_mask_decodes_to_slot_numbers():
    rec = _arma(bod2=struct.pack("<II", _mask(32, 34, 38), 1),
                mod2=b"Armor\\Iron\\Male\\CuirassLight_1.nif\0")
    slots, paths = parse_arma(rec)
    assert slots == frozenset({32, 34, 38})
    assert paths == ["meshes/armor/iron/male/cuirasslight_1.nif"]


def test_bodt_12_byte_variant_decodes_the_same_way():
    bodt = struct.pack("<IB3xI", _mask(37, 38), 0, 1)          # mask, flags, junk, skill
    rec = _arma(bodt=bodt, mod2=b"Armor\\Iron\\Male\\Boots_1.nif\0")
    slots, _ = parse_arma(rec)
    assert slots == frozenset({37, 38})


def test_bod2_is_preferred_over_bodt_when_both_present():
    rec = _arma(bod2=struct.pack("<II", _mask(30), 1),
                bodt=struct.pack("<IB3xI", _mask(60), 0, 1),
                mod2=b"x.nif\0")
    slots, _ = parse_arma(rec)
    assert slots == frozenset({30})


def test_no_mask_or_no_model_path_gives_none():
    assert parse_arma(_arma(mod2=b"x.nif\0")) is None                  # no slot data at all
    assert parse_arma(_arma(bod2=struct.pack("<II", _mask(32), 1))) is None   # no model path


def test_multiple_model_subrecords_each_get_the_mask():
    rec = _arma(bod2=struct.pack("<II", _mask(32), 1),
                mod2=b"Male\\a.nif\0", mod3=b"Female\\a.nif\0")
    slots, paths = parse_arma(rec)
    assert sorted(paths) == ["meshes/female/a.nif", "meshes/male/a.nif"]
    assert slots == frozenset({32})


# -- build_slot_index: a fabricated profile end to end -------------------------------
def _write_plugin(path: Path, arma_records: bytes) -> None:
    from test_esp_records import _grup, _plugin
    path.write_bytes(_plugin(_grup("ARMA", arma_records)))


def _write_arma_record(formid: int, slots: "tuple[int, ...]", model: bytes) -> bytes:
    from test_esp_records import _rec, _sub
    subs = _sub("BOD2", struct.pack("<II", _mask(*slots), 1)) + _sub("MOD2", model)
    return _rec("ARMA", formid, subs)


def test_build_slot_index_over_a_fabricated_profile(tmp_path):
    from Utils.filemap import _write_mod_index

    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Boots Mod"
    mod_a.mkdir(parents=True)
    _write_plugin(mod_a / "BootsMod.esp",
                  _write_arma_record(1, (37, 38), b"Male\\boots.nif\0"))

    class FakeGame:
        def get_mod_data_path(self):
            return tmp_path / "Data"                    # no vanilla plugins for this test

        def get_effective_mod_staging_path(self):
            return mods_dir

    (tmp_path / "plugins.txt").write_text("*BootsMod.esp\n", encoding="utf-8")
    (tmp_path / "modlist.txt").write_text("*Boots Mod\n", encoding="utf-8")
    _write_mod_index(tmp_path / "modindex.bin",
                     {"Boots Mod": ({"bootsmod.esp": "BootsMod.esp"}, {})})

    index = build_slot_index(FakeGame(), tmp_path)
    assert index == {"meshes/male/boots.nif": frozenset({37, 38})}


def test_build_slot_index_unions_slots_from_later_plugins(tmp_path):
    from Utils.filemap import _write_mod_index

    mods_dir = tmp_path / "mods"
    for name, formid, slots in (("Mod A", 1, (32,)), ("Mod B", 2, (34,))):
        d = mods_dir / name
        d.mkdir(parents=True)
        _write_plugin(d / f"{name.replace(' ', '')}.esp",
                      _write_arma_record(formid, slots, b"shared.nif\0"))

    class FakeGame:
        def get_mod_data_path(self):
            return tmp_path / "Data"

        def get_effective_mod_staging_path(self):
            return mods_dir

    (tmp_path / "plugins.txt").write_text("*ModA.esp\n*ModB.esp\n", encoding="utf-8")
    (tmp_path / "modlist.txt").write_text("*Mod B\n*Mod A\n", encoding="utf-8")   # B higher priority
    _write_mod_index(tmp_path / "modindex.bin", {
        "Mod A": ({"moda.esp": "ModA.esp"}, {}),
        "Mod B": ({"modb.esp": "ModB.esp"}, {}),
    })

    index = build_slot_index(FakeGame(), tmp_path)
    assert index == {"meshes/shared.nif": frozenset({32, 34})}


def test_build_slot_index_with_no_profile_dir_is_empty():
    assert build_slot_index(object(), None) == {}


# -- optional: a slice of the real game ---------------------------------------------
_DATA = Path.home() / "games/steamapps/common/Skyrim Special Edition/Data_Core"


@pytest.mark.skipif(not (_DATA / "Skyrim.esm").is_file(), reason="needs a Skyrim SE install")
def test_real_vanilla_iron_armor_slots_match_the_engine():
    from Utils.plugins.esp_records import read_records

    recs = {r.sub("EDID").rstrip(b"\0").decode(): r
            for r in read_records(_DATA / "Skyrim.esm", {"ARMA"})
            if r.sub("EDID")}
    cases = {
        "IronCuirassAA": frozenset({32, 34, 38}),
        "IronBootsAA": frozenset({37, 38}),
        "IronHelmetAA": frozenset({31, 43}),
        "IronShieldAA": frozenset({39}),
    }
    for edid, want in cases.items():
        slots, _paths = parse_arma(recs[edid])
        assert slots == want, edid

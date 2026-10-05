"""Shared record-info helpers: the humanize/keyword-prefix-classification
pattern used by both Armor Type's FO4 fallback and Weapon Type, equip-slot
text formatting, and localized/inline FULL-name reading."""
from __future__ import annotations

import struct

from Utils.plugins.esp_records import Record
from Utils.plugins.record_info import classify_by_keyword_prefix, format_slots, humanize, read_edid_full


def test_humanize_splits_camel_case_into_title_case_words():
    assert humanize("WarAxe") == "War Axe"
    assert humanize("Battleaxe") == "Battleaxe"
    assert humanize("Power") == "Power"


def test_humanize_handles_digits_and_empty_input():
    assert humanize("Scope2x") == "Scope 2 X"
    assert humanize("") == ""


def test_classify_by_keyword_prefix_matches_and_humanizes():
    assert classify_by_keyword_prefix(["ArmorTypePower"], "ArmorType") == "Power"
    assert classify_by_keyword_prefix(["WeaponTypePistol"], "WeaponType") == "Pistol"


def test_classify_by_keyword_prefix_is_case_insensitive():
    assert classify_by_keyword_prefix(["armortypepower"], "ArmorType") == "Power"


def test_classify_by_keyword_prefix_returns_none_when_nothing_matches():
    assert classify_by_keyword_prefix(["VendorItemArmor"], "ArmorType") is None
    assert classify_by_keyword_prefix([], "ArmorType") is None


def test_classify_by_keyword_prefix_does_not_cross_match_different_prefixes():
    # WeapType and WeaponType are genuinely different Bethesda conventions
    # (Skyrim vs Fallout 4) -- confirmed real data, not interchangeable.
    assert classify_by_keyword_prefix(["WeaponTypePistol"], "WeapType") is None


def test_format_slots_with_labels_and_without():
    assert format_slots(frozenset(), {32: "Body"}) == "—"
    assert format_slots(frozenset({32}), {32: "Body"}) == "32 (Body)"
    assert format_slots(frozenset({32, 34}), {32: "Body"}) == "32 (Body), 34"
    assert format_slots(frozenset({34}), None) == "34"


def _tes4(flags: int) -> bytes:
    return struct.pack("<4sIII8x", b"TES4", 0, flags, 0)


def test_read_edid_full_inline_name():
    rec = Record("ARMO", 1, {"EDID": [b"Foo\0"], "FULL": [b"Foo Name\0"]})
    assert read_edid_full(rec, localized=False, strings={}) == ("Foo", "Foo Name")


def test_read_edid_full_localized_name_resolved_through_strings():
    rec = Record("ARMO", 1, {"EDID": [b"Foo\0"], "FULL": [struct.pack("<I", 42)]})
    assert read_edid_full(rec, localized=True, strings={42: "Resolved Name"}) == ("Foo", "Resolved Name")


def test_read_edid_full_localized_name_blank_when_unresolved():
    rec = Record("ARMO", 1, {"EDID": [b"Foo\0"], "FULL": [struct.pack("<I", 999)]})
    assert read_edid_full(rec, localized=True, strings={}) == ("Foo", "")


def test_read_edid_full_missing_edid_or_full():
    rec = Record("ARMO", 1, {})
    assert read_edid_full(rec, localized=False, strings={}) == ("", "")

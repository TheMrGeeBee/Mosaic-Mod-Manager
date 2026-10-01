"""Tests for gui_qt.modlist.modlist_data.read_meta_for_entries' TW3
collection-order flag (FLAG_COLLECTION_ORDER_LOCKED).

A mod whose priority is set by a Nexus Collection's own loadOrder (see
Utils.mods.tw3_load_index.collection_governed_mod_names) gets flagged in
the main Mod List so dragging it there doesn't silently fight the
curator's order without the user realizing why.
"""
from __future__ import annotations

from pathlib import Path

from gui_qt.modlist.modlist_data import read_meta_for_entries, FLAG_COLLECTION_ORDER_LOCKED
from Utils.mods.modlist import ModEntry


def _write_meta_ini(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[General]\ninstalled = 2026-01-01\n", encoding="utf-8")


def _entry(name: str) -> ModEntry:
    return ModEntry(name=name, enabled=True, locked=False)


def test_mod_in_collection_order_mods_gets_flagged(tmp_path):
    staging = tmp_path / "staging"
    _write_meta_ini(staging / "Governed Mod" / "meta.ini")
    _write_meta_ini(staging / "Free Mod" / "meta.ini")

    _v, _i, flags, *_rest = read_meta_for_entries(
        [_entry("Governed Mod"), _entry("Free Mod")], staging,
        collection_order_mods=frozenset({"Governed Mod"}))

    assert flags.get("Governed Mod", 0) & FLAG_COLLECTION_ORDER_LOCKED
    assert not (flags.get("Free Mod", 0) & FLAG_COLLECTION_ORDER_LOCKED)


def test_default_empty_set_flags_nothing(tmp_path):
    staging = tmp_path / "staging"
    _write_meta_ini(staging / "Some Mod" / "meta.ini")

    _v, _i, flags, *_rest = read_meta_for_entries(
        [_entry("Some Mod")], staging)

    assert not (flags.get("Some Mod", 0) & FLAG_COLLECTION_ORDER_LOCKED)

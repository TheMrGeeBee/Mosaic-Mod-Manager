"""Localized .STRINGS table reading: loose Data/Strings/ files and the
BSA/BA2-packed case (confirmed the common one on a real install -- Skyrim SE
ships Strings/Skyrim_English.STRINGS inside Skyrim - Interface.bsa, not
loose), plus the TES4 localization-flag check and the per-game English-token
fallback (Skyrim/Oblivion/FO3/NV use "English", Fallout 4 uses "en" --
confirmed against a real Fallout4 - Interface.ba2)."""
from __future__ import annotations

import struct

import pytest

from Utils.archives.bsa_writer import write_bsa
from Utils.plugins.string_table import is_localized, load_strings_table


def _tes4(flags: int) -> bytes:
    return struct.pack("<4sIII8x", b"TES4", 0, flags, 0)


def _strings_blob(entries: dict) -> bytes:
    """{string_id: text} -> a minimal .STRINGS file (header, directory, blob)."""
    blob = b""
    offsets = {}
    for sid, text in entries.items():
        offsets[sid] = len(blob)
        blob += text.encode("utf-8") + b"\0"
    header = struct.pack("<II", len(entries), len(blob))
    directory = b"".join(struct.pack("<II", sid, off) for sid, off in offsets.items())
    return header + directory + blob


def test_is_localized_reads_the_tes4_header_flag(tmp_path):
    loc = tmp_path / "Loc.esp"
    loc.write_bytes(_tes4(0x80))
    not_loc = tmp_path / "NotLoc.esp"
    not_loc.write_bytes(_tes4(0x00))
    assert is_localized(loc) is True
    assert is_localized(not_loc) is False


def test_is_localized_is_false_for_missing_or_non_tes4_files(tmp_path):
    assert is_localized(tmp_path / "missing.esp") is False
    junk = tmp_path / "junk.esp"
    junk.write_bytes(b"not a plugin")
    assert is_localized(junk) is False


def test_load_strings_table_reads_a_loose_file(tmp_path):
    plugin = tmp_path / "Skyrim.esm"
    plugin.write_bytes(_tes4(0x80))
    strings_dir = tmp_path / "Strings"
    strings_dir.mkdir()
    (strings_dir / "Skyrim_English.STRINGS").write_bytes(
        _strings_blob({100: "Studded Armor", 200: "Iron Sword"}))
    table = load_strings_table(plugin)
    assert table == {100: "Studded Armor", 200: "Iron Sword"}


def test_load_strings_table_falls_back_to_a_bsa_packed_file(tmp_path):
    # The common real-world case: no loose Strings/ directory at all, the
    # table is packed inside one of the plugin's own base archives instead.
    plugin = tmp_path / "Skyrim.esm"
    plugin.write_bytes(_tes4(0x80))
    src = tmp_path / "src"
    (src / "strings").mkdir(parents=True)
    (src / "strings" / "skyrim_english.strings").write_bytes(
        _strings_blob({1: "Hello"}))
    write_bsa(tmp_path / "Skyrim - Interface.bsa", src, version=105, compress=False)
    table = load_strings_table(plugin)
    assert table == {1: "Hello"}


def test_load_strings_table_tries_fallout4s_short_language_code(tmp_path):
    plugin = tmp_path / "Fallout4.esm"
    plugin.write_bytes(_tes4(0x80))
    strings_dir = tmp_path / "Strings"
    strings_dir.mkdir()
    # No "Fallout4_English.STRINGS" -- only the short "_en" form exists, as
    # on a real Fallout 4 install.
    (strings_dir / "Fallout4_en.STRINGS").write_bytes(_strings_blob({5: "Combat Armor"}))
    table = load_strings_table(plugin)
    assert table == {5: "Combat Armor"}


def test_load_strings_table_is_empty_when_nothing_matches(tmp_path):
    plugin = tmp_path / "NoStrings.esm"
    plugin.write_bytes(_tes4(0x80))
    assert load_strings_table(plugin) == {}

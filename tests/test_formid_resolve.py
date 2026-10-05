"""FormID identity resolution (cross-plugin "which plugin+local-id does this
raw FormID mean") and display formatting (the Base Form ID string, including
the FE-compressed form ESL-flagged plugins use)."""
from __future__ import annotations

from Utils.plugins.formid_resolve import build_display_order, display_formid, resolve


def test_resolve_indexes_into_the_referencing_plugins_own_masters():
    masters = ["Skyrim.esm", "Update.esm"]
    assert resolve(0x00012345, masters, "MyMod.esp") == ("skyrim.esm", 0x012345)
    assert resolve(0x0100ABCD, masters, "MyMod.esp") == ("update.esm", 0x00ABCD)


def test_resolve_falls_back_to_self_when_high_byte_is_this_files_own_index():
    masters = ["Skyrim.esm"]
    assert resolve(0x01000042, masters, "MyMod.esp") == ("mymod.esp", 0x000042)


def test_resolve_falls_back_to_self_on_out_of_range_high_byte():
    # Malformed/corrupt data — never crash, best-effort like the rest of this reader.
    assert resolve(0xFF000001, [], "MyMod.esp") == ("mymod.esp", 0x000001)


def test_display_order_splits_full_and_esl_plugins_into_separate_sequences():
    names = ["Skyrim.esm", "Light.esl", "Regular.esp", "AnotherLight.esp"]
    is_esl = {"light.esl": True, "anotherlight.esp": True}.get
    full_pos, esl_pos = build_display_order(names, lambda n: bool(is_esl(n.lower())))
    assert full_pos == {"skyrim.esm": 0, "regular.esp": 1}
    assert esl_pos == {"light.esl": 0, "anotherlight.esp": 1}


def test_display_formid_for_a_full_plugin_uses_two_hex_load_order_position():
    full_pos = {"skyrim.esm": 0, "mymod.esp": 5}
    assert display_formid("MyMod.esp", 0x1B3A2, full_pos, {}) == "0501B3A2"


def test_display_formid_for_an_esl_plugin_uses_fe_prefix_and_twelve_bit_local_id():
    esl_pos = {"light.esl": 2}
    # Local id is masked to 12 bits — ESL records are capped at 0x000-0xFFF.
    assert display_formid("Light.esl", 0x1A3C, {}, esl_pos) == "FE002A3C"


def test_display_formid_falls_back_to_bare_local_id_if_plugin_is_unknown():
    assert display_formid("Unlisted.esp", 0x1234, {}, {}) == "001234"

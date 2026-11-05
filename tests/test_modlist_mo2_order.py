"""MO2 files a separator *after* the mods it heads; Mosaic puts it before
them. ``mo2_to_natural_order`` is the conversion, and getting it wrong is not
subtle: the first real VtMB install showed every mod one heading too low.
"""

from __future__ import annotations

from Utils.mods.modlist import ModEntry, mo2_to_natural_order


def _mod(name, enabled=True):
    return ModEntry(name=name, enabled=enabled, locked=False)


def _sep(name):
    return ModEntry(name=f"{name}_separator", enabled=True, locked=True,
                    is_separator=True)


def _names(entries):
    return [e.name for e in entries]


def test_a_separator_moves_above_the_mods_it_heads():
    mo2 = [_mod("Art Texture Pack"), _mod("Bx Textures"), _sep("#Graphics Base")]
    assert _names(mo2_to_natural_order(mo2)) == [
        "#Graphics Base_separator", "Art Texture Pack", "Bx Textures"]


def test_every_group_lands_under_its_own_heading():
    """The exact shape that misgrouped on the real list: the Unofficial
    Patches sat above ``##Unofficial Patch`` in the file and Mosaic read them
    as belonging to ``##Antitribu``, the heading below."""
    mo2 = [_mod("Antitribu Overhaul"), _sep("##Antitribu"),
           _mod("Unofficial Patch Plus"), _mod("Unofficial Patch Basic"),
           _sep("##Unofficial Patch"),
           _sep("#Overhauls"),
           _mod("Art Texture Pack"), _sep("#Graphics Base")]
    assert _names(mo2_to_natural_order(mo2)) == [
        "##Antitribu_separator", "Antitribu Overhaul",
        "##Unofficial Patch_separator", "Unofficial Patch Plus",
        "Unofficial Patch Basic",
        "#Overhauls_separator",
        "#Graphics Base_separator", "Art Texture Pack"]


def test_mods_keep_their_order_so_priorities_do_not_move():
    mo2 = [_mod("A"), _mod("B"), _sep("One"), _mod("C"), _sep("Two"), _mod("D")]
    out = mo2_to_natural_order(mo2)
    assert [e.name for e in out if not e.is_separator] == ["A", "B", "C", "D"]
    assert len(out) == len(mo2)


def test_flags_and_objects_are_carried_through_untouched():
    disabled = _mod("Off", enabled=False)
    out = mo2_to_natural_order([disabled, _sep("Head")])
    assert out[1] is disabled and out[1].enabled is False
    assert out[0].is_separator and out[0].locked


def test_a_list_without_separators_is_unchanged():
    mo2 = [_mod("A"), _mod("B")]
    assert _names(mo2_to_natural_order(mo2)) == ["A", "B"]
    assert mo2_to_natural_order([]) == []


def test_an_empty_heading_stays_empty():
    mo2 = [_sep("Nothing Here"), _mod("A"), _sep("Head")]
    assert _names(mo2_to_natural_order(mo2)) == [
        "Nothing Here_separator", "Head_separator", "A"]


def test_mods_after_the_last_separator_stay_where_they_are():
    """MO2 leaves them ungrouped, but Mosaic's ungrouped float is at the
    opposite end of the list, so re-prioritising them to reach it would be a
    worse answer than letting them join the last group."""
    mo2 = [_mod("A"), _sep("Head"), _mod("B"), _mod("C")]
    assert _names(mo2_to_natural_order(mo2)) == [
        "Head_separator", "A", "B", "C"]

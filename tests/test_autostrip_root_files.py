"""Auto-stripping a wrapper folder must not discard root-level game content.

try_auto_strip_top_level drops every entry shallower than the strip depth. When
an archive holds BOTH root-level content and a nested Data-shaped folder, that
silently loses the root half. Observed on 'Requiem - Dragonborn Patch', where a
root .esp/.bsa were dropped in favour of a nested tool config; the discarded
plugin was a master for 24 other plugins.
"""
from __future__ import annotations

from Utils.mods.mod_install import (
    check_mod_top_level_file_types, try_auto_strip_top_level)

REQUIRED = {"meshes", "textures", "scripts", "interface", "sound", "data"}
EXTS = {".esp", ".esl", ".esm", ".ini", ".bsa", ".ba2"}


def fl(*paths):
    return [(p, p, False) for p in paths]


def dsts(file_list):
    return [d for _s, d, _f in file_list]


def test_a_plain_wrapper_folder_is_still_stripped():
    """The case auto-strip exists for must keep working."""
    out, did = try_auto_strip_top_level(
        fl("ModName/meshes/a.nif", "ModName/textures/b.dds"),
        REQUIRED, protected_exts=EXTS)
    assert did is True
    assert sorted(dsts(out)) == ["meshes/a.nif", "textures/b.dds"]


def test_root_plugin_is_not_discarded_for_a_nested_data_folder():
    """The reported case."""
    out, did = try_auto_strip_top_level(
        fl("Fozars_Dragonborn_-_Requiem_Patch.esp",
           "Fozars_Dragonborn_-_Requiem_Patch.bsa",
           "Reqtificator/Data/ActorAssignmentRules_x.esp.conf"),
        REQUIRED, protected_exts=EXTS)
    assert did is False, "must refuse rather than drop the root .esp/.bsa"
    assert len(out) == 3
    # and the caller's next branch recognises the root content
    assert check_mod_top_level_file_types(out, EXTS) is True


def test_root_bsa_alone_also_blocks_the_strip():
    out, did = try_auto_strip_top_level(
        fl("Thing.bsa", "Wrapper/meshes/a.nif"), REQUIRED, protected_exts=EXTS)
    assert did is False
    assert "Thing.bsa" in dsts(out)


def test_a_stray_readme_at_root_does_not_block_the_strip():
    """Junk at the root is not game content and must not prevent stripping."""
    out, did = try_auto_strip_top_level(
        fl("readme.txt", "ModName/meshes/a.nif"), REQUIRED, protected_exts=EXTS)
    assert did is True
    assert dsts(out) == ["meshes/a.nif"]


def test_a_valid_root_layout_returns_early_untouched():
    """When a required folder is already at the root the layout is acceptable,
    so the function returns it unchanged. (The real caller only invokes this
    after check_mod_top_level has failed, so the strip loop is not reached in
    this shape at all.)"""
    given = fl("textures/a.dds", "Wrapper/meshes/b.nif")
    out, did = try_auto_strip_top_level(given, REQUIRED, protected_exts=EXTS)
    assert did is True
    assert dsts(out) == dsts(given), "nothing may be discarded"


def test_without_protected_exts_behaviour_is_unchanged():
    """Callers that pass nothing keep the old semantics (a required folder at
    root still guards, since that check does not depend on extensions)."""
    out, did = try_auto_strip_top_level(
        fl("readme.txt", "ModName/meshes/a.nif"), REQUIRED)
    assert did is True
    assert dsts(out) == ["meshes/a.nif"]


def test_already_correct_layout_is_untouched():
    given = fl("meshes/a.nif", "textures/b.dds")
    out, did = try_auto_strip_top_level(given, REQUIRED, protected_exts=EXTS)
    assert did is True
    assert dsts(out) == dsts(given)


def test_two_levels_of_wrapper_still_strip():
    out, did = try_auto_strip_top_level(
        fl("Outer/Inner/meshes/a.nif"), REQUIRED, protected_exts=EXTS)
    assert did is True
    assert dsts(out) == ["meshes/a.nif"]

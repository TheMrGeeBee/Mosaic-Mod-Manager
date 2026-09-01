"""A collection author's FOMOD selections must survive a step-order mismatch.

Vortex records selections keyed by its manifest's ``options[]`` array index,
and that order need not match the FOMOD's ``installSteps`` order. When they
disagree every step receives the wrong step's groups, nothing matches, and all
of the author's selections are silently dropped — only ``requiredInstallFiles``
install. Observed on 'Skyrim Unbound Reborn': 34 recorded selections produced
2 files, removing two plugins that other mods declare as masters.
"""
from __future__ import annotations

from Utils.installers.fomod_installer import (
    _build_group_fallback, _selected_in_group, resolve_files)
from Utils.installers.fomod_parser import (
    FileInstall, Group, InstallStep, ModuleConfig, Plugin)


def plugin(name, source):
    """One plugin installing a single folder."""
    return Plugin(name=name,
                  files=[FileInstall(source=source, destination="",
                                     priority=0, is_folder=True)])


def group(name, plugins, gtype="SelectAny"):
    return Group(name=name, group_type=gtype, plugins=plugins)


def step(name, groups):
    return InstallStep(name=name, groups=groups)


def config_of(steps):
    return ModuleConfig(steps=steps)


def reversed_order_config():
    """FOMOD step 0 is 'Patches'; the manifest recorded Options/Addons first."""
    return config_of([
        step("Patches", [group("Other", [plugin("Audio Overhaul", "patches/AOS")])]),
        step("", [group("Addons", [plugin("Orc Strongholds", "addons/OrcStrongholds")])]),
    ])


def test_selections_survive_a_reversed_step_order():
    cfg = reversed_order_config()
    # index 0 = the Addons step, index 1 = the Patches step: reversed vs cfg
    sel = {"0": {"Addons": ["Orc Strongholds"]},
           "1": {"Other": ["Audio Overhaul"]}}
    got = {s for s, _d, _f in resolve_files(cfg, sel, set(), set(), set())}
    assert got == {"addons/OrcStrongholds", "patches/AOS"}


def test_matching_step_order_still_works():
    cfg = reversed_order_config()
    sel = {"0": {"Other": ["Audio Overhaul"]},
           "1": {"Addons": ["Orc Strongholds"]}}
    got = {s for s, _d, _f in resolve_files(cfg, sel, set(), set(), set())}
    assert got == {"addons/OrcStrongholds", "patches/AOS"}


def test_an_explicit_empty_group_is_respected_not_overridden():
    """"The author selected nothing in this group" must not fall back."""
    cfg = config_of([step("A", [group("G", [plugin("P", "src/P")])])])
    got = resolve_files(cfg, {"0": {"G": []}}, set(), set(), set())
    assert got == []


def test_ambiguous_group_names_are_never_cross_applied():
    """Two steps sharing a group name: the fallback must not guess."""
    cfg = config_of([
        step("One", [group("Shared", [plugin("A", "src/A")])]),
        step("Two", [group("Shared", [plugin("B", "src/B")])]),
    ])
    fb = _build_group_fallback(cfg, {"0": {"Shared": ["A"]}, "1": {"Shared": ["B"]}})
    assert fb == {}, "an ambiguous group name must not enter the fallback map"


def test_fallback_requires_uniqueness_on_both_sides():
    cfg = config_of([
        step("One", [group("Dup", [plugin("A", "src/A")])]),
        step("Two", [group("Dup", [plugin("B", "src/B")])]),
        step("Three", [group("Uniq", [plugin("C", "src/C")])]),
    ])
    fb = _build_group_fallback(cfg, {"0": {"Dup": ["A"], "Uniq": ["C"]}})
    assert "Dup" not in fb          # ambiguous in the config
    assert fb["Uniq"] == ["C"]      # unique on both sides


def test_selected_in_group_prefers_the_explicit_entry():
    assert _selected_in_group({"G": ["X"]}, "G", {"G": ["Y"]}) == {"X"}
    assert _selected_in_group({}, "G", {"G": ["Y"]}) == {"Y"}
    assert _selected_in_group({}, "G", {}) == set()


def test_empty_step_name_does_not_collide_with_an_index_key():
    """The real failure: one manifest step has an empty name, so name-keying
    alone falls back to the index and collides again."""
    cfg = config_of([
        step("Patches", [group("Other", [plugin("AOS", "patches/AOS")])]),
        step("", [group("Addons", [plugin("Bruma", "addons/Bruma")])]),
    ])
    sel = {"0": {"Addons": ["Bruma"]}, "Patches": {"Other": ["AOS"]}}
    got = {s for s, _d, _f in resolve_files(cfg, sel, set(), set(), set())}
    assert got == {"addons/Bruma", "patches/AOS"}

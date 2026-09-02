"""A recorded selection must not force an option the FOMOD says is unusable.

A plugin's <typeDescriptor> can resolve to NotUsable when a file it needs is
absent. A wizard user could not pick such an option; replaying a collection
author's recorded selection must not force it either, or its plugin installs
with a master that is not there.

Observed on 'Origins Reborn': the author selected "Alternate Perspective
Reborn - Origins", but the collection does not ship Alternate Perspective, so
the option is NotUsable and its .esp landed with a missing master.
"""
from __future__ import annotations

from Utils.installers.fomod_installer import (
    fomod_has_cross_mod_dependency, resolve_files, resolve_plugin_type)
from Utils.installers.fomod_parser import (
    Dependency, FileInstall, Group, InstallStep, ModuleConfig, Plugin,
    TypeDescriptor)


def file_dep(name):
    return Dependency(dep_type="file", operator="And", flag_name="",
                      flag_value="", file_name=name, file_state="Active",
                      sub_deps=[])


def unmatched_non_file_dep():
    """A non-file gate that does not match — the case the NotUsable leniency
    exists for. (A game/version gate would not do: evaluate_dependency treats
    an unevaluable version check as SATISFIED, matching MO2/Vortex, so it
    matches and never reaches the leniency.)"""
    return Dependency(dep_type="flag", operator="And", flag_name="neverSet",
                      flag_value="1", file_name="", file_state="Active",
                      sub_deps=[])


def plugin(name, source, td=None):
    return Plugin(name=name,
                  files=[FileInstall(source=source, destination="",
                                     priority=0, is_folder=True)],
                  type_descriptor=td or TypeDescriptor())


def gated_plugin(name, source, dep, outcome="Required"):
    return plugin(name, source, TypeDescriptor(
        plugin_type="NotUsable", is_conditional=True,
        default_type="NotUsable", patterns=[(dep, outcome)]))


def cfg(*plugins):
    return ModuleConfig(steps=[InstallStep(
        name="S", groups=[Group(name="G", group_type="SelectAny",
                                plugins=list(plugins))])])


SEL = {"0": {"G": ["gated"]}}


def test_selected_option_is_skipped_when_its_file_gate_is_unmet():
    c = cfg(gated_plugin("gated", "patch/AP", file_dep("AlternatePerspective.esp")))
    out = resolve_files(c, SEL, set(), set(), set())
    assert out == [], "an unusable option must not install just because it was recorded"


def test_selected_option_installs_once_its_file_gate_is_met():
    c = cfg(gated_plugin("gated", "patch/AP", file_dep("AlternatePerspective.esp")))
    files = {"alternateperspective.esp"}
    out = resolve_files(c, SEL, files, files, set())
    assert [s for s, _d, _f in out] == ["patch/AP"]


def test_type_resolves_notusable_rather_than_optional_for_a_file_gate():
    p = gated_plugin("gated", "patch/AP", file_dep("AlternatePerspective.esp"))
    assert resolve_plugin_type(p, {}, set(), set(), set()) == "NotUsable"
    files = {"alternateperspective.esp"}
    assert resolve_plugin_type(p, {}, files, files, set()) == "Required"


def test_an_unmatched_non_file_gate_still_gets_the_benefit_of_the_doubt():
    """Nothing here says the option is genuinely unusable, so it stays
    choosable rather than being silently dropped."""
    p = gated_plugin("gated", "patch/X", unmatched_non_file_dep())
    assert resolve_plugin_type(p, {}, set(), set(), set()) == "Optional"
    out = resolve_files(cfg(p), SEL, set(), set(), set())
    assert [s for s, _d, _f in out] == ["patch/X"]


def test_an_ordinary_option_is_unaffected():
    c = cfg(plugin("gated", "patch/plain"))
    out = resolve_files(c, SEL, set(), set(), set())
    assert [s for s, _d, _f in out] == ["patch/plain"]


def test_plugin_level_file_gate_counts_as_a_cross_mod_dependency():
    """Such a FOMOD must be deferred until siblings are installed, or the gate
    is evaluated against a half-built profile and a usable option is dropped."""
    c = cfg(gated_plugin("gated", "patch/AP", file_dep("AlternatePerspective.esp")))
    assert fomod_has_cross_mod_dependency(c) is True


def test_a_non_file_gate_alone_is_not_a_cross_mod_dependency():
    c = cfg(gated_plugin("gated", "patch/X", unmatched_non_file_dep()))
    assert fomod_has_cross_mod_dependency(c) is False

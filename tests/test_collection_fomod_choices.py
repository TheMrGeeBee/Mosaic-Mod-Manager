"""A collection's recorded FOMOD choices must survive the replay, and a pick
that can't be applied must be reported rather than dropped in silence.

The install log used to print the same "applying collection author's choices
automatically" line whether every recorded pick resolved or none of them did, so
a manifest that had drifted from the downloaded file looked identical to one
that matched. These cover the three ways a replay loses a pick — a renamed
option, a group a FOMOD forces an answer for, and an explicitly-empty group that
used to be dropped on the way in — plus the malformed-manifest guard.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from Utils.collections.collection_fomod_audit import (
    ENFORCED_GROUP_TYPES, audit_collection_manifest, convert_choices,
    resolve_recorded_choices)
from Utils.collections.collection_install import (
    _build_schema_index, _fomod_choices_from_collection, build_fomod_summary)
from Utils.installers.fomod_installer import resolve_files
from Utils.installers.fomod_parser import (
    Dependency, FileInstall, Group, InstallStep, ModuleConfig, Plugin,
    TypeDescriptor)


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def plugin(name, source, flags=None, ptype="Optional"):
    return Plugin(name=name,
                  files=[FileInstall(source=source, destination="",
                                     priority=0, is_folder=True)],
                  condition_flags=dict(flags or {}),
                  type_descriptor=TypeDescriptor(plugin_type=ptype))


def group(name, plugins, gtype="SelectAny"):
    return Group(name=name, group_type=gtype, plugins=plugins)


def step(name, groups, visible_condition=None):
    return InstallStep(name=name, groups=groups, visible_condition=visible_condition)


def manifest_choices(options):
    return {"type": "fomod", "options": options}


def m_group(name, picks):
    """A manifest group: *picks* is a list of (name, idx) or [] for "nothing"."""
    return {"name": name,
            "choices": [{"name": n, "idx": i} for n, i in picks]}


def sources(file_list):
    return {src for src, _dst, _folder in file_list}


# --------------------------------------------------------------------------
# an explicitly-empty group must stay explicit
# --------------------------------------------------------------------------
def test_a_group_the_author_left_empty_survives_the_conversion():
    """An ABSENT group falls back to the cross-step group-name map; an explicit
    empty one means "the author chose nothing here". Dropping empties on the way
    in collapsed the first case into the second."""
    got = convert_choices(manifest_choices([
        {"name": "Options", "groups": [m_group("Picked", [("A", 0)]),
                                       m_group("Left alone", [])]},
    ]))
    assert got == {"0": {"Picked": ["A"], "Left alone": []}}


def test_a_step_with_nothing_picked_still_gets_a_key():
    got = convert_choices(manifest_choices([
        {"name": "Nothing here", "groups": [m_group("G", [])]},
    ]))
    assert got == {"0": {"G": []}}


def test_the_installer_converter_is_the_same_implementation():
    choices = manifest_choices([{"name": "S", "groups": [m_group("G", [])]}])
    assert _fomod_choices_from_collection(choices) == convert_choices(choices)


def test_an_empty_group_does_not_pick_up_another_steps_picks():
    """Two steps, one group name each but the same name, one recorded and one
    deliberately empty: the empty one must install nothing."""
    cfg = ModuleConfig(steps=[
        step("First", [group("Variant", [plugin("Lite", "lite")])]),
        step("Second", [group("Variant", [plugin("Full", "full")])]),
    ])
    sel = convert_choices(manifest_choices([
        {"name": "First", "groups": [m_group("Variant", [("Lite", 0)])]},
        {"name": "Second", "groups": [m_group("Variant", [])]},
    ]))
    assert sources(resolve_files(cfg, sel, set(), set(), set())) == {"lite"}


# --------------------------------------------------------------------------
# idx rescues a renamed option; the name always wins when it matches
# --------------------------------------------------------------------------
def test_a_renamed_option_is_recovered_from_its_recorded_position():
    cfg = ModuleConfig(steps=[
        step("Main", [group("Version", [plugin("1.6.1170 (AE)", "ae"),
                                        plugin("1.5.97 (SE)", "se")])]),
    ])
    sel, findings = resolve_recorded_choices(cfg, manifest_choices([
        {"name": "Main", "groups": [m_group("Version", [("1.6.640 (AE)", 0)])]},
    ]))
    assert sel == {"0": {"Version": ["1.6.1170 (AE)"]}}
    assert [f.kind for f in findings] == ["idx-fallback"]
    assert not findings[0].is_warning
    assert sources(resolve_files(cfg, sel, set(), set(), set())) == {"ae"}


def test_a_matching_name_beats_a_disagreeing_idx():
    """Starfield's 'Jiggle Adjustments All in One' records four picks whose idx
    points at a different plugin than the name does. The name is the truth."""
    cfg = ModuleConfig(steps=[
        step("Main", [group("Jiggle", [plugin("Low", "low"),
                                       plugin("Medium", "med"),
                                       plugin("High", "high")])]),
    ])
    sel, findings = resolve_recorded_choices(cfg, manifest_choices([
        {"name": "Main", "groups": [m_group("Jiggle", [("Low", 2)])]},
    ]))
    assert sel == {"0": {"Jiggle": ["Low"]}}
    assert findings == []


def test_a_pick_that_resolves_to_nothing_is_reported_as_a_warning():
    cfg = ModuleConfig(steps=[
        step("Main", [group("Version", [plugin("Only option", "only")])]),
    ])
    sel, findings = resolve_recorded_choices(cfg, manifest_choices([
        {"name": "Main", "groups": [m_group("Version", [("Gone", 9)])]},
    ]))
    assert sel == {"0": {"Version": []}}
    miss = [f for f in findings if f.kind == "plugin-name-miss"]
    assert len(miss) == 1 and miss[0].is_warning
    assert "NOT applied" in str(miss[0])
    # nothing survived, so the "selects nothing at all" note rides along
    assert {f.kind for f in findings} == {"plugin-name-miss", "converts-empty"}


def test_a_group_the_step_does_not_have_is_reported():
    cfg = ModuleConfig(steps=[step("Main", [group("Here", [plugin("A", "a")])])])
    _sel, findings = resolve_recorded_choices(cfg, manifest_choices([
        {"name": "Main", "groups": [m_group("Elsewhere", [("A", 0)])]},
    ]))
    assert any(f.kind == "group-not-in-step" and f.is_warning for f in findings)


# --------------------------------------------------------------------------
# groups a FOMOD will not let the user leave empty
# --------------------------------------------------------------------------
def test_an_exclusive_group_with_no_recorded_pick_uses_the_fomod_default():
    """Vortex omits a group the author never touched. A SelectExactlyOne group
    cannot be left empty in the wizard, so replaying "nothing" installed no
    files at all from it."""
    cfg = ModuleConfig(steps=[
        step("Main", [group("Resolution",
                            [plugin("1K", "1k"),
                             plugin("2K", "2k", ptype="Recommended")],
                            gtype="SelectExactlyOne")]),
    ])
    sel = convert_choices(manifest_choices([
        {"name": "Main", "groups": [m_group("Resolution", [])]},
    ]))
    assert sources(resolve_files(cfg, sel, set(), set(), set())) == {"2k"}


def test_only_one_option_is_forced_into_a_select_exactly_one_group():
    cfg = ModuleConfig(steps=[
        step("Main", [group("Pick one", [plugin("A", "a"), plugin("B", "b")],
                            gtype="SelectExactlyOne")]),
    ])
    got = resolve_files(cfg, {"0": {"Pick one": []}}, set(), set(), set())
    assert len(got) == 1


def test_an_optional_group_left_empty_stays_empty():
    """SelectAny / SelectAtMostOne: "nothing" is a complete answer and forcing a
    default would install files the author deliberately left out."""
    cfg = ModuleConfig(steps=[
        step("Main", [group("Extras", [plugin("Extra", "extra")], gtype="SelectAny"),
                      group("Maybe", [plugin("Maybe", "maybe")],
                            gtype="SelectAtMostOne")]),
    ])
    assert resolve_files(cfg, {"0": {"Extras": [], "Maybe": []}},
                         set(), set(), set()) == []


def test_an_invisible_steps_empty_group_is_left_alone():
    """A FOMOD that branches on "which mod manager?" carries a full parallel set
    of steps for the branch nobody took, and the manifest correctly records
    nothing for those. Defaulting them would install both branches."""
    cfg = ModuleConfig(steps=[
        step("Manager", [group("Which", [plugin("MO2", "mo2", {"vortex": "Off"}),
                                         plugin("Vortex", "vx", {"vortex": "On"})],
                               gtype="SelectExactlyOne")]),
        step("MO2 branch",
             [group("Layout", [plugin("MO layout", "mo-layout")],
                    gtype="SelectExactlyOne")],
             visible_condition=Dependency(dep_type="flag", flag_name="vortex",
                                          flag_value="Off")),
        step("Vortex branch",
             [group("Layout", [plugin("Vortex layout", "vx-layout")],
                    gtype="SelectExactlyOne")],
             visible_condition=Dependency(dep_type="flag", flag_name="vortex",
                                          flag_value="On")),
    ])
    sel = convert_choices(manifest_choices([
        {"name": "Manager", "groups": [m_group("Which", [("Vortex", 1)])]},
        {"name": "MO2 branch", "groups": [m_group("Layout", [])]},
        {"name": "Vortex branch",
         "groups": [m_group("Layout", [("Vortex layout", 0)])]},
    ]))
    assert sources(resolve_files(cfg, sel, set(), set(), set())) == {"vx", "vx-layout"}


def test_only_visible_unrecorded_required_groups_are_reported():
    cfg = ModuleConfig(steps=[
        step("Visible", [group("Needed", [plugin("A", "a")],
                               gtype="SelectExactlyOne")]),
        step("Hidden", [group("Also needed", [plugin("B", "b")],
                              gtype="SelectAtLeastOne")],
             visible_condition=Dependency(dep_type="flag", flag_name="never",
                                          flag_value="On")),
    ])
    _sel, findings = resolve_recorded_choices(cfg, manifest_choices([
        {"name": "Visible", "groups": [m_group("Needed", [])]},
        {"name": "Hidden", "groups": [m_group("Also needed", [])]},
    ]))
    defaulted = [f for f in findings if f.kind == "required-defaulted"]
    assert [f.group for f in defaulted] == ["Needed"]
    assert all(not f.is_warning for f in defaulted)


def test_the_enforced_types_are_only_the_ones_a_wizard_forces():
    assert ENFORCED_GROUP_TYPES == ("SelectExactlyOne", "SelectAtLeastOne")


# --------------------------------------------------------------------------
# malformed manifests must not take the whole install down
# --------------------------------------------------------------------------
def _mod(file_id, choices):
    return {"name": f"Mod {file_id}", "domainName": "starfield", "phase": 0,
            "source": {"type": "nexus", "modId": 1, "fileId": file_id},
            "details": {}}


@pytest.mark.parametrize("ctype", ["fomod_selections", "bain_selections"])
def test_a_choices_block_with_no_selections_key_does_not_abort_the_install(ctype):
    """_build_schema_index is called unwrapped, so a KeyError here aborted the
    ENTIRE collection install over one malformed entry."""
    entry = _mod(1, None)
    entry["choices"] = {"type": ctype}
    idx = _build_schema_index({"mods": [entry]})
    assert idx.fomod_by_file_id.get(1, {}) == {}
    assert idx.bain_by_file_id.get(1, {}) == {}


def test_the_raw_choices_block_is_kept_for_apply_time_resolution():
    entry = _mod(1, None)
    entry["choices"] = manifest_choices([
        {"name": "Main", "groups": [m_group("G", [("A", 0)])]}])
    idx = _build_schema_index({"mods": [entry]})
    assert idx.fomod_raw_by_file_id[1] is entry["choices"]
    assert idx.fomod_by_file_id[1] == {"0": {"G": ["A"]}}


# --------------------------------------------------------------------------
# unattended: no recorded choices must not stop to ask
# --------------------------------------------------------------------------
def test_the_collection_installer_never_asks_about_an_unrecorded_fomod():
    """Any install_collection_archive call that offers the FOMOD wizard has to
    say what an unrecorded FOMOD should do instead, or one such mod stalls the
    whole collection on a modal dialog. Structural, like the hashes wiring: the
    inline, deferred and off-site paths are three separate call sites and the
    bug this guards against is one of them being forgotten."""
    import Utils.collections.collection_install as ci
    tree = ast.parse(inspect.getsource(ci))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", None))
             == "install_collection_archive"]
    assert calls
    offering_wizard = [c for c in calls
                       if "resolve_fomod" in {k.arg for k in c.keywords}]
    assert len(offering_wizard) >= 3, (
        "expected the inline, deferred-FOMOD and off-site call sites")
    offenders = [c.lineno for c in offering_wizard
                 if "fomod_no_choice_action" not in {k.arg for k in c.keywords}]
    assert not offenders, (
        f"install_collection_archive offers resolve_fomod without "
        f"fomod_no_choice_action at line(s) {offenders} — an unrecorded FOMOD "
        f"would open the wizard")


def test_the_defaults_action_bypasses_the_resolver():
    """The ladder must reach the defaults branch before the wizard branch."""
    import Utils.mods.mod_install as mi
    src = inspect.getsource(mi.install_collection_archive)
    defaults_at = src.index('elif fomod_no_choice_action == "defaults"')
    wizard_at = src.index("elif resolve_fomod is not None and has_steps:")
    assert defaults_at < wizard_at


def test_the_wizard_is_still_the_default_for_a_single_mod_install():
    import Utils.mods.mod_install as mi
    sig = inspect.signature(mi.install_collection_archive)
    p = sig.parameters["fomod_no_choice_action"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default == "wizard"


# --------------------------------------------------------------------------
# the end-of-install summary
# --------------------------------------------------------------------------
def test_the_summary_names_the_mods_the_collection_left_unspecified():
    s = build_fomod_summary({
        "Deadly Hazards": "author_choices",
        "Skyland AIO": "author_hashes",
        "Show Star Names": "defaults",
        "Flight Camera Fix": "defaults",
        "Hand-answered": "wizard",
    })
    assert s["counts"] == {"author_choices": 1, "author_hashes": 1,
                           "defaults": 2, "wizard": 1}
    assert s["unspecified"] == ["Flight Camera Fix", "Show Star Names"]
    assert s["answered_by_user"] == ["Hand-answered"]
    assert s["total"] == 5


def test_a_collection_without_fomods_produces_an_empty_summary():
    assert build_fomod_summary({})["total"] == 0


# --------------------------------------------------------------------------
# whole-manifest audit (what the CLI prints)
# --------------------------------------------------------------------------
def test_the_audit_compares_the_manifest_against_what_was_applied():
    entry = _mod(1, None)
    entry["choices"] = manifest_choices([
        {"name": "Main", "groups": [m_group("G", [("A", 0)])]}])
    manifest = {"mods": [entry]}

    matched = audit_collection_manifest(
        manifest, applied_selections=lambda _m: {"0": {"G": ["A"]}})
    assert matched[0].applied_matches is True

    diverged = audit_collection_manifest(
        manifest, applied_selections=lambda _m: {"0": {"G": ["B"]}})
    assert diverged[0].applied_matches is False


def test_an_empty_group_and_an_absent_one_compare_equal():
    """resolve_files treats them the same, and the manifest and the saved
    sidecar disagree on which they write — so the audit must not cry wolf."""
    entry = _mod(1, None)
    entry["choices"] = manifest_choices([
        {"name": "Main", "groups": [m_group("Picked", [("A", 0)]),
                                    m_group("Empty", [])]}])
    got = audit_collection_manifest(
        {"mods": [entry]}, applied_selections=lambda _m: {"0": {"Picked": ["A"]}})
    assert got[0].applied_matches is True


def test_mods_without_any_fomod_recording_are_not_audited():
    assert audit_collection_manifest({"mods": [_mod(1, None)]}) == []

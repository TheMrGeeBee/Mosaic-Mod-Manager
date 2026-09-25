"""BG3 load-order insights (Utils.mods.bg3_pak_index).

Pak records are built by hand in the same shape scan_pak() produces, so the
analysis, rule and modlist-move logic is tested without real .pak files.
"""
from __future__ import annotations

from Utils.mods import bg3_pak_index as bx
from Utils.mods.modlist import ModEntry, read_modlist, write_modlist


def _meta(uuid, name, deps=()):
    return {"uuid": uuid, "name": name, "folder": name, "version64": "1",
            "version": "", "md5": "", "publish_handle": "0", "mod_type": "",
            "dependencies": list(deps), "dependency_names": {},
            "is_override_only": False, "is_meta_only": False}


def _rec(uuid, name, *, stats=None, treasure=None, gui=(), files=(), deps=()):
    return {"meta": _meta(uuid, name, deps), "files": list(files),
            "stats": dict(stats or {}), "treasure": dict(treasure or {}),
            "gui": list(gui), "conflicts": []}


def _entries(*names):
    return [ModEntry(name=n, enabled=True, locked=False) for n in names]


# ---- parsing ----------------------------------------------------------------

def test_parse_stats_keys_by_type_and_name():
    text = ('new entry "Fireball"\ntype "SpellData"\ndata "Level" "3"\n\n'
            'new entry "Tough"\ntype "PassiveData"\n')
    out = bx._parse_stats(text)
    assert set(out) == {"SpellData:Fireball", "PassiveData:Tough"}


def test_parse_treasure_marks_merging_tables():
    text = ('new treasuretable "TUT_Chest_Potions"\nCanMerge 1\n'
            'new subtable "1,1"\n\n'
            'new treasuretable "MyOwnTable"\nnew subtable "1,1"\n')
    out = bx._parse_treasure(text)
    assert out["TUT_Chest_Potions"].startswith(bx.MERGE_PREFIX)
    assert not out["MyOwnTable"].startswith(bx.MERGE_PREFIX)


def test_can_collide_skips_mod_own_namespace():
    assert not bx._can_collide("Public/MyMod_1234/Stats/Generated/Data/Spell.txt")
    assert bx._can_collide("Public/Shared/Assets/Textures/Icons/icons_items_3.dds")
    assert bx._can_collide("Generated/Public/Shared/Assets/x.dds")
    assert bx._can_collide("Localization/English/english.loca")


# ---- analysis ---------------------------------------------------------------

def test_stats_override_winner_is_the_later_loaded_mod(tmp_path):
    # modlist.txt top = highest priority = loads last in modsettings.
    enabled = _entries("NPC Overhaul", "Combat Extender")
    index = {
        "NPC Overhaul": [_rec("u-npc", "NPC", stats={"Character:Bandit": "a"})],
        "Combat Extender": [_rec("u-ce", "CE", stats={"Character:Bandit": "b"})],
    }
    findings, rank = bx.analyse(enabled, index, tmp_path)
    [f] = findings
    assert f.kind == "stats_override"
    assert f.keys == ["Character:Bandit"]
    assert f.winner == "NPC Overhaul"
    assert rank["NPC Overhaul"] > rank["Combat Extender"]


def test_identical_definitions_are_reported_as_harmless(tmp_path):
    enabled = _entries("A", "B")
    index = {"A": [_rec("ua", "A", stats={"StatusData:X": "same"})],
             "B": [_rec("ub", "B", stats={"StatusData:X": "same"})]}
    [f] = bx.analyse(enabled, index, tmp_path)[0]
    assert f.kind == "identical"


def test_merging_treasure_tables_are_not_a_conflict(tmp_path):
    m = bx.MERGE_PREFIX
    enabled = _entries("A", "B", "C")
    index = {"A": [_rec("ua", "A", treasure={"TUT_Chest": m + "1"})],
             "B": [_rec("ub", "B", treasure={"TUT_Chest": m + "2"})],
             "C": [_rec("uc", "C", treasure={"Own": "3"})]}
    assert bx.analyse(enabled, index, tmp_path)[0] == []


def test_replacing_treasure_table_is_a_conflict(tmp_path):
    enabled = _entries("A", "B")
    index = {"A": [_rec("ua", "A", treasure={"TUT_Chest": bx.MERGE_PREFIX + "1"})],
             "B": [_rec("ub", "B", treasure={"TUT_Chest": "2"})]}
    [f] = bx.analyse(enabled, index, tmp_path)[0]
    assert f.kind == "treasure_table"


def test_gui_state_winner_is_the_later_loaded_mod(tmp_path):
    # Confirmed in-game: ACS's inventory (with its Camp Chest button) shows
    # only while ACS loads after BCPP, which replaces the same screen.
    enabled = _entries("ACS", "BCPP")
    index = {"ACS": [_rec("u1", "ACS", gui=["Keyboard:CharacterPanel"])],
             "BCPP": [_rec("u2", "BCPP", gui=["Keyboard:CharacterPanel"])]}
    [f] = bx.analyse(enabled, index, tmp_path)[0]
    assert f.kind == "gui_state" and f.winner == "ACS"


def test_variant_group_from_info_json(tmp_path):
    for mod in ("Hair 4k", "Hair 2k"):
        (tmp_path / mod).mkdir()
        (tmp_path / mod / "info.json").write_text(
            '{"Mods":[{"Group":"g-1"}]}', encoding="utf-8")
    enabled = _entries("Hair 4k", "Hair 2k")
    index = {"Hair 4k": [_rec("u1", "H4")], "Hair 2k": [_rec("u2", "H2")]}
    [f] = bx.analyse(enabled, index, tmp_path)[0]
    assert f.kind == "variant_group"


# ---- rules + modlist moves -------------------------------------------------

def _profile(tmp_path, names):
    write_modlist(tmp_path / "modlist.txt", _entries(*names))
    return tmp_path


def test_apply_winner_moves_winner_above_losers_and_saves_rule(tmp_path):
    prof = _profile(tmp_path, ["Loser", "Other", "Winner"])
    f = bx.Finding(kind="stats_override", mods=["Loser", "Winner"],
                   keys=["k"], winner="Loser")
    bx.apply_winner(prof, f, "Winner")
    assert [e.name for e in read_modlist(prof / "modlist.txt")] == \
        ["Winner", "Loser", "Other"]
    rules = bx.read_rules(prof)["rules"]
    assert rules == [{"winner": "Winner", "loser": "Loser",
                      "reason": "stats_override"}]


def test_apply_winner_refuses_to_break_a_dependency(tmp_path):
    prof = _profile(tmp_path, ["Patch", "Base"])
    f = bx.Finding(kind="stats_override", mods=["Patch", "Base"],
                   keys=["k"], winner="Patch")
    try:
        bx.apply_winner(prof, f, "Base", {"Patch": {"Base"}})
    except bx.RuleConflict:
        pass
    else:
        raise AssertionError("expected RuleConflict")
    assert [e.name for e in read_modlist(prof / "modlist.txt")] == \
        ["Patch", "Base"]


def test_rule_status_flags_violation(tmp_path):
    f = bx.Finding(kind="stats_override", mods=["A", "B"], keys=["k"],
                   winner="A")
    bx._apply_rule_status([f], [{"winner": "B", "loser": "A"}],
                          {"A": 5, "B": 2})
    assert f.resolved_by_rule and f.rule_violated


def test_ignore_finding_persists(tmp_path):
    prof = _profile(tmp_path, ["A", "B"])
    f = bx.Finding(kind="same_file", mods=["A", "B"], keys=["x"], winner=None)
    bx.ignore_finding(prof, f)
    assert bx.read_rules(prof)["ignored"] == [f.id]


def test_patch_that_depends_on_the_loser_is_intended(tmp_path):
    # "CX patch" depends on Combat Extender and overrides its entries on
    # purpose (the real Ultimate NPC Stat Overhaul - CX case).
    enabled = _entries("CX patch", "Combat Extender")
    index = {
        "CX patch": [_rec("u-p", "P", stats={"Character:Bandit": "a"},
                          deps=["u-ce"])],
        "Combat Extender": [_rec("u-ce", "CE", stats={"Character:Bandit": "b"})],
    }
    [f] = bx.analyse(enabled, index, tmp_path)[0]
    assert f.winner == "CX patch" and f.intended
    ins = bx.Insights(findings=[f])
    assert bx.unresolved_count(ins) == 0


def test_patch_named_mod_is_suggested_not_auto_resolved(tmp_path):
    enabled = _entries("Awakened Morningstar (Compatibility Patch)",
                       "Morningstar", "Blood of Lathander")
    index = {
        "Awakened Morningstar (Compatibility Patch)": [_rec(
            "u-p", "Patch", stats={"StatusData:LIGHT": "p", "Weapon:W": "p"})],
        "Morningstar": [_rec("u-m", "M", stats={"StatusData:LIGHT": "m"})],
        "Blood of Lathander": [_rec("u-b", "B", stats={"Weapon:W": "b"})],
    }
    findings, _ = bx.analyse(enabled, index, tmp_path)
    assert len(findings) == 2
    assert all(f.suggested_patch == "Awakened Morningstar (Compatibility Patch)"
               and not f.intended for f in findings)
    assert bx.unresolved_count(bx.Insights(findings=findings)) == 2


def test_game_version_patch_name_is_not_a_patch():
    assert not bx._looks_like_patch("Gale's Gear by AzazeL (Patch 7-8)", [])
    assert not bx._looks_like_patch("Better Visuals (Patch 8)", [])
    assert bx._looks_like_patch("Patches_for_VFX_Heads-353-VFX5", [])
    assert bx._looks_like_patch("Better UI AiO Patch", [])


def test_accept_patch_applies_all_its_findings(tmp_path):
    prof = _profile(tmp_path, ["Morningstar", "Blood", "My Compat Patch"])
    f1 = bx.Finding(kind="stats_override", mods=["Morningstar", "My Compat Patch"],
                    keys=["a"], winner="Morningstar",
                    suggested_patch="My Compat Patch")
    f2 = bx.Finding(kind="stats_override", mods=["Blood", "My Compat Patch"],
                    keys=["b"], winner="Blood", suggested_patch="My Compat Patch")
    assert bx.accept_patch(prof, "My Compat Patch", [f1, f2]) == 2
    assert [e.name for e in read_modlist(prof / "modlist.txt")][0] == \
        "My Compat Patch"
    winners = {(r["winner"], r["loser"]) for r in bx.read_rules(prof)["rules"]}
    assert winners == {("My Compat Patch", "Morningstar"),
                       ("My Compat Patch", "Blood")}


def test_partial_rule_does_not_settle_a_multi_mod_finding():
    f = bx.Finding(kind="stats_override", mods=["Patch", "M", "B", "DnD"],
                   keys=["k"], winner="Patch")
    bx._apply_rule_status([f], [{"winner": "M", "loser": "DnD"}],
                          {"Patch": 9, "M": 5, "B": 4, "DnD": 1})
    assert not f.resolved_by_rule and not f.rule_violated


def test_rules_chain_transitively():
    f = bx.Finding(kind="stats_override", mods=["Extras", "CX", "DnD"],
                   keys=["k"], winner="Extras")
    bx._apply_rule_status([f], [{"winner": "Extras", "loser": "CX"},
                                {"winner": "CX", "loser": "DnD"}],
                          {"Extras": 9, "CX": 5, "DnD": 1})
    assert f.resolved_by_rule and not f.rule_violated


def test_loser_moves_below_mods_that_pull_the_winner_forward(tmp_path):
    # Goon's Library depends on the Fixer, so the Fixer loads just before the
    # library no matter where it sits; the loser must go below the library.
    prof = _profile(tmp_path, ["Fixer", "BS Instruments", "Other", "Goon's Library"])
    f = bx.Finding(kind="stats_override", mods=["Fixer", "BS Instruments"],
                   keys=["k"], winner="BS Instruments")
    deps = {"Goon's Library": {"Fixer"}}
    bx.apply_winner(prof, f, "Fixer", deps)
    order = [e.name for e in read_modlist(prof / "modlist.txt")]
    assert order == ["Fixer", "Other", "Goon's Library", "BS Instruments"]
    enabled = read_modlist(prof / "modlist.txt")
    index = {"Fixer": [_rec("u-f", "F")], "BS Instruments": [_rec("u-b", "B")],
             "Other": [_rec("u-o", "O")],
             "Goon's Library": [_rec("u-g", "G", deps=["u-f"])]}
    rank = bx.compute_load_rank(enabled, index)
    assert rank["Fixer"] > rank["BS Instruments"]


def test_transitive_dependent_loser_is_refused(tmp_path):
    prof = _profile(tmp_path, ["A", "Lib", "B"])
    f = bx.Finding(kind="stats_override", mods=["A", "B"], keys=["k"], winner="B")
    try:
        bx.apply_winner(prof, f, "A", {"Lib": {"A"}, "B": {"Lib"}})
    except bx.RuleConflict:
        return
    raise AssertionError("expected RuleConflict")


# ---- broken decisions / re-apply / tidy -------------------------------------

class _Game:
    def __init__(self, staging):
        self._staging = staging

    def get_effective_mod_staging_path(self):
        return self._staging


def _rules_profile(tmp_path, monkeypatch, order, index, rules):
    prof = _profile(tmp_path, order)
    bx.write_rules(prof, {"rules": rules, "ignored": []})
    monkeypatch.setattr(bx, "build_index", lambda *a, **k: index)
    return prof


def test_broken_rules_reports_an_imported_order_that_undoes_a_decision(tmp_path, monkeypatch):
    index = {"ACS": [_rec("u1", "ACS")], "BCPP": [_rec("u2", "BCPP")]}
    rule = {"winner": "ACS", "loser": "BCPP", "reason": "gui_state"}
    prof = _rules_profile(tmp_path, monkeypatch, ["ACS", "BCPP"], index, [rule])
    game = _Game(tmp_path / "mods")
    assert bx.broken_rules(game, prof) == []
    imported = _entries("BCPP", "ACS")          # BCPP now on top = loads last
    assert bx.broken_rules(game, prof, imported) == [rule]


def test_reapply_rules_restores_every_decision(tmp_path, monkeypatch):
    index = {"ACS": [_rec("u1", "ACS")], "BCPP": [_rec("u2", "BCPP")],
             "Other": [_rec("u3", "Other")]}
    rules = [{"winner": "ACS", "loser": "BCPP", "reason": "gui_state"}]
    prof = _rules_profile(tmp_path, monkeypatch, ["BCPP", "Other", "ACS"],
                          index, rules)
    game = _Game(tmp_path / "mods")
    monkeypatch.setattr(bx, "settle_modlist", lambda *a, **k: 0)
    applied, problems = bx.reapply_rules(game, prof)
    assert applied == 1 and problems == []
    assert bx.broken_rules(game, prof) == []
    assert [e.name for e in read_modlist(prof / "modlist.txt")] == \
        ["ACS", "BCPP", "Other"]


def test_ui_template_overlap_is_a_finding(tmp_path):
    # Better Inventory UI, ACS and BCPP all define CharacterInventoryTemplate
    # in GUI/Library; the later one draws the inventory (and the addon's
    # item-type badges only show when Better Inventory UI's version wins).
    def rec(uuid, name):
        r = _rec(uuid, name)
        r["gui_templates"] = ["Keyboard:CharacterInventoryTemplate"]
        return r
    enabled = _entries("ACS", "Better Inventory UI")
    index = {"ACS": [rec("u1", "ACS")], "Better Inventory UI": [rec("u2", "BIU")]}
    [f] = bx.analyse(enabled, index, tmp_path)[0]
    assert f.kind == "ui_template" and f.winner == "ACS"
    assert f.keys == ["Keyboard:CharacterInventoryTemplate"]


def test_gui_file_kind():
    assert bx._gui_file_kind("mods/x/gui/library/lib_controller.xaml") == "Controller"
    assert bx._gui_file_kind("mods/x/gui/library/bettersplit_c.xaml") == "Controller"
    assert bx._gui_file_kind("mods/x/gui/library/lib_keyboard.xaml") == "Keyboard"


def test_template_filter_ignores_plain_values():
    xaml = ('<ResourceDictionary>'
            '<ControlTemplate x:Key="CharacterInventoryTemplate"/>'
            '<Style x:Key="EquipmentSlotStyle" TargetType="Control"/>'
            '<sys:Double x:Key="EquipmentSlotSize">64</sys:Double>'
            '<SolidColorBrush x:Key="RowBG_d" Color="#000"/>'
            '<BitmapImage x:Key="Icon" UriSource="a.png"/>'
            '</ResourceDictionary>')
    keys = {k for el, k in bx._XAML_KEYED_EL_RE.findall(xaml)
            if el.rsplit(":", 1)[-1] in bx._TEMPLATE_ELEMENTS}
    assert keys == {"CharacterInventoryTemplate", "EquipmentSlotStyle"}


def test_keep_current_order_saves_winner_without_moving(tmp_path):
    prof = _profile(tmp_path, ["Better Context Menu", "Other", "ACS"])
    f = bx.Finding(kind="ui_template", mods=["Better Context Menu", "ACS"],
                   keys=["k"], winner="Better Context Menu")
    assert bx.keep_current_order(prof, f) == "Better Context Menu"
    assert [e.name for e in read_modlist(prof / "modlist.txt")] == \
        ["Better Context Menu", "Other", "ACS"]
    assert bx.read_rules(prof)["rules"] == [
        {"winner": "Better Context Menu", "loser": "ACS", "reason": "ui_template"}]


def test_keep_current_order_needs_a_known_winner(tmp_path):
    prof = _profile(tmp_path, ["A", "B"])
    f = bx.Finding(kind="ui_template", mods=["A", "B"], keys=["k"], winner=None)
    try:
        bx.keep_current_order(prof, f)
    except bx.RuleConflict:
        return
    raise AssertionError("expected RuleConflict")


def test_load_after_add_and_clear(tmp_path):
    prof = _profile(tmp_path, ["Addon", "BIU"])
    bx.add_load_after(prof, "Addon", "BIU")
    assert bx.load_after_of(prof, "Addon") == ["BIU"]
    assert bx.clear_load_after(prof, "Addon") == 1
    assert bx.load_after_of(prof, "Addon") == []


def test_same_module_by_file_name_and_uuid(tmp_path):
    # KAVT deliberately ships unique_tav.pak with Unique Tav's UUID; with both
    # enabled only the higher-priority copy is deployed.
    def rec(uuid, name):
        r = _rec(uuid, name)
        r["rel"] = "unique_tav.pak"
        return r
    enabled = _entries("Unique Tav Custom Appearance", "KAVT")
    index = {"Unique Tav Custom Appearance": [rec("u-ut", "unique_tav")],
             "KAVT": [rec("u-ut", "KAVT - Virtual Tav")]}
    [f] = [x for x in bx.analyse(enabled, index, tmp_path)[0] if x.kind == "same_module"]
    assert f.mods == ["Unique Tav Custom Appearance", "KAVT"]
    assert f.winner == "Unique Tav Custom Appearance"      # top of the list
    assert any(k.startswith("file: unique_tav.pak") for k in f.keys)


def test_same_uuid_different_file_has_no_known_winner(tmp_path):
    def rec(uuid, name, rel):
        r = _rec(uuid, name)
        r["rel"] = rel
        return r
    enabled = _entries("A", "B")
    index = {"A": [rec("same", "A", "a.pak")], "B": [rec("same", "B", "b.pak")]}
    [f] = [x for x in bx.analyse(enabled, index, tmp_path)[0] if x.kind == "same_module"]
    assert f.winner is None


def test_dividers_are_not_same_module(tmp_path):
    def div(uuid, rel):
        r = _rec(uuid, "divider")
        r["meta"]["is_meta_only"] = True
        r["rel"] = rel
        return r
    enabled = _entries("Dividers A", "Dividers B")
    index = {"Dividers A": [div("d1", "001.pak")], "Dividers B": [div("d1", "001.pak")]}
    assert [x for x in bx.analyse(enabled, index, tmp_path)[0]
            if x.kind == "same_module"] == []

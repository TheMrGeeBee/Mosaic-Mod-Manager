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


def test_gui_state_finding_claims_no_winner(tmp_path):
    enabled = _entries("CPCCE", "ACS")
    index = {"CPCCE": [_rec("u1", "CPCCE", gui=["Keyboard:CompanionsPanel"])],
             "ACS": [_rec("u2", "ACS", gui=["Keyboard:CompanionsPanel"])]}
    [f] = bx.analyse(enabled, index, tmp_path)[0]
    assert f.kind == "gui_state" and f.winner is None


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

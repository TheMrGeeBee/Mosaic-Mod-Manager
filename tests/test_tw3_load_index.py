"""Utils.mods.tw3_load_index -- TW3 Load Order Insights: surfaces when two
or more enabled mods ship the same staged file (the real, confirmed-live
mechanism behind the session's original bug report: five mods each
replacing game/player/playerWitcher.ws, invisible until the game's script
compiler threw errors for whichever ones lost).
"""
from __future__ import annotations

from Utils.mods import tw3_load_index as li
from Utils.mods.modlist import ModEntry, read_modlist, write_modlist


def _write_mod_file(staging, mod_name, relpath, content=b"x"):
    p = staging / mod_name / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _entries(*names):
    return [ModEntry(name=n, enabled=True, locked=False) for n in names]


# ---- build_index / _scan_mod -----------------------------------------------

def test_scan_strips_folder_id_from_the_conflict_key(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "ModA",
                    "modFoo/content/scripts/game/player/playerWitcher.ws", b"AAA")
    index = li.build_index(staging, ["ModA"])
    scan = index["ModA"]
    assert scan.folder_ids == {"modFoo"}
    # The folder_id segment is gone -- this is the real TW3 override path.
    assert "mods/content/scripts/game/player/playerwitcher.ws" in scan.files


def test_two_mods_under_different_folder_ids_still_collide_on_the_inner_path(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "ModA",
                    "modFoo/content/scripts/game/player/playerWitcher.ws", b"AAA")
    _write_mod_file(staging, "ModB",
                    "modBar/content/scripts/game/player/playerWitcher.ws", b"BBB")
    index = li.build_index(staging, ["ModA", "ModB"])
    key_a = next(iter(index["ModA"].files))
    key_b = next(iter(index["ModB"].files))
    assert key_a == key_b   # same inner path -> same conflict key


def test_bin_files_are_not_indexed_as_conflicts(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "ModA", "bin/x64_dx12/d3d11.dll", b"x")
    index = li.build_index(staging, ["ModA"])
    assert index["ModA"].files == {}


# ---- analyse ----------------------------------------------------------------

def test_two_mods_same_file_different_content_is_same_file_finding(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "ModA",
                    "modFoo/content/scripts/game/player/playerWitcher.ws", b"AAA")
    _write_mod_file(staging, "ModB",
                    "modBar/content/scripts/game/player/playerWitcher.ws", b"BBB")
    index = li.build_index(staging, ["ModA", "ModB"])
    write_modlist(tmp_path / "modlist.txt", _entries("ModA", "ModB"))
    enabled = _entries("ModA", "ModB")
    findings, rank = li.analyse(enabled, index, tmp_path / "modlist.txt")
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "same_file"
    assert set(f.mods) == {"ModA", "ModB"}
    assert f.severity == 1


def test_identical_content_is_the_identical_kind(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "ModA",
                    "modFoo/content/x.xml", b"same bytes")
    _write_mod_file(staging, "ModB",
                    "modBar/content/x.xml", b"same bytes")
    index = li.build_index(staging, ["ModA", "ModB"])
    write_modlist(tmp_path / "modlist.txt", _entries("ModA", "ModB"))
    findings, _ = li.analyse(_entries("ModA", "ModB"), index, tmp_path / "modlist.txt")
    assert findings[0].kind == "identical"
    assert findings[0].severity == 0


def test_a_file_only_one_mod_ships_is_not_a_finding(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "ModA", "modFoo/content/x.xml", b"x")
    index = li.build_index(staging, ["ModA"])
    write_modlist(tmp_path / "modlist.txt", _entries("ModA"))
    findings, _ = li.analyse(_entries("ModA"), index, tmp_path / "modlist.txt")
    assert findings == []


def test_winner_is_the_mod_highest_in_modlist(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "Top", "modFoo/content/x.xml", b"AAA")
    _write_mod_file(staging, "Bottom", "modBar/content/x.xml", b"BBB")
    index = li.build_index(staging, ["Top", "Bottom"])
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, _entries("Top", "Bottom"))
    findings, rank = li.analyse(_entries("Top", "Bottom"), index, modlist_path)
    assert findings[0].winner == "Top"
    assert rank["Top"] > rank["Bottom"]   # higher rank = wins, inverted convention


def test_findings_sort_same_file_before_identical(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "A1", "modA1/content/same.xml", b"same")
    _write_mod_file(staging, "A2", "modA2/content/same.xml", b"same")
    _write_mod_file(staging, "B1", "modB1/content/diff.xml", b"one")
    _write_mod_file(staging, "B2", "modB2/content/diff.xml", b"two")
    index = li.build_index(staging, ["A1", "A2", "B1", "B2"])
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, _entries("A1", "A2", "B1", "B2"))
    findings, _ = li.analyse(_entries("A1", "A2", "B1", "B2"), index, modlist_path)
    assert [f.kind for f in findings] == ["same_file", "identical"]


# ---- collection_mods / intended_by ------------------------------------------

def _manifest_entry(mod_id, prefix, file_id=0):
    entry = {"id": mod_id, "data": {"prefix": prefix}}
    if file_id:
        entry["fileId"] = file_id
    return entry


def test_collection_matched_mods_get_intended_by_collection_order(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "ModA",
                    "modfoo/content/x.xml", b"AAA")
    _write_mod_file(staging, "ModB",
                    "modbar/content/x.xml", b"BBB")
    index = li.build_index(staging, ["ModA", "ModB"])
    manifest = [_manifest_entry("modfoo", 1), _manifest_entry("modbar", 2)]
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, _entries("ModA", "ModB"))
    findings, _ = li.analyse(_entries("ModA", "ModB"), index, modlist_path,
                             manifest=manifest, file_ids={})
    assert findings[0].intended is True
    assert findings[0].intended_by == "collection order"


def test_collection_mods_matches_by_file_id_when_folder_name_mismatches(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "ModA", "modWeirdWrapper/content/x.xml", b"AAA")
    index = li.build_index(staging, ["ModA"])
    manifest = [_manifest_entry("expectedid", 5, file_id=999)]
    mods = li.collection_mods(index, manifest, file_ids={"ModA": 999})
    assert mods == {"ModA"}


def test_no_manifest_means_no_collection_mods(tmp_path):
    staging = tmp_path / "staging"
    _write_mod_file(staging, "ModA", "modfoo/content/x.xml", b"AAA")
    index = li.build_index(staging, ["ModA"])
    assert li.collection_mods(index, None, {}) == set()


# ---- rule persistence --------------------------------------------------------

def test_rules_round_trip_through_profile_state(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    data = li.read_rules(profile_dir)
    assert data == {"rules": [], "ignored": [], "never_together": []}
    li.write_rules(profile_dir, {"rules": [{"winner": "A", "loser": "B"}],
                                 "ignored": ["same_file:A|B"],
                                 "never_together": [["C", "D"]]})
    data = li.read_rules(profile_dir)
    assert data["rules"] == [{"winner": "A", "loser": "B"}]
    assert data["ignored"] == ["same_file:A|B"]
    assert data["never_together"] == [["C", "D"]]


def test_mark_and_unmark_never_together(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    f = li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A")
    li.mark_never_together(f, profile_dir)
    assert li.read_rules(profile_dir)["never_together"] == [["A", "B"]]
    li.mark_never_together(f, profile_dir)   # idempotent, no duplicate
    assert len(li.read_rules(profile_dir)["never_together"]) == 1
    li.unmark_never_together(f, profile_dir)
    assert li.read_rules(profile_dir)["never_together"] == []


def test_clear_decisions_wipes_everything(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    li.write_rules(profile_dir, {"rules": [{"winner": "A", "loser": "B"}],
                                 "ignored": ["x"], "never_together": [["A", "B"]]})
    li.clear_decisions(profile_dir)
    assert li.read_rules(profile_dir) == {"rules": [], "ignored": [], "never_together": []}


def test_ignore_finding_hides_it_from_compute_insights(tmp_path):
    staging = tmp_path / "staging"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    _write_mod_file(staging, "ModA", "modfoo/content/x.xml", b"AAA")
    _write_mod_file(staging, "ModB", "modbar/content/x.xml", b"BBB")
    write_modlist(profile_dir / "modlist.txt", _entries("ModA", "ModB"))

    class FakeGame:
        def get_effective_mod_staging_path(self):
            return staging

    insights = li.compute_insights(FakeGame(), profile_dir)
    assert len(insights.findings) == 1
    li.ignore_finding(profile_dir, insights.findings[0])
    insights2 = li.compute_insights(FakeGame(), profile_dir)
    assert insights2.findings == []
    assert len(insights2.ignored) == 1


# ---- apply_winner / keep_current_order --------------------------------------

def test_apply_winner_moves_winner_above_losers_in_modlist(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", _entries("Loser", "Middle", "Winner"))
    f = li.Finding(kind="same_file", mods=["Loser", "Winner"], keys=["k"], winner="Loser")
    li.apply_winner(profile_dir, f, "Winner")
    names = [e.name for e in read_modlist(profile_dir / "modlist.txt")]
    assert names.index("Winner") < names.index("Loser")


def test_apply_winner_saves_a_rule(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", _entries("A", "B"))
    f = li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A")
    li.apply_winner(profile_dir, f, "B")
    rules = li.read_rules(profile_dir)["rules"]
    assert rules == [{"winner": "B", "loser": "A", "reason": "same_file"}]


def test_apply_winner_blocked_when_both_mods_are_collection_governed(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    write_modlist(profile_dir / "modlist.txt", _entries("A", "B"))
    f = li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A")
    try:
        li.apply_winner(profile_dir, f, "B", collection={"A", "B"})
        assert False, "expected RuleConflict"
    except li.RuleConflict:
        pass


def test_keep_current_order_requires_a_known_winner(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    f = li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner=None)
    try:
        li.keep_current_order(profile_dir, f)
        assert False, "expected RuleConflict"
    except li.RuleConflict:
        pass


def test_keep_current_order_saves_the_existing_winner_as_a_rule(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    f = li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A")
    won = li.keep_current_order(profile_dir, f)
    assert won == "A"
    assert li.read_rules(profile_dir)["rules"] == [
        {"winner": "A", "loser": "B", "reason": "same_file"}]


# ---- unresolved_count ---------------------------------------------------------

def test_unresolved_count_excludes_identical_and_intended():
    insights = li.Insights(findings=[
        li.Finding(kind="identical", mods=["A", "B"], keys=["k"], winner="A"),
        li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A", intended=True),
        li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A"),
    ])
    assert li.unresolved_count(insights) == 1


def test_unresolved_count_excludes_never_together_and_rule_resolved():
    insights = li.Insights(findings=[
        li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A",
                  never_together=True),
        li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A",
                  resolved_by_rule=True),
        li.Finding(kind="same_file", mods=["A", "B"], keys=["k"], winner="A",
                  resolved_by_rule=True, rule_violated=True),
    ])
    assert li.unresolved_count(insights) == 1

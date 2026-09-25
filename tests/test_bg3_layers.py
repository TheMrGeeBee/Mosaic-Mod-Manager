"""BG3 built-in "Sort Load Order": layer classification and the layered sort.

Pak records are synthetic (same shape as bg3_pak_index.scan_pak), injected by
monkeypatching build_index; meta.ini files are real, in tmp_path.
"""
from __future__ import annotations

import json

from Utils.mods import bg3_layers as L
from Utils.mods import bg3_pak_index as bx
from Utils.mods import bg3_sort
from Utils.mods.modlist import ModEntry, read_modlist, write_modlist


def _rec(uuid, name, deps=(), mod_type="", tags=()):
    return {"meta": {"uuid": uuid, "name": name, "folder": name, "version64": "1",
                     "version": "", "md5": "", "publish_handle": "0",
                     "mod_type": mod_type, "dependencies": list(deps),
                     "dependency_names": {}, "is_override_only": False,
                     "is_meta_only": False, "tags": list(tags)},
            "files": [], "stats": {}, "treasure": {}, "gui": [], "conflicts": []}


# ---- classification ---------------------------------------------------------

def test_classify_priority():
    impui = [_rec("26922ba9-6018-5252-075d-7ff2ba6ed879", "ImpUI")]
    assert L.classify("ImpUI", impui, "Visuals",
                      known_layer="frameworks")[0] == "frameworks"   # known beats category
    assert L.classify("ImpUI", impui, "Visuals", override="misc",
                      known_layer="frameworks")[0] == "misc"         # your choice beats known
    assert L.classify("X", [_rec("u", "X")], "Visuals", override="late") == ("late", "your choice")
    assert L.classify("X", [_rec("u", "X", mod_type="Patch")], "Gameplay")[0] == "patches"
    assert L.classify("X", [_rec("u", "X", tags=["Library"])], "Gameplay")[0] == "libraries"
    assert L.classify("X", [_rec("u", "X")], "Companions")[0] == "story"
    assert L.classify("X", [_rec("u", "X")], "Something New")[0] == "misc"


def test_modio_tags_used_when_no_nexus_category():
    rec = [_rec("u", "Better Hotbar")]
    assert L.classify("Better Hotbar", rec, "", ["Quality of Life", "UI"]) == \
        ("frameworks", "mod.io tag: ui")
    assert L.classify("Earrings", rec, "", ["Customisation"])[0] == "visuals"
    assert L.classify("Nothing", rec, "", [])[0] == "misc"


def test_game_version_patch_name_is_not_a_patch():
    rec = [_rec("u", "Gear")]
    layer, _ = L.classify("Gale's Gear (Patch 7-8)", rec, "Companions",
                          looks_like_patch=bx._looks_like_patch)
    assert layer == "story"


def test_read_categories(tmp_path):
    (tmp_path / "meta.ini").write_text(
        "[General]\ncategoryname = \nmodiotags = UI, Patch 8 Tested\n", encoding="utf-8")
    assert L.read_categories(tmp_path) == ("", ["UI", "Patch 8 Tested"])


# ---- layered sort -----------------------------------------------------------

class _Game:
    def __init__(self, staging):
        self._staging = staging

    def get_effective_mod_staging_path(self):
        return self._staging


def _profile(tmp_path, monkeypatch, order, index, categories, rules=(), extra=()):
    staging = tmp_path / "mods"
    for mod, cat in categories.items():
        (staging / mod).mkdir(parents=True, exist_ok=True)
        (staging / mod / "meta.ini").write_text(
            f"[General]\ncategoryname = {cat}\n", encoding="utf-8")
    prof = tmp_path / "profile"
    prof.mkdir()
    write_modlist(prof / "modlist.txt",
                  [ModEntry(name=m, enabled=True, locked=False) for m in order]
                  + list(extra))
    bx.write_rules(prof, {"rules": list(rules), "ignored": []})
    monkeypatch.setattr(bx, "build_index", lambda *a, **k: index)
    return _Game(staging), prof


def _load_order(plan):
    return plan.load_order


def test_layers_order_the_list(tmp_path, monkeypatch):
    # modlist top = loads last; start from a deliberately wrong order.
    index = {"ImpUI": [_rec("26922ba9-6018-5252-075d-7ff2ba6ed879", "ImpUI")],
             "Hair": [_rec("u-h", "Hair")],
             "Class": [_rec("u-c", "Class")],
             "CF": [_rec("67fbbd53-7c7d-4cfa-9409-6d737b4d92a9", "CF")]}
    game, prof = _profile(tmp_path, monkeypatch, ["ImpUI", "CF", "Class", "Hair"],
                          index, {"ImpUI": "User Interface", "Hair": "Visuals",
                                  "Class": "Gameplay", "CF": "Utilities"})
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt")
    assert _load_order(plan) == ["ImpUI", "Class", "Hair", "CF"]
    bg3_sort.apply_plan(plan)
    assert [e.name for e in read_modlist(prof / "modlist.txt")] == \
        ["CF", "Hair", "Class", "ImpUI"]


def test_dependency_and_decision_beat_layers(tmp_path, monkeypatch):
    index = {"Lib": [_rec("u-l", "Lib", tags=["Library"])],
             "UIMod": [_rec("u-ui", "UIMod", deps=["u-l"])],     # UI needs Lib
             "BCPP": [_rec("u-b", "BCPP")],
             "ACS": [_rec("u-a", "ACS")]}
    rule = {"winner": "ACS", "loser": "BCPP", "reason": "gui_state"}
    game, prof = _profile(tmp_path, monkeypatch, ["ACS", "BCPP", "UIMod", "Lib"],
                          index, {"Lib": "Resources", "UIMod": "User Interface",
                                  "BCPP": "User Interface", "ACS": "User Interface"},
                          rules=[rule])
    lo = bg3_sort.compute_layered_plan(game, prof / "modlist.txt").load_order
    assert lo.index("Lib") < lo.index("UIMod")          # dependency first
    assert lo.index("BCPP") < lo.index("ACS")           # decision: ACS wins


def test_decision_contradicting_a_dependency_is_reported(tmp_path, monkeypatch):
    index = {"Base": [_rec("u-b", "Base")],
             "Addon": [_rec("u-a", "Addon", deps=["u-b"])]}
    rule = {"winner": "Base", "loser": "Addon", "reason": "stats_override"}
    game, prof = _profile(tmp_path, monkeypatch, ["Addon", "Base"], index,
                          {"Base": "Gameplay", "Addon": "Gameplay"}, rules=[rule])
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt")
    assert plan.load_order == ["Base", "Addon"]
    assert any("conflicts with a dependency" in u for u in plan.unresolved)


def test_non_sortable_entries_keep_their_slots(tmp_path, monkeypatch):
    index = {"Hair": [_rec("u-h", "Hair")], "Class": [_rec("u-c", "Class")],
             "Loose": [{"meta": None, "files": [], "stats": {}, "treasure": {},
                        "gui": [], "conflicts": []}]}
    game, prof = _profile(
        tmp_path, monkeypatch, ["Class", "Loose", "Hair"], index,
        {"Hair": "Visuals", "Class": "Gameplay", "Loose": "Visuals"},
        extra=[ModEntry(name="Off", enabled=False, locked=False)])
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt")
    names = [e.name for e in plan.new_entries]
    assert names[1] == "Loose" and names[3] == "Off"
    assert names[0] == "Hair" and names[2] == "Class"   # Visuals wins over Gameplay


def test_result_is_a_fixed_point_of_the_dependency_sort(tmp_path, monkeypatch):
    index = {"Lib": [_rec("u-l", "Lib", tags=["Library"])],
             "A": [_rec("u-a", "A", deps=["u-l"])],
             "B": [_rec("u-b", "B")]}
    game, prof = _profile(tmp_path, monkeypatch, ["Lib", "A", "B"], index,
                          {"Lib": "Resources", "A": "Visuals", "B": "Gameplay"})
    bg3_sort.apply_plan(bg3_sort.compute_layered_plan(game, prof / "modlist.txt"))
    monkeypatch.setattr(bg3_sort, "scan_mod_paks", lambda staging, enabled, **k: {
        rec["meta"]["uuid"]: bx._to_info(rec["meta"], m)
        for e in enabled for m in [e.name] for rec in index.get(m, [])
        if rec.get("meta")})
    again = bg3_sort.compute_sort_plan_for_modlist(game, prof / "modlist.txt")
    assert again.moves == []


def test_collection_mods_are_left_alone(tmp_path, monkeypatch):
    index = {"CollA": [_rec("u-ca", "CollA")], "CollB": [_rec("u-cb", "CollB")],
             "Mine1": [_rec("u-m1", "Mine1")], "Mine2": [_rec("u-m2", "Mine2")]}
    game, prof = _profile(tmp_path, monkeypatch, ["Mine1", "CollA", "Mine2", "CollB"],
                          index, {"CollA": "Visuals", "CollB": "Gameplay",
                                  "Mine1": "Gameplay", "Mine2": "Visuals"})
    (prof / "collection.json").write_text(json.dumps({"loadOrder": [
        {"data": {"uuid": "u-cb"}}, {"data": {"uuid": "u-ca"}}]}), encoding="utf-8")
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt")
    assert plan.collection_count == 2
    names = [e.name for e in plan.new_entries]
    assert names[1] == "CollA" and names[3] == "CollB"      # untouched slots
    assert names[0] == "Mine2" and names[2] == "Mine1"      # Visuals after Gameplay


def test_layer_override_is_saved_and_used(tmp_path, monkeypatch):
    index = {"A": [_rec("u-a", "A")], "B": [_rec("u-b", "B")]}
    game, prof = _profile(tmp_path, monkeypatch, ["A", "B"], index,
                          {"A": "Visuals", "B": "Gameplay"})
    L.set_override(prof, "A", "frameworks")
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt")
    assert plan.load_order == ["A", "B"]
    assert plan.layers["A"] == ("frameworks", "your choice")
    L.set_override(prof, "A", None)
    assert L.read_overrides(prof) == {}


def test_load_after_choice_is_respected_by_the_sort(tmp_path, monkeypatch):
    # The addon's own category would put it before Better Inventory UI.
    index = {"BIU": [_rec("u-b", "BIU")], "Addon": [_rec("u-a", "Addon")]}
    game, prof = _profile(tmp_path, monkeypatch, ["BIU", "Addon"], index,
                          {"BIU": "User Interface", "Addon": "User Interface"})
    assert bg3_sort.compute_layered_plan(game, prof / "modlist.txt").load_order \
        == ["Addon", "BIU"]
    bx.add_load_after(prof, "Addon", "BIU")
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt")
    assert plan.load_order == ["BIU", "Addon"]

"""BG3 known-rules list (Utils.mods.bg3_known_rules) and how the sorter and
Load Order Insights use it.

conftest.py keeps the loader offline and its cache in tmp_path.
"""
from __future__ import annotations

import json

from Utils.mods import bg3_known_rules as K
from Utils.mods import bg3_layers as L
from Utils.mods import bg3_pak_index as bx
from Utils.mods import bg3_sort
from Utils.mods.modlist import ModEntry, write_modlist


def _rec(uuid, name, deps=(), dep_versions=None, version64="36028797018963968"):
    return {"meta": {"uuid": uuid, "name": name, "folder": name,
                     "version64": version64, "version": "", "md5": "",
                     "publish_handle": "0", "mod_type": "",
                     "dependencies": list(deps), "dependency_names": {},
                     "dependency_versions": dict(dep_versions or {}),
                     "is_override_only": False, "is_meta_only": False, "tags": []},
            "files": [], "stats": {}, "treasure": {}, "gui": [],
            "gui_templates": [], "conflicts": []}


BIU_RULES = {"version": 1, "mods": [{
    "name": "Better Inventory UI", "match": {"uuids": ["u-biu"]},
    "after": [{"name": "Better Containers", "match": {"names": ["Better Containers"]}},
              {"name": "BCPP", "match": {"names": ["BCPP"]}}],
    "incompatible": [{"name": "Better Arrow Icons",
                      "match": {"names": ["Better Arrow Icons"]},
                      "note": "arrow icons overlap"}],
    "source": "https://example/4597"}]}


# ---- bundled file -----------------------------------------------------------

def test_bundled_file_is_valid():
    data = json.loads(K.BUNDLED_PATH.read_text(encoding="utf-8"))
    assert isinstance(data["version"], int) and data["mods"]
    for entry in data["mods"]:
        assert entry.get("match"), entry
        if "layer" in entry:
            assert entry["layer"] in L.LAYER_INDEX, entry


# ---- loading ----------------------------------------------------------------

def test_bad_cache_falls_back_to_bundled(tmp_path):
    K._cache_path().write_text("{not json", encoding="utf-8")
    assert K.load_rules(refresh=False)["mods"] == \
        json.loads(K.BUNDLED_PATH.read_text(encoding="utf-8"))["mods"]


def test_cache_used_only_when_its_version_is_higher():
    bundled_v = json.loads(K.BUNDLED_PATH.read_text(encoding="utf-8"))["version"]
    K._cache_path().write_text(json.dumps({"version": bundled_v, "mods": []}),
                               encoding="utf-8")
    assert K.load_rules(refresh=False)["mods"]            # bundled kept
    K._cache_path().write_text(json.dumps({"version": bundled_v + 1, "mods": []}),
                               encoding="utf-8")
    assert K.load_rules(refresh=False)["mods"] == []      # newer main copy


# ---- matching -----------------------------------------------------------------

def test_resolve_matches_by_uuid_and_name_fragment():
    index = {"Better Inventory UI": [_rec("u-biu", "Better Inventory UI")],
             "Better Containers (8x9)": [_rec("u-bc", "Better Containers 8x9")],
             "BCPP UW 6 chars 6x9 inventory": [_rec("u-b1", "BCPP UW 6 6x9")],
             "BCPP 16x9 4 chars": [_rec("u-b2", "BCPP 16x9")],
             "Better Arrow Icons": [_rec("u-arr", "Better Arrow Icons")]}
    r = K.resolve(BIU_RULES, index)
    befores = {first for first, then, _r, _s in r.edges if then == "Better Inventory UI"}
    assert befores == {"Better Containers (8x9)", "BCPP UW 6 chars 6x9 inventory",
                       "BCPP 16x9 4 chars"}
    assert [(m, o) for m, o, _n, _s in r.incompatible] == \
        [("Better Inventory UI", "Better Arrow Icons")]
    # Only enabled mods count.
    r2 = K.resolve(BIU_RULES, index, enabled={"Better Inventory UI", "BCPP 16x9 4 chars"})
    assert [(f, t) for f, t, _r, _s in r2.edges] == [("BCPP 16x9 4 chars", "Better Inventory UI")]


# ---- sorter strength ----------------------------------------------------------

class _Game:
    def __init__(self, staging):
        self._staging = staging

    def get_effective_mod_staging_path(self):
        return self._staging


def _sort_profile(tmp_path, monkeypatch, order, index, rules=()):
    staging = tmp_path / "mods"
    for mod in index:
        (staging / mod).mkdir(parents=True, exist_ok=True)
        (staging / mod / "meta.ini").write_text(
            "[General]\ncategoryname = User Interface\n", encoding="utf-8")
    prof = tmp_path / "profile"
    prof.mkdir()
    write_modlist(prof / "modlist.txt",
                  [ModEntry(name=m, enabled=True, locked=False) for m in order])
    bx.write_rules(prof, {"rules": list(rules), "ignored": []})
    monkeypatch.setattr(bx, "build_index", lambda *a, **k: index)
    return _Game(staging), prof


def test_known_rule_beats_layer_order(tmp_path, monkeypatch):
    # Same layer; current order has BIU first — the author rule moves it after.
    index = {"Better Inventory UI": [_rec("u-biu", "Better Inventory UI")],
             "Better Containers": [_rec("u-bc", "Better Containers")]}
    game, prof = _sort_profile(tmp_path, monkeypatch,
                               ["Better Containers", "Better Inventory UI"], index)
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt", known_rules=BIU_RULES)
    assert plan.load_order == ["Better Containers", "Better Inventory UI"]
    assert any("author rule" in m.reason for m in plan.moves)


def test_user_decision_beats_known_rule_and_is_reported(tmp_path, monkeypatch):
    index = {"Better Inventory UI": [_rec("u-biu", "Better Inventory UI")],
             "Better Containers": [_rec("u-bc", "Better Containers")]}
    rule = {"winner": "Better Containers", "loser": "Better Inventory UI",
            "reason": "ui_template"}
    game, prof = _sort_profile(tmp_path, monkeypatch,
                               ["Better Containers", "Better Inventory UI"], index,
                               rules=[rule])
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt", known_rules=BIU_RULES)
    assert plan.load_order == ["Better Inventory UI", "Better Containers"]
    assert any("skipped" in u for u in plan.unresolved)


def test_dependency_beats_known_rule(tmp_path, monkeypatch):
    # Better Containers depends on Better Inventory UI: the author rule can't hold.
    index = {"Better Inventory UI": [_rec("u-biu", "Better Inventory UI")],
             "Better Containers": [_rec("u-bc", "Better Containers", deps=["u-biu"])]}
    game, prof = _sort_profile(tmp_path, monkeypatch,
                               ["Better Containers", "Better Inventory UI"], index)
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt", known_rules=BIU_RULES)
    assert plan.load_order == ["Better Inventory UI", "Better Containers"]
    assert any("skipped" in u for u in plan.unresolved)


def test_known_layer_pin(tmp_path, monkeypatch):
    rules = {"version": 1, "mods": [{"name": "CF", "match": {"uuids": ["u-cf"]},
                                     "layer": "late"}]}
    index = {"CF": [_rec("u-cf", "CF")], "Other": [_rec("u-o", "Other")]}
    game, prof = _sort_profile(tmp_path, monkeypatch, ["Other", "CF"], index)
    plan = bg3_sort.compute_layered_plan(game, prof / "modlist.txt", known_rules=rules)
    assert plan.layers["CF"] == ("late", "known mod")
    assert plan.load_order == ["Other", "CF"]


# ---- Insights -----------------------------------------------------------------

def _entries(*names):
    return [ModEntry(name=n, enabled=True, locked=False) for n in names]


def test_finding_intended_by_author_rule(tmp_path):
    def rec(uuid, name):
        r = _rec(uuid, name)
        r["gui_templates"] = ["Keyboard:CharacterInventoryTemplate"]
        return r
    index = {"Better Inventory UI": [rec("u-biu", "Better Inventory UI")],
             "BCPP": [rec("u-b", "BCPP")]}
    enabled = _entries("Better Inventory UI", "BCPP")   # BIU on top = loads last
    known = K.resolve(BIU_RULES, index)
    [f] = bx.analyse(enabled, index, tmp_path, None, known)[0]
    assert f.winner == "Better Inventory UI" and f.intended
    assert f.intended_by == "author rule" and "example/4597" in f.note
    assert bx.unresolved_count(bx.Insights(findings=[f])) == 0


def test_known_incompatible_finding(tmp_path):
    index = {"Better Inventory UI": [_rec("u-biu", "Better Inventory UI")],
             "Better Arrow Icons": [_rec("u-a", "Better Arrow Icons")]}
    known = K.resolve(BIU_RULES, index)
    findings = bx.analyse(_entries("Better Inventory UI", "Better Arrow Icons"),
                          index, tmp_path, None, known)[0]
    [f] = [x for x in findings if x.kind == "known_incompatible"]
    assert "arrow icons overlap" in f.note and f.winner is None


def test_outdated_dependency_finding(tmp_path):
    # CPCCE asks for ImpUI 2.0.0.47; installed 1.0.0.0.
    need = str((2 << 55) | 47)
    index = {"CPCCE": [_rec("u-c", "CPCCE", deps=["u-imp"],
                            dep_versions={"u-imp": need})],
             "ImpUI": [_rec("u-imp", "ImpUI", version64="36028797018963968")]}
    findings = bx.analyse(_entries("CPCCE", "ImpUI"), index, tmp_path)[0]
    [f] = [x for x in findings if x.kind == "outdated_dependency"]
    assert f.keys == ["ImpUI: needs 2.0.0.47, installed 1.0.0.0"]


def test_decode_version():
    assert bx.decode_version("36028797018963968") == (1, 0, 0, 0)
    assert bx.decode_version("1") == (1, 0, 0, 0)
    assert bx.decode_version(str((8 << 55) | (5 << 31) | 12)) == (8, 0, 5, 12)
    assert bx.decode_version("garbage") == (0, 0, 0, 0)

"""BG3 suggestions from Nexus author notes (Utils.mods.bg3_author_notes) and
how Load Order Insights shows / accepts them.

Every case here is a real sentence from a Nexus page seen in the dry run over
a 456-mod profile, including the false positives that run exposed.
"""
from __future__ import annotations

import json

from Utils.mods import bg3_author_notes as A
from Utils.mods import bg3_pak_index as bx
from Utils.mods.modlist import ModEntry, read_modlist, write_modlist


def _rec(uuid, name, meta_only=False):
    return {"meta": {"uuid": uuid, "name": name, "folder": name, "version64": "1",
                     "version": "", "md5": "", "publish_handle": "0", "mod_type": "",
                     "dependencies": [], "dependency_names": {},
                     "is_override_only": False, "is_meta_only": meta_only, "tags": []},
            "files": [], "stats": {}, "treasure": {}, "gui": [], "gui_templates": [],
            "conflicts": []}


def _staging(tmp_path, mods: dict[str, tuple[int, str]]):
    """{folder: (nexus mod id, nexus name)} -> staging dir with meta.ini files."""
    staging = tmp_path / "mods"
    for folder, (mid, nexus_name) in mods.items():
        (staging / folder).mkdir(parents=True, exist_ok=True)
        (staging / folder / "meta.ini").write_text(
            f"[General]\nmodid = {mid}\nnexusname = {nexus_name}\n", encoding="utf-8")
    return staging


def _notes(mid, *sentences):
    return {str(mid): {"sentences": [{"kind": k, "text": t} for k, t in sentences]}}


def _run(tmp_path, mods, notes, extra_index=None):
    staging = _staging(tmp_path, {m: v for m, v in mods.items() if v})
    index = {m: [_rec(f"u-{i}", m)] for i, m in enumerate(mods)}
    index.update(extra_index or {})
    return A.suggestions(index, list(index), staging, notes)


def test_install_after_list_with_loose_names(tmp_path):
    # "Better Container" (singular), "BCPP" (acronym), "Better Hotbar 2" (edition).
    mods = {"Better Inventory UI": (4597, "Better Inventory UI"),
            "Better Containers (8x9)": (1, "Better Containers"),
            "BCPP UW 6 chars 6x9 inventory": (2, "BCPP"),
            "Better Hotbar [Vova's Edition]": (3, "Better Hotbar 2"),
            "Unrelated Mod": (4, "Unrelated Mod")}
    notes = _notes(4597, ("order", "If you use Better Container and/or BCPP and/or "
                          "Better Hotbar 2 - install Better Inventory UI after them."))
    [s] = _run(tmp_path, mods, notes)
    assert s.kind == "load_after" and s.mod == "Better Inventory UI"
    assert s.others == ["BCPP UW 6 chars 6x9 inventory", "Better Containers (8x9)",
                        "Better Hotbar [Vova's Edition]"]


def test_trailing_mcm_suffix_does_not_mean_mcm(tmp_path):
    mods = {"ToggleFX": (10, "ToggleFX"),
            "Mod Configuration Menu": (9162, "Mod Configuration Menu (MCM)"),
            "Auto Lockpicking - MCM": (11, "Auto Lockpicking - MCM")}
    [s] = _run(tmp_path, mods, _notes(10, ("order", "Must be after MCM in load order.")))
    assert s.others == ["Mod Configuration Menu"]


def test_other_mod_as_subject_flips_direction(tmp_path):
    mods = {"EasyCheat 2.3.2": (5, "EasyCheat"),
            "Mod Configuration Menu": (9162, "Mod Configuration Menu (MCM)")}
    for sentence in ("Wrong load order, make sure MCM is higher than EasyCheat.",
                     "Install using BG3MM, with MCM higher in your load order."):
        [s] = _run(tmp_path, mods, _notes(5, ("order", sentence)))
        assert s.kind == "load_after", sentence       # EasyCheat after MCM


def test_roman_numerals_and_generic_acronyms_are_not_mods(tmp_path):
    mods = {"Tav's Room": (6, "Tav's Room"),
            "Clean & Furnished - Act III - Slums": (7, "Clean & Furnished - Act III"),
            "VFX Library SHV - 1": (8, "VFX Library SHV")}
    notes = {**_notes(6, ("incompatible", "This mod conflicts with Customized - Elfsong - Act III")),
             **_notes(8, ("incompatible", "incompatible with any other mod that has VFX support"))}
    assert _run(tmp_path, mods, notes) == []


def test_dividers_are_never_named(tmp_path):
    mods = {"Records": (12, "Records"), "Astra's Load Order Dividers": None}
    divider = {"Astra's Load Order Dividers": [_rec("d", "✒︎ 014 · Scripts · MCM ❧", meta_only=True)]}
    assert _run(tmp_path, mods, _notes(12, ("order", "Placed below MCM in the load order.")),
                extra_index=divider) == []


def test_same_page_sentence_belongs_to_the_named_file(tmp_path):
    # Better Inventory UI and its Addon are files of one Nexus page (4597).
    mods = {"Better Inventory UI": (4597, "Better Inventory UI"),
            "Addon for Better Inventory UI (unofficial)": (4597, "Better Inventory UI"),
            "Better Containers (8x9)": (1, "Better Containers")}
    notes = _notes(4597, ("order", "install Better Inventory UI after Better Container."))
    out = _run(tmp_path, mods, notes)
    assert [(s.mod, s.others) for s in out] == [("Better Inventory UI", ["Better Containers (8x9)"])]


# ---- Insights -------------------------------------------------------------------

def _entries(*names):
    return [ModEntry(name=n, enabled=True, locked=False) for n in names]


def test_followed_note_is_intended_and_needs_nothing(tmp_path):
    sug = A.AuthorSuggestion("load_after", "Tag Framework", ["Community Library"],
                             "just after Community Library", "https://x/1")
    index = {"Tag Framework": [_rec("u1", "TF")], "Community Library": [_rec("u2", "CL")]}
    findings = bx.analyse(_entries("Tag Framework", "Community Library"), index,
                          tmp_path, author=[sug])[0]
    [f] = [x for x in findings if x.kind == "author_note"]
    assert f.intended and f.intended_by == "author note"
    assert bx.unresolved_count(bx.Insights(findings=[f])) == 0


def test_unfollowed_note_needs_a_decision_and_accept_saves_load_after(tmp_path):
    sug = A.AuthorSuggestion("load_after", "Tag Framework", ["Community Library"],
                             "just after Community Library", "https://x/1")
    index = {"Tag Framework": [_rec("u1", "TF")], "Community Library": [_rec("u2", "CL")]}
    enabled = _entries("Community Library", "Tag Framework")     # TF loads first
    [f] = [x for x in bx.analyse(enabled, index, tmp_path, author=[sug])[0]
           if x.kind == "author_note"]
    assert not f.intended and bx.unresolved_count(bx.Insights(findings=[f])) == 1
    prof = tmp_path / "profile"
    prof.mkdir()
    write_modlist(prof / "modlist.txt", enabled)
    assert bx.accept_author_note(prof, f) == 1
    assert bx.load_after_of(prof, "Tag Framework") == ["Community Library"]


def test_known_rule_suggestion_json(tmp_path):
    sug = A.AuthorSuggestion("load_after", "Better Inventory UI", ["Better Containers (8x9)"],
                             "install after Better Container", "https://www.nexusmods.com/baldursgate3/mods/4597")
    f = bx.Finding(kind="author_note", mods=["Better Inventory UI", "Better Containers (8x9)"],
                   keys=[], winner=None, suggestion=sug)
    entry = json.loads(bx.known_rule_suggestion(
        f, {"Better Inventory UI": ["u-biu"], "Better Containers (8x9)": ["u-bc"]}))
    assert entry["match"] == {"uuids": ["u-biu"], "names": []}
    assert entry["after"][0]["match"]["uuids"] == ["u-bc"]
    assert entry["source"].endswith("/4597")

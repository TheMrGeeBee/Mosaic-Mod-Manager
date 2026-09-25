"""Author notes from Nexus mod pages (Nexus.nexus_author_notes).

The descriptions below are short excerpts, in the exact markup Nexus's
GraphQL API returns (BBCode mixed with <br />), of Better Inventory UI
(mod 4597) and Shared Bags for Your Party (23822) — the pages the extractor
was tuned on.
"""
from __future__ import annotations

import json

import Nexus.nexus_author_notes as N

BIU_EXCERPT = (
    '[size=5][b][color=#b6d7a8][center]Better Inventory UI[/center]\n'
    '<br />[/color][/b][/size][b][quote][size=5][center]Patch #8 ready (HF #34)'
    '[/center][/size][/quote][/b]\n<br />[Spoiler]\n'
    '<br />[size=3][color=#ff7700]Q (Patch 7):[/color] Do I still need to put mods '
    'to Override/mark them as replacer?\n'
    '<br />[color=#b6d7a8]A:[/color] There is chance few mods will have some '
    'compatibility issues in the first days after release.\n<br />[/Spoiler]\n'
    '<br />The only hard incompatibilities are Immersive UI by Aetherpoint and a few '
    'mods removing related widget elements by Fahadbh.\n'
    '<br />Requires Lodestones (and FocusCore﻿) installed!\n'
    '<br />If you use Better Container and/or BCPP and/or Better Hotbar 2 - install '
    'Better Inventory UI after them (for vanilla MM) or move lower in load order in '
    'case of BG3MM/Mod Manager Tweaks.\n'
    '<br />Main part of the mod requires ImpUI.\n'
    '<br />Compatible with Stackable Items﻿ (use its standard version without fix).\n'
    '<br />Not compatible with Shadow Sorcerer - Subclass.\n'
    '<br />Not compatible with Better Arrow Icons (arrow icons will overlap).\n'
    '<br />Addon requires Distinctive Dyes (its a mild requirement, you can skip it '
    "if mod isn't yet updated for Patch 7).\n")

SHARED_BAGS_EXCERPT = (
    '[size=4][b]Requirements[/b][/size]\n<br />[list]\n'
    '<br />[*]Script Extender — required\n'
    '<br />[*]MCM (Mod Configuration Menu) — required\n<br />[/list]\n')


def _texts(sentences, kind=None):
    return [s["text"] for s in sentences if kind is None or s["kind"] == kind]


def test_better_inventory_ui_sentences():
    out = N.extract_sentences(BIU_EXCERPT)
    order = _texts(out, "order")
    assert len(order) == 1 and "install Better Inventory UI after them" in order[0]
    incompatible = _texts(out, "incompatible")
    assert any("Better Arrow Icons" in t for t in incompatible)
    assert any("Shadow Sorcerer" in t for t in incompatible)
    assert any("Immersive UI by Aetherpoint" in t for t in incompatible)
    requires = _texts(out, "requires")
    assert any("Distinctive Dyes" in t for t in requires)   # patch talk after the keyword is fine
    # Noise: the "Patch #8 ready" banner, the FAQ question and the FAQ answer.
    everything = " ".join(_texts(out))
    assert "Patch #8 ready" not in everything
    assert "Q (Patch 7)" not in everything
    assert "chance few mods" not in everything
    # Strongest kinds first.
    assert out[0]["kind"] == "incompatible"


def test_shared_bags_requirements_from_prose():
    assert _texts(N.extract_sentences(SHARED_BAGS_EXCERPT), "requires") == [
        "Script Extender — required", "MCM (Mod Configuration Menu) — required"]


def test_plain_text_strips_markup():
    assert N.plain_text("[b]Hi[/b]<br />&amp; bye") == "Hi\n& bye"


# ---- cache + fetch --------------------------------------------------------------

class _Resp:
    def __init__(self, nodes, ok=True):
        self.ok, self.status_code, self._nodes = ok, 200 if ok else 500, nodes

    def json(self):
        return {"data": {"legacyModsByDomain": {"nodes": self._nodes}}}


class _Session:
    def __init__(self, nodes, ok=True):
        self.calls, self._nodes, self._ok = [], nodes, ok

    def post(self, url, json=None, timeout=None):
        self.calls.append(json["variables"]["ids"])
        return _Resp(self._nodes, self._ok)


class _Api:
    def __init__(self, session):
        self._session, self._timeout = session, 5


def _patch_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(N, "_cache_path", lambda gd: tmp_path / f"{gd}.json")


def test_fetch_caches_sentences_and_skips_fresh_entries(tmp_path, monkeypatch):
    _patch_cache(tmp_path, monkeypatch)
    node = {"modId": 4597, "name": "Better Inventory UI", "updatedAt": "x",
            "description": BIU_EXCERPT,
            "modRequirements": {"nexusRequirements": {"nodes": [
                {"modId": 1, "modName": "ImpUI (ImprovedUI)", "notes": "ImpUI Patch 8 Slim"}]}}}
    session = _Session([node])
    cache = N.fetch_notes("baldursgate3", [4597], api=_Api(session))
    entry = cache["4597"]
    assert entry["name"] == "Better Inventory UI"
    assert any(s["kind"] == "order" for s in entry["sentences"])
    assert entry["requirement_notes"][0]["text"] == \
        "Requires ImpUI (ImprovedUI) — ImpUI Patch 8 Slim"
    assert "description" not in entry                      # only sentences cached
    N.fetch_notes("baldursgate3", [4597], api=_Api(session))
    assert len(session.calls) == 1                           # fresh: no refetch
    N.fetch_notes("baldursgate3", [4597], force=True, api=_Api(session))
    assert len(session.calls) == 2


def test_fetch_failure_keeps_old_cache(tmp_path, monkeypatch):
    _patch_cache(tmp_path, monkeypatch)
    (tmp_path / "baldursgate3.json").write_text(json.dumps(
        {"1": {"fetched_at": 0, "sentences": [{"kind": "order", "text": "old"}]}}),
        encoding="utf-8")
    cache = N.fetch_notes("baldursgate3", [1], api=_Api(_Session([], ok=False)))
    assert cache["1"]["sentences"][0]["text"] == "old"


def test_stale_ids_respects_max_age(tmp_path, monkeypatch):
    _patch_cache(tmp_path, monkeypatch)
    (tmp_path / "baldursgate3.json").write_text(json.dumps(
        {"1": {"fetched_at": 1000}, "2": {"fetched_at": 1000 + N._MAX_AGE}}),
        encoding="utf-8")
    now = 1000 + N._MAX_AGE + 10
    assert N.stale_ids("baldursgate3", [1, 2, 3], now=now) == [1, 3]

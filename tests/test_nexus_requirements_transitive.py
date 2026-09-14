"""A missing requirement can itself require something else that's also
missing (e.g. an addon needs a framework mod, which itself needs an
animation framework) — real report: "Lune's Idle Expressions - BG3SX
Addon" was flagged missing "BG3SX - Sex Framework", but BG3SX's own
requirement on "BG3AF - Animation Framework" only ever surfaced after
installing BG3SX and re-checking, one hop at a time.

Utils.Nexus.nexus_requirements._expand_transitive_missing walks that chain
during the existing network-calling requirement check, tested here directly
against a fake API client (no real network).
"""
from __future__ import annotations

from Nexus.nexus_api import NexusModRequirement
from Nexus.nexus_requirements import _expand_transitive_missing


class _FakeApi:
    def __init__(self, graph: dict[int, list[NexusModRequirement]]):
        self._graph = graph
        self.calls: list[int] = []

    def get_mod_requirements(self, game_domain, mod_id):
        self.calls.append(mod_id)
        return self._graph.get(mod_id, [])


def _req(mod_id: int, name: str) -> NexusModRequirement:
    return NexusModRequirement(mod_id=mod_id, mod_name=name)


def test_expands_one_level_transitively():
    # BG3SX Addon (1, already known missing) itself requires BG3SX Framework
    # equivalent (2) which is also missing — must surface both.
    api = _FakeApi({1: [_req(2, "B")]})
    result = _expand_transitive_missing(
        api, "baldursgate3", [_req(1, "A")], installed_mod_ids=set(),
        external_set=set(), alternatives_dict={}, log=lambda m: None)
    assert {r.mod_id for r in result} == {1, 2}


def test_multi_hop_chain_fully_expands():
    api = _FakeApi({1: [_req(2, "B")], 2: [_req(3, "C")]})
    result = _expand_transitive_missing(
        api, "baldursgate3", [_req(1, "A")], installed_mod_ids=set(),
        external_set=set(), alternatives_dict={}, log=lambda m: None)
    assert {r.mod_id for r in result} == {1, 2, 3}


def test_stops_on_cycle_instead_of_looping_forever():
    a, b = _req(1, "A"), _req(2, "B")
    api = _FakeApi({1: [b], 2: [a]})
    result = _expand_transitive_missing(
        api, "baldursgate3", [a], installed_mod_ids=set(),
        external_set=set(), alternatives_dict={}, log=lambda m: None)
    assert {r.mod_id for r in result} == {1, 2}


def test_respects_max_depth():
    chain = {i: [_req(i + 1, f"Mod{i + 1}")] for i in range(1, 6)}
    api = _FakeApi(chain)
    result = _expand_transitive_missing(
        api, "baldursgate3", [_req(1, "Mod1")], installed_mod_ids=set(),
        external_set=set(), alternatives_dict={}, log=lambda m: None,
        max_depth=2)
    assert {r.mod_id for r in result} == {1, 2, 3}


def test_already_installed_transitive_requirement_is_excluded():
    api = _FakeApi({1: [_req(2, "B")]})
    result = _expand_transitive_missing(
        api, "baldursgate3", [_req(1, "A")], installed_mod_ids={2},
        external_set=set(), alternatives_dict={}, log=lambda m: None)
    assert {r.mod_id for r in result} == {1}


def test_external_tool_transitive_requirement_is_excluded():
    api = _FakeApi({1: [_req(2, "B")]})
    result = _expand_transitive_missing(
        api, "baldursgate3", [_req(1, "A")], installed_mod_ids=set(),
        external_set={("baldursgate3", 2)}, alternatives_dict={},
        log=lambda m: None)
    assert {r.mod_id for r in result} == {1}


def test_no_further_requirements_returns_input_unchanged():
    api = _FakeApi({})
    result = _expand_transitive_missing(
        api, "baldursgate3", [_req(1, "A")], installed_mod_ids=set(),
        external_set=set(), alternatives_dict={}, log=lambda m: None)
    assert {r.mod_id for r in result} == {1}

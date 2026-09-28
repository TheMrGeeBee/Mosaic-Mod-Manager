"""NexusAPI.get_mod: a real live 200 response has been seen to omit "name"
(the rename dialog's "Fetch name from Nexus" surfaced this as a bare
`KeyError: 'name'` shown verbatim to the user as "Fetch failed: 'name'")."""
from __future__ import annotations

from Nexus.nexus_api import NexusAPI


def _api(monkeypatch, response: dict) -> NexusAPI:
    api = NexusAPI("fake-key")
    monkeypatch.setattr(api, "_get", lambda path, params=None: response)
    return api


def test_a_response_missing_name_does_not_raise(monkeypatch):
    api = _api(monkeypatch, {"mod_id": 108082, "version": "2.2.2a"})
    info = api.get_mod("fallout4", 108082)
    assert info.name == ""
    assert info.mod_id == 108082


def test_a_response_missing_mod_id_falls_back_to_the_requested_one(monkeypatch):
    api = _api(monkeypatch, {"name": "Some Mod"})
    info = api.get_mod("fallout4", 108082)
    assert info.mod_id == 108082
    assert info.name == "Some Mod"


def test_a_normal_response_is_unaffected(monkeypatch):
    api = _api(monkeypatch, {"mod_id": 42, "name": "Real Mod", "version": "1.0"})
    info = api.get_mod("fallout4", 42)
    assert info.mod_id == 42 and info.name == "Real Mod"

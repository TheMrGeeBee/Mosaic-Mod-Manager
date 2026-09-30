"""Test for collection_install.py's mod.io off-site auto-resolution.

Real bug (confirmed against a real exported collection.json, "Better Hotbar
2 [Vova's Edition]"): a mod.io-sourced Collection row exports with
source.type "browse" (the real Nexus Collection schema has no mod.io type,
so Mosaic's own exporter downgrades it, per collection_export.py) plus its
mod.io page URL. On import that page is auto-fetchable through mod.io's own
read-only API -- resolve the page slug to a mod id and its live file's real
binary_url, same as Quick Update does -- so it should install automatically
instead of always landing in the "download manually" off-site panel the
way a genuine third-party webpage does.
"""
from __future__ import annotations

import types

from Utils.collections import collection_install as ci


class _FakeSummary:
    def __init__(self, mod_id, latest_file_id):
        self.mod_id = mod_id
        self.latest_file_id = latest_file_id


class _FakeFile:
    def __init__(self, binary_url):
        self.binary_url = binary_url


class _FakeModioAPI:
    def __init__(self, api_key):
        assert api_key == "key123"

    def get_mod_by_slug(self, slug):
        if slug == "better-hotbar-vovas-edition":
            return _FakeSummary(mod_id=42, latest_file_id=99)
        return None

    def get_file(self, mod_id, file_id):
        assert (mod_id, file_id) == (42, 99)
        return _FakeFile("https://cdn.mod.io/signed/real-file.zip")


def _install_fake_modio(monkeypatch, api_key="key123"):
    key_mod = types.SimpleNamespace(load_modio_key=lambda: api_key)
    api_mod = types.SimpleNamespace(ModioAPI=_FakeModioAPI)

    def fake_load(stem):
        return {"modio_key": key_mod, "modio_api": api_mod}[stem]

    monkeypatch.setattr(ci, "_load_bg3_modio", fake_load)


_URL = "https://mod.io/g/baldursgate3/m/better-hotbar-vovas-edition"


def test_modio_browse_url_resolves_to_binary_url(monkeypatch):
    _install_fake_modio(monkeypatch)
    assert ci._resolve_modio_browse_url(_URL, "baldursgate3") == \
        "https://cdn.mod.io/signed/real-file.zip"


def test_non_modio_browse_url_is_not_resolved(monkeypatch):
    _install_fake_modio(monkeypatch)
    assert ci._resolve_modio_browse_url(
        "https://www.some-other-site.com/download/thing", "baldursgate3") == ""


def test_non_bg3_game_is_never_resolved(monkeypatch):
    _install_fake_modio(monkeypatch)
    assert ci._resolve_modio_browse_url(_URL, "skyrimspecialedition") == ""


def test_missing_api_key_falls_back_silently(monkeypatch):
    _install_fake_modio(monkeypatch, api_key="")
    assert ci._resolve_modio_browse_url(_URL, "baldursgate3") == ""


def test_unknown_slug_falls_back_silently(monkeypatch):
    _install_fake_modio(monkeypatch)
    assert ci._resolve_modio_browse_url(
        "https://mod.io/g/baldursgate3/m/some-other-mod", "baldursgate3") == ""


def test_modio_failure_never_raises(monkeypatch):
    def boom(stem):
        raise RuntimeError("network down")
    monkeypatch.setattr(ci, "_load_bg3_modio", boom)
    assert ci._resolve_modio_browse_url(_URL, "baldursgate3") == ""

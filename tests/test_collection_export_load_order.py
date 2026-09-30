"""Tests for collection_export.build_load_order() -- the manifest's
``loadOrder`` block (BG3/FBLO games only).

Requested directly, as a follow-up to modRules carrying Load Order
Insights / Sort Load Order decisions: the exported collection should also
carry the pak-level load order those same wizards (and deploy) already
resolve, not just the per-mod mods[]-array order + modRules constraints.
collection_reset.py's own _resolve_collection_priorities prefers a
manifest's loadOrder over a pure modRules topo-sort when present, and
Utils/mods/modsettings.py's _apply_manifest_pak_order needs it for correct
pak-level interleaving (a single mod can ship several paks).

build_load_order() calls Utils.mods.bg3_pak_index.build_index() (reads
real .pak files) then resolve_pak_order() -- both monkeypatched here so
the test doesn't need real binary pak fixtures; only resolve_pak_order's
*output* (the thing collection_export.py actually consumes) is under
test."""
from __future__ import annotations

from Utils.collections import collection_export
from Utils.mods import bg3_pak_index as bx
from Utils.mods.modlist import ModEntry, write_modlist


class _FakeGame:
    def __init__(self, staging_root, domain="baldursgate3"):
        self._staging_root = str(staging_root)
        self.nexus_game_domain = domain

    def get_effective_mod_staging_path(self):
        return self._staging_root


def _info(uuid, name, source_mod):
    return bx.BG3ModInfo(uuid=uuid, name=name, folder=name, version64="1",
                         source_mod=source_mod)


def _profile(tmp_path, names):
    write_modlist(tmp_path / "modlist.txt",
                  [ModEntry(name=n, enabled=True, locked=False) for n in names])
    return tmp_path


def test_non_bg3_game_returns_none(tmp_path, monkeypatch):
    profile = _profile(tmp_path, ["A"])
    game = _FakeGame(tmp_path, domain="skyrimspecialedition")
    assert collection_export.build_load_order(
        profile, "skyrimspecialedition", game, []) is None


def test_no_modlist_returns_none(tmp_path):
    game = _FakeGame(tmp_path)
    assert collection_export.build_load_order(
        tmp_path / "missing", "baldursgate3", game, []) is None


def test_builds_one_entry_per_pak_lowest_priority_first(tmp_path, monkeypatch):
    profile = _profile(tmp_path, ["Mod A", "Mod B"])
    game = _FakeGame(tmp_path)
    monkeypatch.setattr(bx, "build_index", lambda *a, **k: {})
    # resolve_pak_order's real contract: lowest-priority (loads first) first.
    ordered = [_info("uuid-b", "Mod B", "Mod B"),
              _info("uuid-a", "Mod A", "Mod A")]
    monkeypatch.setattr(bx, "resolve_pak_order", lambda *a, **k: ordered)

    mods = [{"name": "Mod A", "source": {"fileId": 111}},
           {"name": "Mod B", "source": {"fileId": 222}}]
    lo = collection_export.build_load_order(profile, "baldursgate3", game, mods)

    assert lo == [
        {"data": {"uuid": "uuid-b"}, "name": "Mod B", "fileId": 222},
        {"data": {"uuid": "uuid-a"}, "name": "Mod A", "fileId": 111},
    ]


def test_multiple_paks_per_mod_each_get_an_entry(tmp_path, monkeypatch):
    profile = _profile(tmp_path, ["Divider Pack"])
    game = _FakeGame(tmp_path)
    monkeypatch.setattr(bx, "build_index", lambda *a, **k: {})
    ordered = [_info("uuid-1", "Divider Pack", "Divider Pack"),
              _info("uuid-2", "Divider Pack", "Divider Pack")]
    monkeypatch.setattr(bx, "resolve_pak_order", lambda *a, **k: ordered)

    lo = collection_export.build_load_order(
        profile, "baldursgate3", game, [{"name": "Divider Pack", "source": {}}])

    assert len(lo) == 2
    assert [e["data"]["uuid"] for e in lo] == ["uuid-1", "uuid-2"]


def test_non_nexus_mod_omits_fileId_but_keeps_uuid(tmp_path, monkeypatch):
    profile = _profile(tmp_path, ["Direct Mod"])
    game = _FakeGame(tmp_path)
    monkeypatch.setattr(bx, "build_index", lambda *a, **k: {})
    monkeypatch.setattr(bx, "resolve_pak_order",
                        lambda *a, **k: [_info("uuid-x", "Direct Mod", "Direct Mod")])

    mods = [{"name": "Direct Mod", "source": {"type": "direct"}}]
    lo = collection_export.build_load_order(profile, "baldursgate3", game, mods)

    assert lo == [{"data": {"uuid": "uuid-x"}, "name": "Direct Mod"}]
    assert "fileId" not in lo[0]


def test_empty_resolved_order_returns_none(tmp_path, monkeypatch):
    profile = _profile(tmp_path, ["A"])
    game = _FakeGame(tmp_path)
    monkeypatch.setattr(bx, "build_index", lambda *a, **k: {})
    monkeypatch.setattr(bx, "resolve_pak_order", lambda *a, **k: [])

    assert collection_export.build_load_order(
        profile, "baldursgate3", game, [{"name": "A", "source": {}}]) is None


def test_scan_failure_returns_none_not_raises(tmp_path, monkeypatch):
    profile = _profile(tmp_path, ["A"])
    game = _FakeGame(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("corrupt pak")
    monkeypatch.setattr(bx, "build_index", boom)

    assert collection_export.build_load_order(
        profile, "baldursgate3", game, [{"name": "A", "source": {}}]) is None

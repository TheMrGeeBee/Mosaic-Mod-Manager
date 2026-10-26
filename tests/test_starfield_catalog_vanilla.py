"""Starfield has no Starfield.ccc; Bethesda's free Creations downloaded in-game are
listed only in the prefix's ContentCatalog.txt and must count as vanilla."""
from __future__ import annotations

import json
from pathlib import Path

from Games.Bethesda.starfield import Starfield
from Utils.exe_launch.game_helpers import _read_ccc_manifest


def _game(prefix: Path) -> Starfield:
    g = Starfield.__new__(Starfield)
    g._prefix_path = prefix
    g.get_game_path = lambda: None
    return g


def _write_catalog(prefix: Path, entries: dict) -> None:
    d = prefix / Starfield._APPDATA_SUBPATH
    d.mkdir(parents=True)
    (d / "ContentCatalog.txt").write_text(json.dumps(entries), encoding="utf-8")


def test_only_bethesda_creations_are_taken_from_the_catalog(tmp_path):
    _write_catalog(tmp_path, {
        "TM_a": {"Files": ["sfbgs019 - main.ba2", "sfbgs019.esm"]},
        "TM_b": {"Files": ["meaningfulbackgroundsplus.esm"]},
    })
    assert _game(tmp_path).catalog_vanilla_plugins() == ["sfbgs019.esm"]


def test_catalog_plugin_is_vanilla_only_when_present_in_data(tmp_path):
    _write_catalog(tmp_path, {"TM_a": {"Files": ["sfbgs019.esm", "sfbgs021.esm"]}})
    result = _read_ccc_manifest(_game(tmp_path), "Starfield.ccc", {"sfbgs019.esm"})
    assert result == {"sfbgs019.esm": "sfbgs019.esm"}


def test_missing_or_broken_catalog_is_harmless(tmp_path):
    assert _game(tmp_path).catalog_vanilla_plugins() == []
    _write_catalog(tmp_path, {})
    (tmp_path / Starfield._APPDATA_SUBPATH / "ContentCatalog.txt").write_text("{nope")
    assert _game(tmp_path).catalog_vanilla_plugins() == []

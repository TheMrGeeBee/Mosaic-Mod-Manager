"""Vanilla plugins stay out of plugins.txt because the engine force-loads them.

Starfield Creations break that assumption: libloadorder force-loads only the
hardcoded base masters, so a Creation without a ``*`` entry is inert — and so
is any mod that masters it. They are vanilla for ownership, listed for loading.
"""
from __future__ import annotations

from pathlib import Path

from gui_qt.plugin.plugin_state import (
    PluginEntry, PluginRow, _sync_listed_vanilla, save_plugins,
)
from Utils.plugins.plugins import read_plugins


class FakeGame:
    plugins_use_star_prefix = True
    plugins_include_vanilla = False

    def __init__(self, profile_dir: Path, listed: set[str]):
        self._profile_dir = profile_dir
        self._listed = listed

    def plugins_txt_listed_vanilla(self) -> set[str]:
        return self._listed


def _plugins_path(game, profile):
    return game._profile_dir / "plugins.txt"


def _rows():
    return [
        PluginRow("Starfield.esm", True, vanilla=True),     # engine force-loads
        PluginRow("sfbgs019.esm", True, vanilla=True),      # Creation — must list
        PluginRow("Some Mod.esp", True),
        PluginRow("Off Mod.esp", False),
    ]


def test_save_keeps_creations_and_still_drops_base_masters(tmp_path, monkeypatch):
    import gui_qt.plugin.plugin_state as ps
    monkeypatch.setattr(ps, "plugins_path", _plugins_path)
    game = FakeGame(tmp_path, {"sfbgs019.esm"})

    save_plugins(game, "p", _rows())

    written = [(e.name, e.enabled)
               for e in read_plugins(tmp_path / "plugins.txt", star_prefix=True)]
    assert written == [("sfbgs019.esm", True), ("Some Mod.esp", True),
                       ("Off Mod.esp", False)]
    # loadorder.txt still carries everything, base masters included.
    assert "Starfield.esm" in (tmp_path / "loadorder.txt").read_text()


def test_a_game_with_no_exceptions_is_unchanged(tmp_path, monkeypatch):
    import gui_qt.plugin.plugin_state as ps
    monkeypatch.setattr(ps, "plugins_path", _plugins_path)
    game = FakeGame(tmp_path, set())

    save_plugins(game, "p", _rows())

    written = [e.name for e in read_plugins(tmp_path / "plugins.txt",
                                            star_prefix=True)]
    assert written == ["Some Mod.esp", "Off Mod.esp"]


def test_sync_rewrites_a_file_that_lost_its_creation(tmp_path):
    """Repair path: an earlier build dropped the Creation as plain vanilla, so
    the game stopped loading it. The whole file is rewritten in panel order —
    appending would land the Creation master after the mods that need it."""
    p = tmp_path / "plugins.txt"
    p.write_text("*Some Mod.esp\nOff Mod.esp\n", encoding="utf-8")
    game = FakeGame(tmp_path, {"sfbgs019.esm"})
    ordered = [PluginEntry(r.name, r.enabled) for r in _rows()]
    vanilla = {"starfield.esm": "Starfield.esm", "sfbgs019.esm": "sfbgs019.esm"}

    _sync_listed_vanilla(game, p, True, ordered, vanilla)

    assert [e.name for e in read_plugins(p, star_prefix=True)] == [
        "sfbgs019.esm", "Some Mod.esp", "Off Mod.esp"]


def test_sync_leaves_a_healthy_file_alone(tmp_path):
    p = tmp_path / "plugins.txt"
    p.write_text("*sfbgs019.esm\n*Some Mod.esp\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    game = FakeGame(tmp_path, {"sfbgs019.esm"})
    ordered = [PluginEntry(r.name, r.enabled) for r in _rows()]

    _sync_listed_vanilla(game, p, True, ordered,
                         {"starfield.esm": "Starfield.esm",
                          "sfbgs019.esm": "sfbgs019.esm"})

    assert p.read_text(encoding="utf-8") == before


def test_sync_is_a_no_op_for_games_without_creations(tmp_path):
    p = tmp_path / "plugins.txt"
    p.write_text("*Some Mod.esp\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")

    _sync_listed_vanilla(FakeGame(tmp_path, set()), p, True,
                         [PluginEntry("Some Mod.esp", True)], {})

    assert p.read_text(encoding="utf-8") == before


def test_a_disabled_creation_stays_disabled_in_plugins_txt(tmp_path, monkeypatch):
    """Unlike a base master, a Creation can be switched off — e.g. to match the
    plugin list an older save was made with — and the write must honour that."""
    import gui_qt.plugin.plugin_state as ps
    monkeypatch.setattr(ps, "plugins_path", _plugins_path)
    game = FakeGame(tmp_path, {"sfbgs019.esm"})
    rows = _rows()
    rows[1] = PluginRow("sfbgs019.esm", False, vanilla=True, listed_vanilla=True)

    save_plugins(game, "p", rows)

    written = {e.name: e.enabled
               for e in read_plugins(tmp_path / "plugins.txt", star_prefix=True)}
    assert written["sfbgs019.esm"] is False


def test_the_model_lets_a_creation_be_toggled_but_not_a_base_master():
    from gui_qt.plugin.plugin_model import PluginModel

    base = PluginRow("Starfield.esm", True, vanilla=True)
    creation = PluginRow("sfbgs019.esm", True, vanilla=True, listed_vanilla=True)
    model = PluginModel.__new__(PluginModel)
    model._rows = [base, creation]
    model._save = lambda: None
    model._refresh_game_indexes = lambda: None
    model.dataChanged = type("_Sig", (), {"emit": lambda *a, **k: None})()
    model.index = lambda r, c: (r, c)

    model.toggle(0)
    assert base.enabled is True     # force-loaded: untouched
    model.toggle(1)
    assert creation.enabled is False

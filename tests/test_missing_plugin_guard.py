"""Enabled plugins that have no file in Data/ crash the game on the loading screen
with nothing in the crash log to explain it. Real case (2026-09-26): mod folders
renamed under a deployed profile left 141 dangling symlinks, the start plugin among
them. Mosaic now checks before Play."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow  # noqa: E402

from Games.Bethesda.skyrim_se import SkyrimSE  # noqa: E402
from gui_qt.app import MainWindow  # noqa: E402
from gui_qt.overlays.confirm_overlay import ConfirmOverlay  # noqa: E402


@pytest.fixture
def setup(tmp_path, monkeypatch):
    game_root = tmp_path / "game"
    (game_root / "Data").mkdir(parents=True)
    profile_root = tmp_path / "root"
    prof = profile_root / "profiles" / "P"
    prof.mkdir(parents=True)
    g = SkyrimSE()
    monkeypatch.setattr(g, "get_game_path", lambda: game_root)
    monkeypatch.setattr(g, "get_profile_root", lambda: profile_root)
    monkeypatch.setattr(g, "get_deploy_active", lambda: True)
    monkeypatch.setattr(g, "get_last_deployed_profile", lambda: "P")
    return g, game_root, prof


def _plugins(prof, *lines):
    (prof / "plugins.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_everything_present_reports_nothing(setup):
    g, root, prof = setup
    (root / "Data" / "A.esp").write_text("x")
    (root / "Data" / "b.esm").write_text("x")           # case-insensitive
    _plugins(prof, "# comment", "*A.esp", "*B.esm")
    assert g.find_missing_plugin_files("P") == []


def test_a_dangling_symlink_counts_as_missing(setup, tmp_path):
    g, root, prof = setup
    (root / "Data" / "Real.esp").write_text("x")
    (root / "Data" / "Gone.esp").symlink_to(tmp_path / "deleted-mod-folder" / "Gone.esp")
    (root / "Data" / "Ok.esp").symlink_to(root / "Data" / "Real.esp")
    _plugins(prof, "*Real.esp", "*Gone.esp", "*Ok.esp", "*Never There.esp")
    assert g.find_missing_plugin_files("P") == ["Gone.esp", "Never There.esp"]


def test_disabled_plugins_are_ignored(setup):
    g, root, prof = setup
    _plugins(prof, "NotEnabled.esp", "*Enabled.esp")
    assert g.find_missing_plugin_files("P") == ["Enabled.esp"]


def test_nothing_to_say_when_the_profile_is_not_the_deployed_one(setup, monkeypatch):
    g, root, prof = setup
    _plugins(prof, "*Missing.esp")
    monkeypatch.setattr(g, "get_last_deployed_profile", lambda: "Other")
    assert g.find_missing_plugin_files("P") == []
    monkeypatch.setattr(g, "get_last_deployed_profile", lambda: "P")
    monkeypatch.setattr(g, "get_deploy_active", lambda: False)
    assert g.find_missing_plugin_files("P") == []


def test_no_plugins_file_or_data_dir_is_not_an_error(setup):
    g, root, prof = setup
    assert g.find_missing_plugin_files("P") == []       # no plugins.txt
    _plugins(prof, "*A.esp")
    (root / "Data").rmdir()
    assert g.find_missing_plugin_files("P") == []       # no Data dir


# ---- the app's launch guard ---------------------------------------------------

class Harness(QMainWindow):
    def __init__(self, game, missing):
        super().__init__()
        self.resize(900, 700)
        self.show()
        self._gs = type("GS", (), {"profile": "P"})()
        self.logs = []
        self._game = game
        self._missing = missing

    def _append_log(self, m):
        self.logs.append(m)


Harness._guard_missing_plugins = MainWindow._guard_missing_plugins


class FakeGame:
    def __init__(self, missing, raises=False):
        self._m, self._r = missing, raises

    def find_missing_plugin_files(self, _profile):
        if self._r:
            raise RuntimeError("boom")
        return self._m


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_guard_launches_straight_away_when_nothing_is_missing(app):
    h = Harness(None, [])
    fired = []
    h._guard_missing_plugins(FakeGame([]), lambda: fired.append(1))
    assert fired == [1] and not h.findChildren(ConfirmOverlay)


def test_guard_asks_first_and_names_the_missing_plugins(app):
    h = Harness(None, [])
    fired = []
    h._guard_missing_plugins(FakeGame(["Alternate Perspective - Gate to Sovngarde Edition.esp", "C.O.I.N.esp"]),
                             lambda: fired.append(1))
    assert fired == []                                   # not launched yet
    (ov,) = h.findChildren(ConfirmOverlay)
    text = " ".join(l.text() for l in ov.findChildren(QLabel))
    assert "2 enabled plugin(s) are missing" in text and "C.O.I.N.esp" in text and "Deploy" in text
    ov._finish(True)                                     # Launch anyway
    assert fired == [1]


def test_guard_cancel_does_not_launch(app):
    h = Harness(None, [])
    fired = []
    h._guard_missing_plugins(FakeGame(["X.esp"]), lambda: fired.append(1))
    h.findChildren(ConfirmOverlay)[0]._finish(False)
    assert fired == []


def test_guard_never_blocks_on_an_internal_error(app):
    h = Harness(None, [])
    fired = []
    h._guard_missing_plugins(FakeGame([], raises=True), lambda: fired.append(1))
    assert fired == [1] and any("skipped" in m for m in h.logs)


def test_games_without_the_hook_launch_normally(app):
    h = Harness(None, [])
    fired = []
    h._guard_missing_plugins(object(), lambda: fired.append(1))
    assert fired == [1]

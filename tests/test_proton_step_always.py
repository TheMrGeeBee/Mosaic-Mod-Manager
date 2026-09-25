"""'Always use this configuration' on the wizards' Choose-Proton-Version step.

One shared widget serves seven tool wizards (BethINI, xEdit, DynDOLOD, ...). With the
option on, the step continues by itself with the saved choices - after a short
countdown that "Change configuration" cancels, so it can never lock the user out."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from Utils.exe_launch import exe_launch as el  # noqa: E402
from wizards_qt import proton_step as ps  # noqa: E402


class FakeGame:
    name = "Skyrim Special Edition"

    def __init__(self, prefix=None):
        self._prefix = prefix

    def get_prefix_path(self):
        return self._prefix

    def get_game_path(self):
        return Path("/nonexistent")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch, app):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("Utils.wine_proton.steam_finder.list_installed_proton",
                        lambda: [SimpleNamespace(parent=SimpleNamespace(name=n))
                                 for n in ("proton-cachyos-slr", "GE-Proton10-1")])
    monkeypatch.setattr("Utils.wine_proton.steam_finder.find_proton_for_game", lambda _sid: None)
    monkeypatch.setattr(ps.ProtonStepWidget, "AUTO_CONTINUE_SECONDS", 2)
    exe = tmp_path / "tools" / "Bethini.exe"
    exe.parent.mkdir(parents=True)
    return FakeGame(tmp_path / "pfx"), exe


def _widget(game, exe, calls):
    (Path(game.get_prefix_path()) / "drive_c").mkdir(parents=True, exist_ok=True)
    w = ps.ProtonStepWidget(game, exe, "Bethini.exe", "BethINI Pie",
                            lambda name, mode: calls.append((name, mode)))
    return w


def _spin(seconds, cond=lambda: False):
    end = time.time() + seconds
    while time.time() < end and not cond():
        QApplication.processEvents()
        time.sleep(0.02)


def test_the_flag_round_trips_and_defaults_off(env):
    game, exe = env
    assert el.load_skip_proton_step(game, "Bethini.exe") is False
    el.save_skip_proton_step(game, "Bethini.exe", True)
    assert el.load_skip_proton_step(game, "Bethini.exe") is True
    assert el.load_skip_proton_step(game, "Other.exe") is False        # per tool
    el.save_skip_proton_step(game, "Bethini.exe", False)
    assert el.load_skip_proton_step(game, "Bethini.exe") is False


def test_the_checkbox_is_saved_when_continuing(env):
    game, exe = env
    calls = []
    w = _widget(game, exe, calls)
    assert not w._always_chk.isChecked()
    w._always_chk.setChecked(True)
    w._on_chosen()
    assert el.load_skip_proton_step(game, "Bethini.exe") is True and len(calls) == 1
    w2 = _widget(game, exe, [])
    assert w2._always_chk.isChecked()                                  # remembered


def test_without_the_option_the_step_waits_for_the_user(env):
    game, exe = env
    calls = []
    w = _widget(game, exe, calls)
    w.show()
    _spin(2.6)
    assert calls == [] and not w._auto_banner.isVisible()


def test_with_the_option_it_counts_down_and_continues_with_the_saved_choices(env):
    game, exe = env
    el.save_proton_override(game, "Bethini.exe", "GE-Proton10-1")
    el.save_skip_proton_step(game, "Bethini.exe", True)
    calls = []
    w = _widget(game, exe, calls)
    w.show()
    QApplication.processEvents()
    assert w._auto_banner.isVisible() and "continuing in" in w._auto_label.text()
    assert calls == []                                                 # not instantly: a moment to react
    _spin(3.5, lambda: calls)
    assert calls == [("GE-Proton10-1", el.PREFIX_MODE_ISOLATED)]
    assert not w._auto_banner.isVisible()


def test_change_configuration_cancels_the_countdown_and_keeps_the_page(env):
    game, exe = env
    el.save_skip_proton_step(game, "Bethini.exe", True)
    calls = []
    w = _widget(game, exe, calls)
    w.show()
    QApplication.processEvents()
    assert w._auto_banner.isVisible()
    w._auto_change_btn.click()
    assert not w._auto_banner.isVisible()
    _spin(2.8)
    assert calls == []                                                 # stayed put
    assert w._always_chk.isChecked()                                   # untick it to turn the option off
    w._always_chk.setChecked(False)
    w._on_chosen()
    assert el.load_skip_proton_step(game, "Bethini.exe") is False and len(calls) == 1


def test_a_saved_proton_that_is_no_longer_installed_shows_the_page_instead(env):
    game, exe = env
    el.save_proton_override(game, "Bethini.exe", "Removed-Proton-9")
    el.save_skip_proton_step(game, "Bethini.exe", True)
    calls = []
    w = _widget(game, exe, calls)
    w.show()
    _spin(2.6)
    assert calls == [] and not w._auto_banner.isVisible()


def test_the_game_prefix_choice_is_honoured_by_the_auto_continue(env):
    game, exe = env
    el.save_prefix_mode(game, "Bethini.exe", el.PREFIX_MODE_GAME)
    el.save_skip_proton_step(game, "Bethini.exe", True)
    calls = []
    w = _widget(game, exe, calls)
    w.show()
    _spin(3.5, lambda: calls)
    assert len(calls) == 1 and calls[0][1] == el.PREFIX_MODE_GAME


def test_the_countdown_runs_once_per_widget(env):
    game, exe = env
    el.save_skip_proton_step(game, "Bethini.exe", True)
    w = _widget(game, exe, [])
    w.show()
    QApplication.processEvents()
    w._auto_change_btn.click()
    w.hide()
    w.show()
    QApplication.processEvents()
    assert not w._auto_banner.isVisible()                              # not restarted after "Change"


def test_no_proton_installed_is_unaffected(env, monkeypatch):
    game, exe = env
    monkeypatch.setattr("Utils.wine_proton.steam_finder.list_installed_proton", lambda: [])
    el.save_skip_proton_step(game, "Bethini.exe", True)
    w = ps.ProtonStepWidget(game, exe, "Bethini.exe", "BethINI Pie", lambda *_a: None)
    w.show()
    QApplication.processEvents()                                       # must not raise
    assert w._auto_timer is None

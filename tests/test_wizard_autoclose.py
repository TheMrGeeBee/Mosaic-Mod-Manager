"""A wizard whose tool has exited and whose cleanup is done should close itself and
refresh the modlist - not sit on a dangling "Done" page. ESLifier and Pandora did:
their worker ended with "Click Done to close" while the other nine tool wizards
(BethINI, xEdit, BodySlide, ...) already closed on tool exit. (2026-09-26: a user
pressing Play while a finished-but-unclosed wizard was still running its exit
cleanup had the game killed.)"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import inspect
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


class FakeGame:
    name = "Skyrim Special Edition"

    def get_game_path(self):
        return Path("/nonexistent")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Ctx:
    def __init__(self):
        self.refreshed = 0

    def refresh_modlist(self):
        self.refreshed += 1


def _make(cls, monkeypatch, finder_module, finder_name):
    monkeypatch.setattr(finder_module, finder_name, lambda _g: None)
    closed, ctx = [], Ctx()
    view = cls(FakeGame(), log_fn=lambda _m: None, on_close=lambda: closed.append(1), ctx=ctx)
    return view, closed, ctx


def test_eslifier_closes_and_refreshes_when_its_finished_signal_fires(app, monkeypatch):
    import wizards_qt.eslifier_view as m
    view, closed, ctx = _make(m.ESLifierView, monkeypatch, m, "find_eslifier_exe")
    view._ran = True
    view._run_finished_sig.emit()
    assert closed == [1] and ctx.refreshed == 1


def test_pandora_closes_and_refreshes_when_its_finished_signal_fires(app, monkeypatch):
    import wizards_qt.pandora_view as m
    view, closed, ctx = _make(m.PandoraView, monkeypatch, m, "find_pandora_exe")
    view._ran = True
    view._run_finished_sig.emit()
    assert closed == [1] and ctx.refreshed == 1


def test_a_finished_signal_after_the_user_already_closed_is_ignored(app, monkeypatch):
    import wizards_qt.pandora_view as m
    view, closed, ctx = _make(m.PandoraView, monkeypatch, m, "find_pandora_exe")
    view._ran = True
    view._finish()
    view._run_finished_sig.emit()
    assert closed == [1] and ctx.refreshed == 1              # once, not twice


def test_both_workers_emit_the_finished_signal_after_a_clean_run():
    """Structural: the success path of each worker must reach the signal."""
    import wizards_qt.eslifier_view as e
    import wizards_qt.pandora_view as p
    assert "safe_emit(self._run_finished_sig)" in inspect.getsource(e.ESLifierView)
    src = inspect.getsource(p.PandoraView._start_run)
    ok_branch = src.split("else:", 1)[1].split("except Exception", 1)[0]
    assert "safe_emit(self._run_finished_sig)" in ok_branch
    err_branch = src.split("if rc != 0:", 1)[1].split("else:", 1)[0]
    assert "_run_finished_sig" not in err_branch             # an error page stays open to be read


def test_no_tool_wizard_tells_the_user_to_click_done_when_it_closes_by_itself():
    import wizards_qt.bethini_view as b
    src = inspect.getsource(b)
    assert "click Done" not in src and "closes by itself" in src

"""The preflight overlay only renders and reports; these pin its state machine:
which primary action it offers for a given set of checks, that a running fix can't
be abandoned or double-fired, and what ``on_done`` receives.

Runs headless (offscreen Qt platform) — no window is ever shown on screen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from gui_qt.collections.collection_preflight_overlay import PreflightOverlay  # noqa: E402
from Utils.collections.collection_preflight import FIX_RUNTIME_SWAP, Check  # noqa: E402

FIXABLE = Check("skyrim-runtime", False, "Skyrim is 1.7.104.0, this collection needs 1.6.1170.0",
                "detail <b>escaped</b>", fix=FIX_RUNTIME_SWAP)
UNFIXABLE = Check("skyrim-runtime", False, "Unsupported Skyrim version (1.6.640.0)", "verify in Steam")
WARNING = Check("disk-space", False, "Disk space is tight", "may not fit", blocking=False)
OK = Check("disk-space", True, "Enough free disk space")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def host(app):
    w = QMainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    w.close()


def _overlay(host, checks):
    calls = {"fix": 0, "done": []}
    ov = PreflightOverlay(host, checks, lambda _ov: calls.__setitem__("fix", calls["fix"] + 1),
                          lambda r: calls["done"].append(r))
    return ov, calls


def test_fixable_blocker_offers_fix_and_continue(host):
    ov, calls = _overlay(host, [FIXABLE, OK])
    assert ov._mode == "fix" and ov._go.text() == "Fix and continue"
    assert ov._cancel.text() == "Cancel"
    ov._on_go()
    assert calls["fix"] == 1


def test_unfixable_blocker_offers_only_close(host):
    ov, calls = _overlay(host, [UNFIXABLE])
    assert ov._mode == "close" and not ov._go.isVisibleTo(ov) and ov._cancel.text() == "Close"
    ov._on_go()
    assert calls["fix"] == 0


def test_a_fixable_blocker_next_to_an_unfixable_one_cannot_be_fixed_away(host):
    ov, _ = _overlay(host, [FIXABLE, UNFIXABLE])
    assert ov._mode == "close"


def test_warnings_only_offers_continue_anyway(host):
    ov, calls = _overlay(host, [WARNING, OK])
    assert ov._mode == "continue" and ov._go.text() == "Continue anyway"
    ov._on_go()
    assert calls["done"] == ["continue"]


def test_cancel_reports_none(host):
    ov, calls = _overlay(host, [FIXABLE])
    ov._cancel.click()
    assert calls["done"] == [None]


def test_busy_blocks_double_fire_cancel_and_escape(host):
    ov, calls = _overlay(host, [FIXABLE])
    ov._on_go()
    ov.set_busy("Switching the game version…")
    assert not ov._go.isEnabled() and not ov._cancel.isEnabled()
    ov._on_go()
    assert calls["fix"] == 1                              # no second fix while one runs
    QTest.keyClick(ov, Qt.Key_Escape)
    assert calls["done"] == []                            # Esc ignored mid-fix
    assert ov._status.text() == "Switching the game version…"


def test_error_reenables_the_buttons_and_shows_the_message(host):
    ov, calls = _overlay(host, [FIXABLE])
    ov.set_busy("working")
    ov.set_error("Skyrim is running — close it first.")
    assert ov._go.isEnabled() and ov._cancel.isEnabled()
    assert ov._status.text() == "Skyrim is running — close it first."
    ov._on_go()
    assert calls["fix"] == 1                              # retry is possible


def test_recheck_that_clears_everything_continues(host):
    ov, calls = _overlay(host, [FIXABLE])
    ov.set_busy("Checking the game again…")
    ov.close_ok()
    assert calls["done"] == ["continue"]


def test_set_checks_after_a_fix_switches_the_action(host):
    ov, _ = _overlay(host, [FIXABLE])
    ov.set_checks([WARNING])
    assert ov._mode == "continue"


def test_detail_text_is_html_escaped(host):
    ov, _ = _overlay(host, [FIXABLE])
    text = ov._body.text()
    assert "&lt;b&gt;escaped&lt;/b&gt;" in text and "<b>escaped</b>" not in text


def test_finishing_twice_only_reports_once(host):
    ov, calls = _overlay(host, [WARNING])
    ov._on_go()
    ov._cancel.click()
    assert calls["done"] == ["continue"]

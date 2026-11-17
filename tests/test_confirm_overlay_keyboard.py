"""ConfirmOverlay is usable from the keyboard: Tab / arrows move between the
buttons, Enter presses the focused one, and a destructive prompt starts on
Cancel."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton

from gui_qt.overlays.confirm_overlay import ConfirmOverlay


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def host(qapp):
    import shiboken6
    h = QMainWindow()
    h.resize(800, 600)
    behind = QPushButton("behind", h)
    behind.setFocusPolicy(Qt.StrongFocus)
    h.setCentralWidget(behind)
    h.show()
    behind.setFocus()
    qapp.processEvents()
    yield h
    h.close()
    qapp.processEvents()
    if shiboken6.isValid(h):
        shiboken6.delete(h)
    qapp.processEvents()


def _open(host, **kw):
    results = []
    ov = ConfirmOverlay(host, "Remove", "Sure?", results.append, **kw)
    QApplication.processEvents()
    return ov, results


def _press(ov, key, mods=Qt.NoModifier):
    QTest.keyClick(QApplication.focusWidget() or ov, key, mods)
    QApplication.processEvents()


def test_destructive_prompt_starts_on_cancel(host):
    ov, _ = _open(host)
    assert QApplication.focusWidget().text() == "Cancel"


def test_enter_on_the_default_cancels(host):
    ov, results = _open(host)
    _press(ov, Qt.Key_Return)
    assert results == [False]


def test_tab_then_enter_confirms(host):
    ov, results = _open(host)
    _press(ov, Qt.Key_Tab)
    assert QApplication.focusWidget().text() == "Remove"
    _press(ov, Qt.Key_Return)
    assert results == [True]


def test_arrow_keys_move_between_buttons_and_wrap(host):
    ov, results = _open(host)
    _press(ov, Qt.Key_Right)
    assert QApplication.focusWidget().text() == "Remove"
    _press(ov, Qt.Key_Right)
    assert QApplication.focusWidget().text() == "Cancel"
    _press(ov, Qt.Key_Left)
    assert QApplication.focusWidget().text() == "Remove"
    _press(ov, Qt.Key_Enter)
    assert results == [True]


def test_shift_tab_goes_back(host):
    ov, _ = _open(host)
    _press(ov, Qt.Key_Backtab, Qt.ShiftModifier)
    assert QApplication.focusWidget().text() == "Remove"


def test_tab_never_leaves_the_overlay(host):
    ov, _ = _open(host)
    for _i in range(5):
        _press(ov, Qt.Key_Tab)
        assert QApplication.focusWidget().text() in ("Cancel", "Remove")


def test_escape_still_cancels(host):
    ov, results = _open(host)
    _press(ov, Qt.Key_Escape)
    assert results == [False]


def test_ok_only_message_focuses_ok_and_enter_accepts(host):
    results = []
    ov = ConfirmOverlay.show_message(host, "Hi", "Done", results.append)
    QApplication.processEvents()
    assert QApplication.focusWidget().text() == "OK"
    _press(ov, Qt.Key_Return)
    assert results == [True]


def test_non_danger_prompt_starts_on_confirm(host):
    ov, results = _open(host, danger=False, confirm_label="Yes")
    assert QApplication.focusWidget().text() == "Yes"
    _press(ov, Qt.Key_Return)
    assert results == [True]

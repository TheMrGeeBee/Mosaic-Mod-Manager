"""An overlay takes keyboard focus when it opens. Real case (2026-10-23): during
a Wabbajack install, Space opened the game picker behind the overlay, because
focus stayed on the toolbar button the user had last clicked."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow, QPushButton

from gui_qt.overlay_base import OverlayBase


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _teardown(app, host):
    import shiboken6
    host.close()
    app.processEvents()
    if shiboken6.isValid(host):
        shiboken6.delete(host)
    app.processEvents()


class _Plain(OverlayBase):
    def __init__(self, host):
        super().__init__(host)
        self._make_card("PlainCard")
        self._present()


class _WithField(OverlayBase):
    def __init__(self, host):
        super().__init__(host)
        _card, v = self._make_card("FieldCard")
        self.field = QLineEdit()
        v.addWidget(self.field)
        self.field.setFocus()
        self._present()


def _host(qapp):
    host = QMainWindow()
    host.resize(800, 600)
    button = QPushButton("Game", host)
    button.setFocusPolicy(Qt.StrongFocus)
    host.setCentralWidget(button)
    host.show()
    button.setFocus()
    qapp.processEvents()
    return host, button


def test_overlay_takes_focus_from_the_widget_behind_it(qapp):
    host, button = _host(qapp)
    try:
        overlay = _Plain(host)
        qapp.processEvents()
        assert overlay.focusWidget() is overlay or overlay.hasFocus()
        assert not button.hasFocus()
    finally:
        _teardown(qapp, host)


def test_overlay_keeps_a_field_its_subclass_focused(qapp):
    host, _button = _host(qapp)
    try:
        overlay = _WithField(host)
        qapp.processEvents()
        assert overlay.focusWidget() is overlay.field
    finally:
        _teardown(qapp, host)

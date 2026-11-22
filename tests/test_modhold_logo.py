"""The Modhold logo assets the header button relies on."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtGui import QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ICONS = Path(__file__).resolve().parents[1] / "src" / "icons"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_svg_is_valid_and_is_the_unmodified_source(app):
    svg = ICONS / "modhold.svg"
    assert QSvgRenderer(str(svg)).isValid()
    assert "e0a84e" in svg.read_text(encoding="utf-8")


def test_png_exists_and_loads_with_transparency(app):
    pm = QPixmap(str(ICONS / "modhold.png"))
    assert not pm.isNull() and pm.width() == pm.height() >= 128
    assert pm.toImage().hasAlphaChannel()


def test_header_icon_helper_finds_it(app):
    from gui_qt.icons import icon
    assert not icon("modhold.png", 22).isNull()

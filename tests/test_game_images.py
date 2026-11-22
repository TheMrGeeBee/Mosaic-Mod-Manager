"""Game artwork the game picker shows (src/icons/games/<game_id>.png)."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

ICONS = Path(__file__).resolve().parent.parent / "src" / "icons" / "games"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_vampire_bloodlines_has_a_square_image():
    from PIL import Image
    path = ICONS / "vampire_bloodlines.png"
    assert path.is_file()
    width, height = Image.open(path).size
    assert width == height >= 256                      # the picker center-crops to a square


def test_the_game_picker_loads_the_vampire_bloodlines_image(qapp):
    from gui_qt.views.add_game_view import IMG_SQ, _game_logo
    pm = _game_logo("vampire_bloodlines", IMG_SQ)
    assert pm is not None and not pm.isNull() and pm.width() == pm.height() == IMG_SQ


def test_the_image_matches_the_game_id_the_game_reports():
    import re
    src = (Path(__file__).resolve().parent.parent / "src" / "Games"
           / "Vampire The Masquerade - Bloodlines" / "vampire_bloodlines.py").read_text(encoding="utf-8")
    m = re.search(r"def game_id\(self\)[^\n]*\n(?:\s*\"\"\".*?\"\"\"\s*\n)?\s*return \"([^\"]+)\"", src, re.S)
    assert m and (ICONS / f"{m.group(1)}.png").is_file(), "the game's id has no image file"

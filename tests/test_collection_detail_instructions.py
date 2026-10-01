"""Tests for CollectionDetailView surfacing a collection's "instructions"
fields on import/display -- previously write-only.

Real gap, confirmed by grepping the whole codebase: collection_export.py
writes mods[].source.instructions and info.installInstructions,
create_collection_view.py lets a curator type them, and nexus_api.py's
publish payload carries them -- but nothing on the import/install side
ever read them back. A real Vortex-authored collection (or Mosaic's own
round-tripped export) that relies on either field lost that guidance
completely on import, with no error or warning."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from gui_qt.collections.collection_detail_view import CollectionDetailView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _collection(slug=""):
    return SimpleNamespace(name="Test Collection", slug=slug, user_name="",
                           summary="", game_domain="baldursgate3")


def _view(qapp, request, manifest):
    game = SimpleNamespace(name="BG3", nexus_game_domain="baldursgate3")
    host = QMainWindow()
    view = CollectionDetailView(None, _collection(), game, local_manifest=manifest)
    host.setCentralWidget(view)
    request.addfinalizer(lambda: (host.close(), qapp.processEvents()))
    return view


def _manifest(mods, install_instructions=""):
    return {"info": {"installInstructions": install_instructions}, "mods": mods}


def test_collection_level_instructions_shown(qapp, request):
    view = _view(qapp, request, _manifest(
        [{"name": "A", "source": {"type": "nexus", "modId": 1, "fileId": 2}}],
        install_instructions="Run BG3SE Configs before playing."))

    # isVisible() reflects actual on-screen visibility (needs host.show()),
    # not just this widget's own setVisible() call -- isHidden() does.
    assert not view._instructions_wrap.isHidden()
    assert view._instructions_lbl.text() == "Run BG3SE Configs before playing."


def test_collection_level_instructions_render_markdown(qapp, request):
    # The authoring field (create_collection_view.py) tells curators
    # Markdown is supported, matching real Nexus Collections -- the display
    # side must actually render it (bold/headings), not show raw syntax.
    from PySide6.QtCore import Qt
    view = _view(qapp, request, _manifest(
        [{"name": "A", "source": {"type": "nexus", "modId": 1, "fileId": 2}}],
        install_instructions="**Important:** read this first."))

    assert view._instructions_lbl.textFormat() == Qt.MarkdownText


def test_no_collection_instructions_hides_the_banner(qapp, request):
    view = _view(qapp, request, _manifest(
        [{"name": "A", "source": {"type": "nexus", "modId": 1, "fileId": 2}}]))

    assert view._instructions_wrap.isHidden()


def test_per_mod_instructions_reach_the_mod_object(qapp, request):
    view = _view(qapp, request, _manifest([
        {"name": "Better Hotbar 2 [Vova's Edition]", "optional": True,
         "source": {"type": "nexus", "modId": 1, "fileId": 2,
                   "instructions": "You must choose this or (Vova's Edition) Better Hotbar 2"}},
        {"name": "(Vova's Edition) Better Hotbar 2", "optional": True,
         "source": {"type": "nexus", "modId": 1, "fileId": 3}},
    ]))

    by_name = {m.mod_name: m for m in view._mods}
    assert by_name["Better Hotbar 2 [Vova's Edition]"].instructions == \
        "You must choose this or (Vova's Edition) Better Hotbar 2"
    assert by_name["(Vova's Edition) Better Hotbar 2"].instructions == ""


def test_mod_with_instructions_gets_a_marker_in_the_table(qapp, request):
    view = _view(qapp, request, _manifest([
        {"name": "A", "source": {"type": "nexus", "modId": 1, "fileId": 2,
                                 "instructions": "Pick this or B."}},
    ]))

    item = view._table.item(0, 0)
    assert "📝" in item.text()
    # Rendered to HTML (so Markdown in the source actually formats, per
    # _markdown_tooltip) rather than shown as a raw-text tooltip -- check
    # the content made it through, not an exact plain-text match.
    assert "Pick this or B." in item.toolTip()
    assert "<html" in item.toolTip().lower()


def test_mod_without_instructions_has_no_marker(qapp, request):
    view = _view(qapp, request, _manifest(
        [{"name": "A", "source": {"type": "nexus", "modId": 1, "fileId": 2}}]))

    item = view._table.item(0, 0)
    assert "📝" not in item.text()
    assert item.toolTip() == ""


def test_optional_checkbox_marks_and_shows_instructions(qapp, request):
    view = _view(qapp, request, _manifest([
        {"name": "A", "optional": True,
         "source": {"type": "nexus", "modId": 1, "fileId": 2,
                   "instructions": "Pick this or B."}},
    ]))

    cb, _fid = view._opt_boxes[0]
    assert "📝" in cb.text()
    assert "Pick this or B." in cb.toolTip()


def test_per_mod_markdown_actually_renders_bold(qapp, request):
    view = _view(qapp, request, _manifest([
        {"name": "A", "source": {"type": "nexus", "modId": 1, "fileId": 2,
                                 "instructions": "**Important:** read this."}},
    ]))

    tooltip = view._table.item(0, 0).toolTip()
    # Markdown's "**Important:**" should become real bold styling, not show
    # up as literal asterisks in the rendered HTML (Qt's own Markdown-to-
    # richtext conversion emits "font-weight:700", not a <strong> tag).
    assert "font-weight:700" in tooltip
    assert "**Important:**" not in tooltip

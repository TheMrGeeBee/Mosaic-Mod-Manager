"""Collapse / Expand on separators: all, and just the selected ones.

The footer already had a single Expand-all/Collapse-all toggle, but there was
no way to collapse one chosen heading (other than clicking it) or a selected
block of them, and no explicit "Expand all" when the toggle had flipped the
other way. These cover the model's bulk API and the context-menu items built
on top of it.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME
from Utils.mods.modlist import ModEntry
from gui_qt.modlist.modlist_menu import _collapse_items
from gui_qt.modlist.modlist_model import ModListModel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _sep(name):
    return ModEntry(name=f"{name}_separator", enabled=True, locked=True,
                    is_separator=True)


def _mod(name):
    return ModEntry(name=name, enabled=True, locked=False)


def _model():
    return ModListModel([
        ModEntry(ROOT_FOLDER_NAME, True, True, True),
        _sep("Graphics"), _mod("Textures"), _mod("Grass"),
        _sep("Patches"), _mod("Unofficial Patch"),
        _sep("Audio"), _mod("Music"),
        ModEntry(OVERWRITE_NAME, True, True, True),
    ])


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

def test_only_the_named_separators_collapse(qapp):
    m = _model()
    m.set_collapsed_names(["Graphics", "Audio"], True)
    assert m.collapsed_names() == {"Graphics", "Audio"}
    assert m.is_collapsed("Graphics") and not m.is_collapsed("Patches")


def test_expanding_a_selection_leaves_the_rest_collapsed(qapp):
    m = _model()
    m.set_all_collapsed(True)
    m.set_collapsed_names(["Patches"], False)
    assert m.collapsed_names() == {"Graphics", "Audio"}


def test_the_overwrite_and_root_boundaries_never_collapse(qapp):
    """They have no block of their own to hide, and a stale entry for one in
    the persisted set must not swallow the mods below it."""
    m = _model()
    m.set_collapsed_names([ROOT_FOLDER_NAME, OVERWRITE_NAME, "Graphics"], True)
    assert m.collapsed_names() == {"Graphics"}
    m.set_all_collapsed(True)
    assert m.collapsed_names() == {"Graphics", "Patches", "Audio"}


def test_an_unknown_name_is_ignored(qapp):
    m = _model()
    m.set_collapsed_names(["Not A Separator", "Textures"], True)
    assert m.collapsed_names() == set()


def test_a_bulk_collapse_drops_the_separator_tint_cache(qapp):
    """A separator is tinted only while collapsed, and data() answers from
    this cache on every paint -- so Collapse all with a highlight selection
    live used to keep painting the stale untinted answer."""
    m = _model()
    m._sep_hl_cache[1] = 2
    m.set_all_collapsed(True)
    assert m._sep_hl_cache == {}


# ---------------------------------------------------------------------------
# context menu
# ---------------------------------------------------------------------------

class _FakeView:
    """Records what the menu items would do to the real view."""

    def __init__(self):
        self.calls = []

    def set_all_collapsed(self, collapsed):
        self.calls.append(("all", collapsed))

    def set_rows_collapsed(self, rows, collapsed):
        self.calls.append((tuple(rows), collapsed))


def _items(model, rows, multi):
    """Build the collapse group and return {label: slot}."""
    out = {}

    def act(label, slot, enabled=True, shortcut=None):
        out[label] = slot

    view = _FakeView()
    _collapse_items(view, model, rows, multi, act, lambda: None)
    return view, out


def test_one_separator_offers_collapse_then_expand(qapp):
    m = _model()
    _view, items = _items(m, [1], multi=False)
    assert "Collapse" in items and "Expand" not in items

    m.set_collapsed_names(["Graphics"], True)
    _view, items = _items(m, [1], multi=False)
    assert "Expand" in items and "Collapse" not in items


def test_collapse_all_is_offered_until_everything_is_collapsed(qapp):
    m = _model()
    _view, items = _items(m, [1], multi=False)
    assert "Collapse all" in items and "Expand all" not in items

    m.set_all_collapsed(True)
    _view, items = _items(m, [1], multi=False)
    assert "Expand all" in items and "Collapse all" not in items


def test_a_partly_collapsed_list_offers_both(qapp):
    m = _model()
    m.set_collapsed_names(["Graphics"], True)
    _view, items = _items(m, [1], multi=False)
    assert "Collapse all" in items and "Expand all" in items


def test_several_selected_separators_get_counted_items(qapp):
    m = _model()
    view, items = _items(m, [1, 4, 6], multi=True)
    assert "Collapse selected (3)" in items
    assert "Expand selected (3)" not in items      # none of them is collapsed
    items["Collapse selected (3)"]()
    assert view.calls == [((1, 4, 6), True)]


def test_a_mixed_selection_offers_both_directions(qapp):
    m = _model()
    m.set_collapsed_names(["Graphics"], True)
    view, items = _items(m, [1, 4], multi=True)
    items["Expand selected (2)"]()
    items["Collapse selected (2)"]()
    assert view.calls == [((1, 4), False), ((1, 4), True)]


def test_the_all_items_act_on_everything_not_the_selection(qapp):
    m = _model()
    view, items = _items(m, [1], multi=False)
    items["Collapse all"]()
    assert view.calls == [("all", True)]

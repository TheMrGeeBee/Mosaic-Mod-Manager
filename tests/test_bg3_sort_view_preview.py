"""Test for BG3SortView._on_preview_ready rendering collection-governed
mods in the review tree.

Real bug, reported after a fresh install from a real exported collection:
the Sort Load Order pane's summary correctly said "40 mod(s) follow your
collection's order and are not moved", but the list right below it was
completely empty -- confusing, since the pane's whole purpose is to let
the user review the resulting order. Root cause: compute_layered_plan()
(Utils/mods/bg3_sort.py) deliberately never adds a collection-governed mod
to plan.layers (deploy orders those by the manifest regardless of
modlist.txt -- correct), but the view's tree-building loop skipped any
mod not in plan.layers entirely, silently dropping all of them from
the display along with the ones genuinely left out by design.

_on_preview_ready only touches self.tr/_tree/_load_after_map/_set_status/
_apply_btn/_after_box, so it's tested against a lightweight stand-in with
real-but-minimal Qt widgets for those, rather than constructing a full
BG3SortView (which would need a real game + profile just to compute its
own initial preview in __init__)."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QLabel, QPushButton, QTreeWidget,
)

from Utils.mods.bg3_sort import SortPlan
from wizards_qt.bg3_sort_view import BG3SortView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeSortView:
    def tr(self, text):
        return text

    def _set_status(self, _lbl, _text, _color=""):
        pass

    def _set_mod_actions(self, _mod):
        pass


def _make_view(qapp):
    v = _FakeSortView()
    v._tree = QTreeWidget()
    v._tree.setColumnCount(3)
    v._preview_summary = QLabel()
    v._apply_btn = QPushButton()
    v._after_box = QComboBox()
    v._load_after_map = {}
    return v


def _top_level_labels(tree):
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def test_collection_governed_mods_appear_in_their_own_group(qapp):
    view = _make_view(qapp)
    plan = SortPlan(
        new_entries=[],
        layers={"Free Mod": ("gameplay", "tweak")},
        load_order=["Collection Mod A", "Collection Mod B", "Free Mod"],
        previous_load_order=["Collection Mod A", "Collection Mod B", "Free Mod"],
        collection_count=2,
    )

    BG3SortView._on_preview_ready(view, plan)

    labels = _top_level_labels(view._tree)
    assert any("collection's order" in lbl.lower() for lbl in labels)
    coll_group = next(view._tree.topLevelItem(i)
                      for i in range(view._tree.topLevelItemCount())
                      if "collection's order" in view._tree.topLevelItem(i).text(0).lower())
    child_names = [coll_group.child(i).text(0).strip()
                  for i in range(coll_group.childCount())]
    assert child_names == ["Collection Mod A", "Collection Mod B"]


def test_no_collection_mods_means_no_collection_group(qapp):
    view = _make_view(qapp)
    plan = SortPlan(
        new_entries=[],
        layers={"Free Mod": ("gameplay", "tweak")},
        load_order=["Free Mod"],
        previous_load_order=["Free Mod"],
        collection_count=0,
    )

    BG3SortView._on_preview_ready(view, plan)

    labels = _top_level_labels(view._tree)
    assert not any("collection's order" in lbl.lower() for lbl in labels)


def test_free_mods_still_render_normally_alongside_collection_group(qapp):
    view = _make_view(qapp)
    plan = SortPlan(
        new_entries=[],
        layers={"Free Mod": ("gameplay", "tweak")},
        load_order=["Collection Mod A", "Free Mod"],
        previous_load_order=["Collection Mod A", "Free Mod"],
        collection_count=1,
    )

    BG3SortView._on_preview_ready(view, plan)

    all_text = []
    for i in range(view._tree.topLevelItemCount()):
        top = view._tree.topLevelItem(i)
        for j in range(top.childCount()):
            all_text.append(top.child(j).text(0).strip())
    assert "Collection Mod A" in all_text
    assert "Free Mod" in all_text

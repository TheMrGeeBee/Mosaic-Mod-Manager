"""Tests for CreateCollectionView's bulk-selection toolbar (Select column +
Optional/Update Policy bulk apply).

Requested live: with a long modlist, checking each mod's Optional/Update
Policy one at a time is impractical when several mods need the same
setting. Adds a per-row Select checkbox and toolbar actions (Select all/
none, scoped to the current search filter; bulk-set Optional; bulk-apply
Update Policy)."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from gui_qt.views.create_collection_view import CreateCollectionView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeGame:
    def __init__(self, staging_root, profile_dir):
        self._staging_root = str(staging_root)
        self._active_profile_dir = str(profile_dir)
        self.name = "TestGame"
        self.nexus_game_domain = "testgame"

    def get_effective_mod_staging_path(self):
        return self._staging_root


def _make_view(qapp, tmp_path, names, request):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    (profile_dir / "modlist.txt").write_text(
        "\n".join(f"+{n}" for n in names) + "\n", encoding="utf-8")
    for n in names:
        d = staging / n
        d.mkdir()
        (d / "meta.ini").write_text(
            "[General]\nmodid = 111\nfileid = 222\nversion = 1.0\n", encoding="utf-8")
    game = _FakeGame(staging, profile_dir)
    host = QMainWindow()
    window = type("W", (), {"_notify": lambda self, *a: None})()
    view = CreateCollectionView(window, game, None, log_fn=lambda *_a: None)
    host.setCentralWidget(view)
    # An un-closed QMainWindow's C++ object can outlive the Python test
    # function (GC timing, not deterministic), and a later test's
    # processEvents() can then dispatch into an event filter still installed
    # on it -- confirmed live: this crashed an UNRELATED test file's
    # processEvents() call with "libshiboken: ... already deleted" once
    # enough of these accumulated across a test session. Close explicitly.
    request.addfinalizer(lambda: (host.close(), qapp.processEvents()))
    return host, view


def test_selection_starts_empty(qapp, tmp_path, request):
    _host, view = _make_view(qapp, tmp_path, ["Mod A", "Mod B", "Mod C"], request)
    assert view._selected == set()
    assert view._selected_count_label.text() == "0 selected"


def test_bulk_set_optional_only_affects_selected_rows(qapp, tmp_path, request):
    _host, view = _make_view(
        qapp, tmp_path, ["Mod A", "Mod B", "Mod C", "Mod D", "Mod E"], request)

    view._set_selected(0, True)
    view._set_selected(2, True)
    view._set_selected(4, True)
    assert view._selected_count_label.text() == "3 selected"

    view._bulk_set_optional(True)

    assert view._all_rows[0]["optional"] is True
    assert view._all_rows[2]["optional"] is True
    assert view._all_rows[4]["optional"] is True
    assert view._all_rows[1]["optional"] is False
    assert view._all_rows[3]["optional"] is False
    # Selection must survive the rebuild the bulk action triggers.
    assert view._selected == {0, 2, 4}


def test_bulk_apply_update_policy_only_affects_selected_rows(qapp, tmp_path, request):
    _host, view = _make_view(qapp, tmp_path, ["Mod A", "Mod B", "Mod C"], request)

    view._set_selected(0, True)
    view._set_selected(1, True)
    idx = view._bulk_policy_combo.findData("latest")
    view._bulk_policy_combo.setCurrentIndex(idx)

    view._bulk_apply_update_policy()

    assert view._all_rows[0]["update_policy"] == "latest"
    assert view._all_rows[1]["update_policy"] == "latest"
    assert view._all_rows[2]["update_policy"] == "exact"


def test_select_all_is_scoped_to_the_search_filter(qapp, tmp_path, request):
    _host, view = _make_view(
        qapp, tmp_path, ["Apple Mod", "Banana Mod", "Apricot Mod"], request)

    view._search.setText("ap")
    view._on_search("ap")
    view._select_all_visible()

    selected_names = {view._all_rows[i]["name"] for i in view._selected}
    assert selected_names == {"Apple Mod", "Apricot Mod"}


def test_select_none_clears_everything(qapp, tmp_path, request):
    _host, view = _make_view(qapp, tmp_path, ["Mod A", "Mod B"], request)
    view._select_all_visible()
    assert len(view._selected) == 2

    view._select_none()

    assert view._selected == set()
    assert view._selected_count_label.text() == "0 selected"


def test_removing_a_variant_reindexes_the_selection(qapp, tmp_path, request):
    """self._selected stores list indices into self._all_rows -- removing a
    variant row shifts everything after it, so the selection must re-index
    or the bulk toolbar would silently act on the wrong mod."""
    _host, view = _make_view(qapp, tmp_path, ["Mod A", "Mod B"], request)
    base_idx = 0
    base_row = view._all_rows[base_idx]
    other_idx = 1 - base_idx
    base_row["mod_id"] = 111
    file_1 = SimpleNamespace(file_id=901, version="2K", name="2K",
                             size_in_bytes=1, size_kb=None)
    file_2 = SimpleNamespace(file_id=902, version="4K", name="4K",
                             size_in_bytes=1, size_kb=None)
    view._add_variant_row(base_row, file_1)  # index 2
    view._add_variant_row(base_row, file_2)  # index 3

    view._set_selected(other_idx, True)  # the un-varianted mod
    view._set_selected(3, True)          # the second (4K) variant
    assert view._selected == {other_idx, 3}

    view._remove_variant(2)              # removes the first (2K) variant

    assert view._selected == {other_idx, 2}   # index 3 shifted down to 2
    assert view._all_rows[2]["name"] == f"{base_row['name']} (4K)"

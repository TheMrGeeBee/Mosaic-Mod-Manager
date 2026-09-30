"""Tests for CreateCollectionView's mod.io version picker.

Caught live: the Version/File column in the Create Collection table always
showed a static "—" (or a bare version string with no other options) for a
mod.io-identified mod, since the fetch-on-open logic only ever queried
Nexus's get_mod_files. Extended to also fetch mod.io's real file list for
mod.io rows -- but naively reusing the Nexus-oriented _on_versions_ready
auto-select logic (which assumes a "fileid — version" label and jumps to
options[0], the newest file) would have silently changed the pinned version
to mod.io's newest UPLOAD the moment the fetch completed, even though
mod.io's "newest upload" isn't necessarily the author's live release (see
the modio_update_checker.py downgrade-guard fix) -- and would have stuffed a
mod.io file id into the row's Nexus-only file_id field. Both are guarded
against here.
"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from gui_qt.views.create_collection_view import CreateCollectionView
from gui_qt.views.mod_row_widgets import VersionOverlay


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeGame:
    def __init__(self, staging_root, profile_dir):
        self._staging_root = str(staging_root)
        self._active_profile_dir = str(profile_dir)
        self.name = "BG3"
        self.nexus_game_domain = "baldursgate3"

    def get_effective_mod_staging_path(self):
        return self._staging_root


def _make_view(qapp, tmp_path, request):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    (profile_dir / "modlist.txt").write_text("+Better Hotbar\n", encoding="utf-8")
    meta_dir = staging / "Better Hotbar"
    meta_dir.mkdir()
    (meta_dir / "meta.ini").write_text(
        "[General]\nmodioModId = 4343518\nmodioProfileUrl = https://mod.io/x\n"
        "modioVersion = 2.0.0.61\n",
        encoding="utf-8")
    game = _FakeGame(staging, profile_dir)
    host = QMainWindow()
    window = type("W", (), {"_notify": lambda self, *a: None})()
    view = CreateCollectionView(window, game, None, log_fn=lambda *_a: None)
    host.setCentralWidget(view)
    # An un-closed QMainWindow's C++ object can outlive the Python test
    # function (GC timing, not deterministic), and a later test's
    # processEvents() can then dispatch into an event filter still installed
    # on it -- confirmed live: this crashed with "libshiboken: ... already
    # deleted" once enough of these accumulated across a test session.
    request.addfinalizer(lambda: (host.close(), qapp.processEvents()))
    return host, view


def test_modio_row_has_mod_id_and_seeded_version(qapp, tmp_path, request):
    host, view = _make_view(qapp, tmp_path, request)
    row = view._all_rows[0]
    assert row["is_modio"] is True
    assert row["modio_mod_id"] == 4343518
    assert row["ver_label"] == "2.0.0.61"


def test_modio_row_defaults_to_modio_source_not_bundle(qapp, tmp_path, request):
    """profile_export.load_rows() defaults a mod.io mod to "bundle" (the
    right call for Export Profile's self-contained sharing) -- Create
    Collection has a real mod.io page/file list to reference instead, so it
    should override that default rather than requiring a manual switch."""
    host, view = _make_view(qapp, tmp_path, request)
    row = view._all_rows[0]
    assert row["source"] == "modio"


def test_modio_versions_ready_does_not_auto_jump_to_newest(qapp, tmp_path, request):
    """A fetch completing must not silently change the pinned version --
    mod.io's newest upload isn't necessarily the live/intended release."""
    host, view = _make_view(qapp, tmp_path, request)
    row = view._all_rows[0]
    options = [
        {"label": "2.0.0.63", "name": "f63.zip", "size_bytes": 1000, "modio_file_id": 7430399},
        {"label": "2.0.0.61", "name": "f61.zip", "size_bytes": 900, "modio_file_id": 7430300},
    ]

    view._on_versions_ready(0, options)

    assert row["ver_options"] == options
    assert row["ver_label"] == "2.0.0.61"
    assert row["version"] == "2.0.0.61"


def test_picking_a_modio_version_updates_version_not_nexus_file_id(qapp, tmp_path, request):
    host, view = _make_view(qapp, tmp_path, request)
    row = view._all_rows[0]
    row["ver_options"] = [
        {"label": "2.0.0.63", "name": "f63.zip", "size_bytes": 1000, "modio_file_id": 7430399},
        {"label": "2.0.0.61", "name": "f61.zip", "size_bytes": 900, "modio_file_id": 7430300},
    ]

    view._open_version_dialog(0)
    qapp.processEvents()
    overlay = host.findChild(VersionOverlay)
    assert overlay is not None
    overlay._list.setCurrentRow(0)  # the 2.0.0.63 option
    overlay._apply()
    qapp.processEvents()

    assert row["ver_label"] == "2.0.0.63"
    assert row["version"] == "2.0.0.63"
    assert row["modio_file_id"] == 7430399
    # Nexus's file_id field must stay untouched -- a mod.io file id is a
    # different id space and has no meaning there.
    assert row["file_id"] == 0


def test_open_dialog_then_fetch_completing_refreshes_the_open_overlay(qapp, tmp_path, request):
    """Caught live: the dialog opens immediately with just the placeholder
    entry (the fetch hasn't had time to complete yet), and previously had no
    way to pick up the real list once the fetch DID complete a moment later
    -- the user had to close and reopen it. set_options()/_version_overlay
    tracking must push a live update into the still-open dialog instead."""
    host, view = _make_view(qapp, tmp_path, request)

    view._open_version_dialog(0)
    qapp.processEvents()
    overlay = host.findChild(VersionOverlay)
    assert overlay is not None
    assert overlay._list.count() == 1  # just the placeholder so far

    view._on_versions_ready(0, [
        {"label": "2.0.0.63", "name": "f63.zip", "size_bytes": 1000, "modio_file_id": 7430399},
        {"label": "2.0.0.61", "name": "f61.zip", "size_bytes": 900, "modio_file_id": 7430300},
    ])
    qapp.processEvents()

    assert overlay._list.count() == 2

    # An overlay left open (never _apply()/_cancel()'d) keeps its event
    # filter installed on `host` — closing it explicitly avoids a dangling
    # filter that can crash a LATER test's processEvents() once host is
    # eventually torn down (confirmed: this was intermittently crashing
    # test_create_collection_modio_versions.py itself when run after other
    # Qt-heavy test files, with "libshiboken: ... already deleted").
    overlay._cancel()
    qapp.processEvents()

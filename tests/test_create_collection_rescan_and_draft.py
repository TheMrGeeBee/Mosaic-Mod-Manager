"""Tests for CreateCollectionView's Rescan button and background draft
autosave/restore.

Requested directly: filling in per-mod settings and notes in Create
Collection, then realizing a mod was forgotten, meant closing and
reopening the tab to pick it up -- which discarded every edit already
made. Two asks, both implemented: a "Rescan" button (like Load Order
Insights') that re-reads the modlist and merges in what changed without
losing existing edits, and a background autosave so even an accidental
close doesn't lose the work."""
from __future__ import annotations

import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
        self.name = "BG3"
        self.nexus_game_domain = "baldursgate3"

    def get_effective_mod_staging_path(self):
        return self._staging_root


def _write_modlist(profile_dir, names):
    (profile_dir / "modlist.txt").write_text(
        "\n".join(f"+{n}" for n in names) + "\n", encoding="utf-8")


def _stage_mod(staging, name):
    d = staging / name
    d.mkdir(exist_ok=True)
    (d / "meta.ini").write_text(
        "[General]\nmodid = 111\nfileid = 222\nversion = 1.0\n", encoding="utf-8")


def _make_view(qapp, tmp_path, names, request, *, profile_dir=None, staging=None):
    profile_dir = profile_dir or (tmp_path / "profile")
    staging = staging or (tmp_path / "staging")
    profile_dir.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    _write_modlist(profile_dir, names)
    for n in names:
        _stage_mod(staging, n)
    game = _FakeGame(staging, profile_dir)
    host = QMainWindow()
    window = type("W", (), {"_notify": lambda self, *a: None})()
    view = CreateCollectionView(window, game, None, log_fn=lambda *_a: None)
    host.setCentralWidget(view)
    request.addfinalizer(lambda: (host.close(), qapp.processEvents()))
    return host, view, profile_dir, staging


# ---- Rescan -----------------------------------------------------------

def test_rescan_adds_a_newly_enabled_mod_without_losing_existing_edits(
        qapp, tmp_path, request):
    _host, view, profile_dir, staging = _make_view(
        qapp, tmp_path, ["Mod A", "Mod B"], request)

    by_name = {r["name"]: r for r in view._all_rows}
    data_idx = view._all_rows.index(by_name["Mod A"])
    view._set_optional(data_idx, True)      # the "forgotten notes" stand-in

    _stage_mod(staging, "Mod C")
    _write_modlist(profile_dir, ["Mod A", "Mod B", "Mod C"])
    view._rescan_rows()

    by_name = {r["name"]: r for r in view._all_rows}
    assert set(by_name) == {"Mod A", "Mod B", "Mod C"}
    assert by_name["Mod A"]["optional"] is True        # edit survived
    assert by_name["Mod B"]["optional"] is False
    assert by_name["Mod C"]["optional"] is False        # freshly added


def test_rescan_removes_a_mod_no_longer_enabled(qapp, tmp_path, request):
    _host, view, profile_dir, _staging = _make_view(
        qapp, tmp_path, ["Mod A", "Mod B", "Mod C"], request)

    _write_modlist(profile_dir, ["Mod A", "Mod B"])   # C disabled/removed
    view._rescan_rows()

    assert {r["name"] for r in view._all_rows} == {"Mod A", "Mod B"}


def test_rescan_never_touches_variant_rows(qapp, tmp_path, request):
    _host, view, _profile_dir, _staging = _make_view(
        qapp, tmp_path, ["Mod A"], request)

    view._all_rows.append({
        "name": "Mod A (4K)", "mod_id": 1, "file_id": 99, "version": "4k",
        "optional": True, "has_fomod": False, "has_bain": False,
        "fomod_export": False, "versions_fetched": False, "size_bytes": 0,
        "root_folder": False, "enabled": True, "locked": False,
        "source": "nexus", "direct_url": "", "update_policy": "exact",
        "instructions": "", "is_variant": True,
    })

    view._rescan_rows()

    names = {r["name"] for r in view._all_rows}
    assert "Mod A (4K)" in names
    variant = next(r for r in view._all_rows if r["name"] == "Mod A (4K)")
    assert variant["is_variant"] is True


# ---- background draft autosave / restore -------------------------------

def test_set_optional_schedules_an_autosave_timer(qapp, tmp_path, request):
    _host, view, _profile_dir, _staging = _make_view(
        qapp, tmp_path, ["Mod A"], request)

    view._set_optional(0, True)

    assert view._autosave_timer is not None
    assert view._autosave_timer.isActive()


def test_save_draft_writes_rows_and_form_fields(qapp, tmp_path, request):
    _host, view, profile_dir, _staging = _make_view(
        qapp, tmp_path, ["Mod A"], request)
    view._name.setText("My Collection")
    view._instructions.setPlainText("Read this first.")
    view._set_optional(0, True)

    view._save_draft()

    draft_path = profile_dir / "collection_export_draft.json"
    assert draft_path.is_file()
    data = json.loads(draft_path.read_text(encoding="utf-8"))
    assert data["info"]["name"] == "My Collection"
    assert data["info"]["installInstructions"] == "Read this first."
    assert data["rows"][0]["name"] == "Mod A"
    assert data["rows"][0]["optional"] is True


def test_reopening_restores_the_draft_instead_of_rescanning(
        qapp, tmp_path, request):
    profile_dir = tmp_path / "profile"
    staging = tmp_path / "staging"
    _host1, view1, profile_dir, staging = _make_view(
        qapp, tmp_path, ["Mod A", "Mod B"], request,
        profile_dir=profile_dir, staging=staging)
    by_name = {r["name"]: r for r in view1._all_rows}
    view1._set_optional(view1._all_rows.index(by_name["Mod A"]), True)
    view1._name.setText("Resumed Collection")
    view1._save_draft()

    # A second view over the SAME profile — simulates closing and reopening
    # the tab. Must resume the saved state, not re-scan the modlist fresh.
    _host2, view2, _pd, _st = _make_view(
        qapp, tmp_path, ["Mod A", "Mod B"], request,
        profile_dir=profile_dir, staging=staging)

    assert view2._name.text() == "Resumed Collection"
    by_name2 = {r["name"]: r for r in view2._all_rows}
    assert by_name2["Mod A"]["optional"] is True
    assert by_name2["Mod B"]["optional"] is False


def test_no_prior_draft_falls_back_to_a_fresh_scan(qapp, tmp_path, request):
    _host, view, profile_dir, _staging = _make_view(
        qapp, tmp_path, ["Mod A"], request)

    assert not (profile_dir / "collection_export_draft.json").is_file()
    assert [r["name"] for r in view._all_rows] == ["Mod A"]

"""Test for CreateCollectionView reading Load Order Insights' "Never
Together" markings and defaulting those rows to Optional.

Requested: mods marked Never Together in Insights (e.g. the same UI mod
downloaded from both Nexus and mod.io -- genuinely mutually-exclusive
alternatives) should not require the curator to remember to mark them
Optional by hand every time they export a collection."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from Utils.mods.bg3_pak_index import write_rules
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


def _make_view(qapp, tmp_path, names, request, never_together=None):
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
    if never_together:
        write_rules(profile_dir, {"rules": [], "ignored": [],
                                  "never_together": never_together})
    game = _FakeGame(staging, profile_dir)
    host = QMainWindow()
    window = type("W", (), {"_notify": lambda self, *a: None})()
    view = CreateCollectionView(window, game, None, log_fn=lambda *_a: None)
    host.setCentralWidget(view)
    request.addfinalizer(lambda: (host.close(), qapp.processEvents()))
    return host, view


def test_never_together_mods_default_to_optional(qapp, tmp_path, request):
    _host, view = _make_view(
        qapp, tmp_path,
        ["Better Hotbar (Nexus)", "Better Hotbar (mod.io)", "Unrelated Mod"],
        request,
        never_together=[["Better Hotbar (Nexus)", "Better Hotbar (mod.io)"]])

    by_name = {r["name"]: r for r in view._all_rows}
    assert by_name["Better Hotbar (Nexus)"]["optional"] is True
    assert by_name["Better Hotbar (mod.io)"]["optional"] is True
    assert by_name["Unrelated Mod"]["optional"] is False


def test_no_never_together_state_leaves_everything_required(qapp, tmp_path, request):
    _host, view = _make_view(qapp, tmp_path, ["Mod A", "Mod B"], request)

    assert all(r["optional"] is False for r in view._all_rows)


def test_multiple_never_together_groups_all_apply(qapp, tmp_path, request):
    _host, view = _make_view(
        qapp, tmp_path,
        ["BCPP 16x9", "BCPP 16x10", "BCPP UW", "Something Else"],
        request,
        never_together=[["BCPP 16x9", "BCPP 16x10", "BCPP UW"]])

    by_name = {r["name"]: r for r in view._all_rows}
    assert by_name["BCPP 16x9"]["optional"] is True
    assert by_name["BCPP 16x10"]["optional"] is True
    assert by_name["BCPP UW"]["optional"] is True
    assert by_name["Something Else"]["optional"] is False

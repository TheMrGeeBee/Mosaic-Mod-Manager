"""The end-of-install audit used to be a log line only: the final toast said
"Collection installed - 2101/2109 (8 skipped)" and never named a mod. The audit
now produces a structured report (saved in the profile, shown in a dialog) that
names every mod that is not in the profile and why."""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from Utils.collections.collection_install import (
    INSTALL_REPORT_NAME, CollectionInstallControl, build_install_report, write_install_report)

MISSING = [
    ("Mod A", 11, 111, "download_failed", "Connection failed: boom"),
    ("Mod B", 22, 222, "deferred", ""),
    ("Mod C", 33, 333, "weird_status", ""),
    ("Mod D", None, 444, "install_failed", "  extraction error  "),
]


def test_report_names_every_failed_mod_with_a_reason():
    r = build_install_report(MISSING, total=2109, slug="gts", revision=117)
    assert (r["collection"], r["revision"], r["total"], r["failed_count"]) == ("gts", 117, 2109, 4)
    by_name = {f["name"]: f for f in r["failed"]}
    assert by_name["Mod A"]["reason"] == "Connection failed: boom"           # a recorded detail wins
    assert "FOMOD/BAIN" in by_name["Mod B"]["reason"]                        # plain-language status
    assert by_name["Mod C"]["reason"] == "weird_status"                       # unknown status still shown
    assert by_name["Mod D"]["reason"] == "extraction error" and by_name["Mod D"]["mod_id"] == 0
    assert by_name["Mod A"]["file_id"] == 111 and by_name["Mod A"]["status"] == "download_failed"


def test_report_is_written_atomically_into_the_profile(tmp_path):
    r = build_install_report(MISSING, total=10)
    path = write_install_report(tmp_path, r)
    assert path == tmp_path / INSTALL_REPORT_NAME
    assert json.loads(path.read_text())["failed_count"] == 4
    assert not list(tmp_path.glob("*.tmp"))


def test_a_clean_install_removes_a_stale_report(tmp_path):
    (tmp_path / INSTALL_REPORT_NAME).write_text("{}")
    assert write_install_report(tmp_path, build_install_report([], total=10)) is None
    assert not (tmp_path / INSTALL_REPORT_NAME).exists()


def test_missing_profile_dir_is_tolerated(tmp_path):
    assert write_install_report(tmp_path / "gone", build_install_report(MISSING, total=1)) is None
    assert write_install_report(None, build_install_report(MISSING, total=1)) is None


def test_control_carries_the_failed_list():
    c = CollectionInstallControl()
    assert c.failed_mods == []
    c.failed_mods = build_install_report(MISSING, total=4)["failed"]
    assert len(c.failed_mods) == 4


# ---- the app's dialog -----------------------------------------------------------

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from gui_qt.app import MainWindow  # noqa: E402
from gui_qt.overlays.confirm_overlay import ConfirmOverlay  # noqa: E402


class Harness(QMainWindow):
    pass


Harness._show_failed_mods_report = MainWindow._show_failed_mods_report


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_dialog_lists_the_mods_then_runs_the_follow_up(app):
    h = Harness()
    h.resize(900, 700)
    h.show()
    h._col_failed = build_install_report(MISSING, total=4)["failed"]
    after = []
    h._show_failed_mods_report(then=lambda: after.append(1))
    (ov,) = h.findChildren(ConfirmOverlay)
    from PySide6.QtWidgets import QLabel
    labels = " ".join(l.text() for l in ov.findChildren(QLabel))
    assert "Mod A" in labels and "Connection failed: boom" in labels and "Continue" in labels
    assert h._col_failed == [] and after == []
    ov._finish(True)
    assert after == [1]                                   # the off-site reminder follows the dialog


def test_no_failures_goes_straight_to_the_follow_up(app):
    h = Harness()
    h._col_failed = []
    after = []
    h._show_failed_mods_report(then=lambda: after.append(1))
    assert after == [1] and not h.findChildren(ConfirmOverlay)


def test_dialog_truncates_a_long_list_but_says_how_many_more(app):
    h = Harness()
    h.resize(900, 700)
    h.show()
    h._col_failed = build_install_report(
        [(f"Mod {i}", i, i, "download_failed", "x") for i in range(25)], total=25)["failed"]
    h._show_failed_mods_report()
    (ov,) = h.findChildren(ConfirmOverlay)
    from PySide6.QtWidgets import QLabel
    text = " ".join(l.text() for l in ov.findChildren(QLabel))
    assert "25 mod(s) did not install" in text and "…and 15 more" in text
    assert "Mod 9 " in text and "Mod 10 " not in text

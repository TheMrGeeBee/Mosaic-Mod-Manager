"""An unexpected error in the collection install worker must NOT be treated as a
cancel. A cancel deletes a freshly created profile (and, with "clear archives
after install" on, the game's whole download cache) — right for a user who backed
out, catastrophic for an error at mod 1,800 of 2,100 that the user never asked
for. The "failed" terminal state keeps both, so Install -> Continue can resume.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from gui_qt.app import MainWindow  # noqa: E402


class Overlay:
    def __init__(self):
        self.finished = None
        self.dismissed = False

    def finish(self, text):
        self.finished = text

    def dismiss(self):
        self.dismissed = True


class Harness(QMainWindow):
    def __init__(self):
        super().__init__()
        self._col_install_running = True
        self._col_install_control = None
        self._staged_finish_queue = []
        self._col_install_overlay = Overlay()
        self.notes: list[tuple] = []
        self.selected: list[str] = []
        self.refreshed = 0
        self.spawned_cleanup = False

    def _drain_pending_after_staged(self):
        pass

    def _refresh_open_collection_buttons(self):
        self.refreshed += 1

    def _select_installed_collection_profile(self, name, rescan_index=False):
        self.selected.append(name)

    def _notify(self, text, state="info", sticky=False):
        self.notes.append((text, state, sticky))

    def _dismiss_col_overlay(self):
        pass


Harness._on_col_finished = MainWindow._on_col_finished


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_failed_install_keeps_the_profile_and_says_how_to_resume(app, tmp_path, monkeypatch):
    import threading
    started = []
    real_thread = threading.Thread
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: started.append(k.get("name")) or real_thread(*a, **k))
    profile = tmp_path / "profiles" / "GTS"
    (profile / "mods" / "Some Mod").mkdir(parents=True)
    h = Harness()
    h._on_col_finished("failed", {"profile_dir": str(profile), "error": "disk on fire"})

    assert (profile / "mods" / "Some Mod").is_dir()             # nothing deleted
    assert started == []                                        # no cancel-cleanup worker was spawned
    assert h._col_install_running is False                      # the install lock is released
    assert h.selected == ["GTS"]                                # the kept profile is selected
    assert h.refreshed == 1
    (text, state, sticky), = h.notes
    assert "disk on fire" in text and "Nothing was deleted" in text and "Continue" in text
    assert state == "error" and sticky
    assert "progress was kept" in h._col_install_overlay.finished


def test_failed_install_tolerates_a_missing_profile_dir(app, tmp_path):
    h = Harness()
    h._on_col_finished("failed", {"profile_dir": str(tmp_path / "gone"), "error": ""})
    assert h.selected == [] and h._col_install_running is False
    assert "unexpected error" in h.notes[0][0]

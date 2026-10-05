"""Integration test for gui_qt.wabbajack.wabbajack_controller: the real
controller, import view, install overlay and manual-download overlay, with a
real install running on its worker thread. The worker's automatic download
fails its hash check, so it blocks on a manual-download request that the UI
thread answers -- exercising the signal/Event hand-off between the two
threads, not just each side alone.

Faked: the network (the downloader the dispatcher returns), the OS file
picker, profile creation, and the main window around the controller.
"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton

from test_wabbajack_install_pipeline import _build
from Utils.wabbajack.downloaders.http_source import WabbajackDownloadResult


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the UI")


def _button(root, label):
    return next(b for b in root.findChildren(QPushButton) if b.text() == label and b.isVisible())


class FakeGame:
    name = "Skyrim Special Edition"

    def __init__(self, staging):
        self.staging = staging

    def is_configured(self):
        return True

    def set_active_profile_dir(self, path):
        pass

    def load_paths(self):
        pass

    def get_effective_mod_staging_path(self):
        return self.staging

    def get_game_path(self):
        return "/games/Skyrim"


class FakeTabs:
    def __init__(self):
        self.opened = {}

    def has_key(self, key):
        return key in self.opened

    def close_tab(self, key):
        self.opened.pop(key, None)

    def open_tab(self, widget, title, key=None):
        self.opened[key] = (widget, title)


class FakeWindow(QMainWindow):
    def __init__(self, game):
        super().__init__()
        self.resize(1000, 700)
        self._gs = SimpleNamespace(game=game)
        self._tabs = FakeTabs()
        self.notices, self.logs, self.selected = [], [], []

    def _notify(self, text, kind):
        self.notices.append((kind, text))

    def _append_log(self, text):
        self.logs.append(text)

    def _ensure_nexus_api(self):
        return None

    def _select_installed_collection_profile(self, name, rescan_index=False):
        self.selected.append(name)


def test_import_and_install_with_manual_fallback(qapp, tmp_path, monkeypatch):
    from Utils.exe_launch import game_helpers
    from Utils.wabbajack import wabbajack_install
    from Utils.wabbajack.downloaders import loverslab_auth
    from Utils.wine_proton import portal_filechooser
    from gui_qt.wabbajack import wabbajack_controller as wc

    wj, _modlist, src = _build(tmp_path)
    staging = tmp_path / "staging" / "mods"
    profile_dir = tmp_path / "staging" / "profiles" / "Test_List"

    def create_profile(game_name, name, profile_specific_mods=False):
        assert name == "Test_List"
        profile_dir.mkdir(parents=True)
        return str(profile_dir)

    monkeypatch.setattr(game_helpers, "_create_profile", create_profile)
    monkeypatch.setattr(game_helpers, "_profiles_for_game", lambda name: [])
    monkeypatch.setattr("Utils.config_paths.get_download_cache_dir_for_game",
                        lambda name: tmp_path / "downloads")
    monkeypatch.setattr("Utils.ui_config.load_nexus_last_premium", lambda: False)
    monkeypatch.setattr(loverslab_auth, "load_session", lambda: {})
    monkeypatch.setattr(wabbajack_install, "_rebuild_index", lambda *a: None)

    def corrupt(state, dest, **kw):
        dest.write_bytes(b"truncated")
        return WabbajackDownloadResult(success=True, file_path=dest)

    monkeypatch.setattr(wabbajack_install, "resolve_downloader", lambda state: corrupt)
    # The OS picker answers on a worker thread, like the real portal does.
    monkeypatch.setattr(portal_filechooser, "pick_file",
                        lambda title, cb, filters=None: cb(src))

    win = FakeWindow(FakeGame(staging))
    win.show()
    controller = wc.WabbajackController(win)

    # Pick the file -> parsed on a worker -> the import view opens.
    controller._picked.emit(wj)
    _wait_until(qapp, lambda: "wabbajack_import" in win._tabs.opened)
    view, title = win._tabs.opened["wabbajack_import"]
    assert title == "Wabbajack: Test List"
    win.setCentralWidget(view)
    qapp.processEvents()

    # Install: the worker's download fails its hash check, so it blocks on
    # the manual-download overlay until "Choose file…" is clicked.
    QTest.mouseClick(_button(view, "Install as new profile"), Qt.LeftButton)
    _wait_until(qapp, lambda: any(b.text() == "Choose file…" and b.isVisible()
                                  for b in win.findChildren(QPushButton)))
    QTest.mouseClick(_button(win, "Choose file…"), Qt.LeftButton)

    _wait_until(qapp, lambda: win.selected == ["Test_List"])
    overlay = controller._overlay
    assert overlay is not None
    assert overlay._summary.isVisible()
    assert overlay._summary.text().startswith("Installed 2 mods into the profile 'Test_List'.")
    assert (staging / "ModA" / "ModA.esp").read_bytes() == b"ESP BYTES"
    assert (profile_dir / "plugins.txt").read_bytes() == b"*ModA.esp\n"

    QTest.mouseClick(_button(win, "Close"), Qt.LeftButton)
    qapp.processEvents()
    assert controller._overlay is None


def test_unreadable_file_is_reported_not_opened(qapp, tmp_path):
    from gui_qt.wabbajack import wabbajack_controller as wc
    bad = tmp_path / "broken.wabbajack"
    bad.write_bytes(b"not a zip")
    win = FakeWindow(FakeGame(tmp_path / "mods"))
    controller = wc.WabbajackController(win)
    controller._picked.emit(bad)
    _wait_until(qapp, lambda: any(k == "error" for k, _ in win.notices))
    assert "wabbajack_import" not in win._tabs.opened


def test_install_summary_wording():
    from gui_qt.wabbajack.wabbajack_controller import install_summary
    from Utils.wabbajack.wabbajack_install import WabbajackInstallReport
    text, ok = install_summary(WabbajackInstallReport(installed_mods=["A"]), "P")
    assert ok and text == "Installed 1 mod into the profile 'P'."
    text, ok = install_summary(WabbajackInstallReport(
        installed_mods=["A", "B"], failed_archives=[("x", "404")]), "P")
    assert not ok and "1 download(s) couldn't be completed" in text and "See the log" in text
    text, ok = install_summary(WabbajackInstallReport(cancelled=True), "P")
    assert not ok and text.startswith("Install cancelled.")

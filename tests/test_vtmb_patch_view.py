"""Offscreen test of wizards_qt.vtmb_patch_view: Check page state and the
Install step end to end with a fake innoextract."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from test_vtmb_patch import _fake_innoextract, _game_root


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _teardown(app, host):
    import shiboken6
    host.close()
    app.processEvents()
    if shiboken6.isValid(host):
        shiboken6.delete(host)
    app.processEvents()


def _wait_until(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the UI")


class FakeGame:
    name = "Vampire: The Masquerade - Bloodlines"

    def __init__(self, root):
        self.root = root

    def get_game_path(self):
        return self.root


def test_check_then_install(qapp, tmp_path, monkeypatch):
    from Utils.modding_tools import vtmb_patch as core
    from wizards_qt import vtmb_patch_view as vv

    root = _game_root(tmp_path)
    tool = _fake_innoextract(tmp_path)
    monkeypatch.setattr(core, "find_innoextract", lambda: tool)
    installer = tmp_path / "VTMBup120.6.exe"
    installer.write_text("x")

    host = QMainWindow()
    view = vv.VtmbPatchView(FakeGame(root))
    host.setCentralWidget(view)
    host.show()
    try:
        qapp.processEvents()
        text = view._check_label.text()
        assert "not installed yet" in text
        assert view._next_btn.isEnabled()

        view._archive_path = installer
        view._goto(vv._PG_APPLY)
        _wait_until(qapp, lambda: view._done_btn.isEnabled())
        assert (root / "Unofficial_Patch" / "python" / "fileutil.py").is_file()
        assert (root / "Vampire" / "pack102.vpk").read_text() == "vanilla"
        assert "is installed" in view._run_status.text()
    finally:
        _teardown(qapp, host)


def test_check_blocks_without_innoextract(qapp, tmp_path, monkeypatch):
    from Utils.modding_tools import vtmb_patch as core
    from wizards_qt import vtmb_patch_view as vv

    monkeypatch.setattr(core, "find_innoextract", lambda: None)
    host = QMainWindow()
    view = vv.VtmbPatchView(FakeGame(_game_root(tmp_path)))
    host.setCentralWidget(view)
    host.show()
    try:
        qapp.processEvents()
        assert "innoextract is not installed" in view._check_label.text()
        assert not view._next_btn.isEnabled()
    finally:
        _teardown(qapp, host)


def _check_text(qapp, tmp_path, monkeypatch, installed, installer_ver):
    from Utils.modding_tools import vtmb_patch as core
    from wizards_qt import vtmb_patch_view as vv
    from test_vtmb_patch import _UI, _write_version_file

    root = _game_root(tmp_path)
    if installed:
        _write_version_file(root / "Unofficial_Patch", _UI.replace("12.0", installed), "utf-16")
    monkeypatch.setattr(core, "find_innoextract", lambda: "/bin/true")
    inst = tmp_path / "VTMBup.exe"
    inst.write_text("x")
    monkeypatch.setattr(core, "find_installer", lambda dirs: inst if installer_ver else None)
    monkeypatch.setattr(core, "installer_version", lambda p, t=None: installer_ver)
    host = QMainWindow()
    view = vv.VtmbPatchView(FakeGame(root))
    host.setCentralWidget(view)
    host.show()
    try:
        qapp.processEvents()
        return view._check_label.text()
    finally:
        _teardown(qapp, host)


def test_check_page_reports_up_to_date(qapp, tmp_path, monkeypatch):
    text = _check_text(qapp, tmp_path, monkeypatch, "12.0", "12.0")
    assert "Unofficial Patch 12.0 is installed" in text
    assert "Up to date" in text


def test_check_page_reports_a_newer_patch(qapp, tmp_path, monkeypatch):
    text = _check_text(qapp, tmp_path, monkeypatch, "11.9", "12.0")
    assert "Unofficial Patch 11.9 is installed" in text
    assert "newer patch is available" in text


def test_check_page_without_an_installer_still_shows_the_version(qapp, tmp_path, monkeypatch):
    text = _check_text(qapp, tmp_path, monkeypatch, "12.0", None)
    assert "Unofficial Patch 12.0 is installed" in text
    assert "No installer downloaded yet" in text


def test_check_page_when_not_installed(qapp, tmp_path, monkeypatch):
    text = _check_text(qapp, tmp_path, monkeypatch, None, "12.0")
    assert "not installed yet" in text

"""Offscreen test of wizards_qt.game_version_view: the real wizard view on a
fake Steam library -- Check, Choose (the shipped Fallout 4 table), Download
(the watcher ticks a depot off from a real-format Steam log line), Switch,
and Switch back."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow

from Utils.steam import depot_versions as dv


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
    name = "Fallout 4"
    steam_id = "377160"
    exe_name = "Fallout4Launcher.exe"

    def __init__(self, root):
        self.root = root

    def get_game_path(self):
        return self.root

    def get_deploy_active(self):
        return False


def _library(tmp_path):
    lib = tmp_path / "lib" / "steamapps"
    game = lib / "common" / "Fallout 4"
    (game / "Data").mkdir(parents=True)
    (game / "Fallout4.exe").write_bytes(b"exe 1.11.240")
    (lib / "appmanifest_377160.acf").write_text('''"AppState"
{
	"appid"		"377160"
	"buildid"		"24564252"
	"InstalledDepots"
	{
		"377162"	{ "manifest"		"4737695048810094469" "size" "55343016" }
		"435881"	{ "manifest"		"1207717296920736193" "size" "2890058024" }
	}
}''')
    return game


def test_switch_and_switch_back_through_the_wizard(qapp, tmp_path, monkeypatch):
    from Utils import config_paths
    from Utils.exe_launch import exe_launch
    from wizards_qt import game_version_view as gv

    game_root = _library(tmp_path)
    state = tmp_path / "config" / "games" / "Fallout 4"
    log = tmp_path / "console_log.txt"
    log.write_text("")
    monkeypatch.setattr(config_paths, "get_game_config_dir", lambda name: state)
    monkeypatch.setattr(dv, "content_log_paths", lambda roots=None: [log])
    monkeypatch.setattr(exe_launch, "_wine_process_alive", lambda exe: False)
    # The fake exe has no PE header; skip the version check for this test.
    table = dv.load_table("377160")
    table.get("1.10.163").exe_version = ""
    monkeypatch.setattr(dv, "load_table", lambda app, user=None: table)

    host = QMainWindow()
    host.resize(1100, 800)
    view = gv.GameVersionView(FakeGame(game_root))
    host.setCentralWidget(view)
    host.show()
    try:
        qapp.processEvents()
        assert "Steam build 24564252" in view._check_label.text()
        assert view._check_next.isEnabled()
        QTest.mouseClick(view._check_next, Qt.LeftButton)

        labels = [view._versions.item(i).data(Qt.UserRole) for i in range(view._versions.count())]
        view._versions.setCurrentRow(labels.index("1.10.163"))
        assert view._plan.download == [("377162", "5847529232406005096")]
        assert view._plan.unchanged == ["435881"]
        QTest.mouseClick(view._choose_next, Qt.LeftButton)

        cmd = view._depot_vlay.itemAt(0).widget().findChildren(gv.QLineEdit)[0].text()
        assert cmd == "download_depot 377160 377162 5847529232406005096"
        assert not view._apply_btn.isEnabled()

        # Steam finishes the download: folder + the real log line format.
        depot = tmp_path / "Steam" / "steamrt64" / "steamapps" / "content" / "app_377160" / "depot_377162"
        depot.mkdir(parents=True)
        (depot / "Fallout4.exe").write_bytes(b"exe 1.10.163")
        log.write_text(f'[2026-10-08 03:07:57] Depot download complete : '
                       f'"{tmp_path}/Steam/steamrt64\\steamapps\\content\\app_377160\\depot_377162" '
                       f'(manifest 5847529232406005096)\n')
        view._poll_downloads()
        _wait_until(qapp, lambda: view._apply_btn.isEnabled())

        QTest.mouseClick(view._apply_btn, Qt.LeftButton)
        _wait_until(qapp, lambda: view._done_btn.isEnabled())
        assert "Switched to 1.10.163" in view._run_status.text()
        assert (game_root / "Fallout4.exe").read_bytes() == b"exe 1.10.163"

        view._goto(0)
        assert view._back_btn.isVisible() and not view._check_next.isVisible()
        view._done_btn.setEnabled(False)
        QTest.mouseClick(view._back_btn, Qt.LeftButton)
        _wait_until(qapp, lambda: view._done_btn.isEnabled())
        assert (game_root / "Fallout4.exe").read_bytes() == b"exe 1.11.240"
        assert dv.load_state(state) is None
    finally:
        _teardown(qapp, host)


def test_check_page_blocks_while_deployed(qapp, tmp_path, monkeypatch):
    from Utils import config_paths
    from Utils.exe_launch import exe_launch
    from wizards_qt import game_version_view as gv

    game_root = _library(tmp_path)
    (game_root / "Data_Core").mkdir()
    monkeypatch.setattr(config_paths, "get_game_config_dir", lambda name: tmp_path / "cfg")
    monkeypatch.setattr(exe_launch, "_wine_process_alive", lambda exe: False)
    host = QMainWindow()
    view = gv.GameVersionView(FakeGame(game_root))
    host.setCentralWidget(view)
    host.show()
    try:
        qapp.processEvents()
        assert "restore first" in view._check_label.text()
        assert view._restore_btn.isVisible() and not view._check_next.isEnabled()
    finally:
        _teardown(qapp, host)

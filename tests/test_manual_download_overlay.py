"""The manual-download overlay notices a file the user downloads in their
browser (into a watched download folder) and finishes by itself, ignoring a
browser's in-progress file. Download-folder scanning itself is in
test_wabbajack_manual_downloads.py."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from Utils.wabbajack import manual_downloads
from Utils.wabbajack.wabbajack_hash import hash_bytes
from Utils.wabbajack.wabbajack_manifest import Archive, ManualState


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


def test_overlay_finishes_when_the_download_appears(qapp, tmp_path, monkeypatch):
    from gui_qt.wabbajack.manual_download_overlay import ManualDownloadOverlay
    data = b"mod archive bytes" * 100
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(manual_downloads, "download_folders", lambda: [downloads])
    monkeypatch.setattr(ManualDownloadOverlay, "SCAN_MS", 50)
    archive = Archive(hash=hash_bytes(data), name="DD - I4 Patch v7.zip", size=len(data),
                      state=ManualState(url="https://www.loverslab.com/files/file/1/?do=download"))
    results = []
    host = QMainWindow()
    host.resize(900, 600)
    host.show()
    try:
        ov = ManualDownloadOverlay.show_over(host, archive, "", results.append)
        assert "Watching" in ov._watch.text()
        (downloads / "DD - I4 Patch v7.zip.part").write_bytes(data)   # still downloading
        for _ in range(10):
            qapp.processEvents()
            time.sleep(0.02)
        assert results == []
        (downloads / "DD - I4 Patch v7.zip").write_bytes(data)
        _wait_until(qapp, lambda: results == [downloads / "DD - I4 Patch v7.zip"])
    finally:
        _teardown(qapp, host)


def test_page_url_drops_a_note_after_the_link():
    from gui_qt.wabbajack.manual_download_overlay import archive_page_url
    a = Archive(state=ManualState(
        url="http://www.mediafire.com/file/kfac38dni6d53rp/MiscHairstyle1.6_by_Atherisz.7z/file"
            " # MiscHairstyles1.6"))
    assert archive_page_url(a) == ("http://www.mediafire.com/file/kfac38dni6d53rp/"
                                   "MiscHairstyle1.6_by_Atherisz.7z/file")

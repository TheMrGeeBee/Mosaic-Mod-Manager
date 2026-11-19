"""A resumed collection install that still has real downloads in flight must not
claim "nothing to download" (the finished-mod counts alone can't tell)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from gui_qt.collections.collection_install_overlay import CollectionInstallOverlay  # noqa: E402


@pytest.fixture
def overlay():
    app = QApplication.instance() or QApplication([])
    host = QMainWindow()
    host.resize(900, 700)
    ov = CollectionInstallOverlay(host, "Test")
    yield ov
    ov.deleteLater()
    host.deleteLater()
    app.processEvents()


def test_all_cached_state_when_nothing_is_downloading(overlay):
    overlay.set_cache_progress(11, 11)
    assert overlay._all_cached
    assert "nothing to download" in overlay._agg_lbl.text()


def test_download_in_flight_blocks_the_cached_state(overlay):
    overlay.dl_start(1, "Tamriel Data (HD)", 1000)
    overlay.set_cache_progress(11, 11)
    assert not overlay._all_cached
    assert not overlay._dl_frame.isHidden()
    assert "nothing to download" not in overlay._agg_lbl.text()


def test_download_starting_after_cached_state_reverts_it(overlay):
    overlay.set_cache_progress(11, 11)
    assert overlay._all_cached and overlay._dl_frame.isHidden()
    overlay.dl_start(1, "Tamriel Data (HD)", 1000)
    assert not overlay._all_cached
    assert not overlay._dl_frame.isHidden()
    overlay.set_agg(10, 100, 0.9)
    assert "%" in overlay._agg_lbl.text()

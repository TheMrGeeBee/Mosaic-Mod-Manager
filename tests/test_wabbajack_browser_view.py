"""Tests for gui_qt.wabbajack.wabbajack_browser_view (Jackify-style browser):
the pure filter/sort helpers, and the real view offscreen, fed from a
pre-written gallery cache (no network) -- including a card click opening the
details window and Install Modlist fetching the file and handing it on."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow

from test_wabbajack_gallery import _entry
from Utils.wabbajack import wabbajack_gallery as g
from gui_qt.wabbajack import wabbajack_browser_view as bv


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _teardown(app, host):
    """Destroy this test's window now -- with its children, including an
    overlay whose deleteLater is still queued -- so the teardown can't land in
    a later test's processEvents (see the Qt test host-window cleanup note).
    Only this window: flushing every queued deletion would also hit other
    test files' leftovers."""
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


def _entries():
    return [g.parse_entry(d) for d in (
        _entry("Alpha", dateUpdated="2026-01-01", tags=["Lore", "Combat"], official=True),
        _entry("Beta", dateUpdated="2026-09-01", tags=["Lore"],
               download_metadata={"SizeOfArchives": 10}),
        _entry("Adult One", nsfw=True),
        _entry("Gone", force_down=True),
        _entry("Other Game", game="fallout4"),
    )]


SSE = "Skyrim Special Edition"


def test_filter_entries():
    def titles(**kw):
        return [e.title for e in bv.filter_entries(_entries(), game_name=SSE, **kw)]
    assert titles() == ["Alpha", "Beta"]
    assert "Adult One" in titles(show_adult=True)
    assert "Gone" in titles(hide_unavailable=False)
    assert titles(official_only=True) == ["Alpha"]
    assert titles(tags={"lore", "Combat"}) == ["Alpha"]
    assert titles(query="alp") == ["Alpha"]
    assert [e.title for e in bv.filter_entries(_entries(), game_name="Fallout 4")] == ["Other Game"]


def test_sort_entries():
    es = bv.filter_entries(_entries(), game_name=SSE)
    assert [e.title for e in bv.sort_entries(es, "updated")] == ["Beta", "Alpha"]
    assert [e.title for e in bv.sort_entries(es, "smallest")] == ["Beta", "Alpha"]
    assert [e.title for e in bv.sort_entries(es, "largest")] == ["Alpha", "Beta"]
    assert [e.title for e in bv.sort_entries(es, "name")] == ["Alpha", "Beta"]


def _view(qapp, tmp_path, monkeypatch, game_name=SSE):
    monkeypatch.setattr(bv.WabbajackBrowserView, "_load_show_adult", staticmethod(lambda: False))
    monkeypatch.setattr(bv.WabbajackBrowserView, "_save_show_adult", staticmethod(lambda on: None))
    monkeypatch.setattr(bv.ThumbnailLoader, "request", lambda self, i, url: None)
    cache = tmp_path / "gallery.json"
    cache.write_text(json.dumps({"fetched_at": time.time(), "entries": [
        _entry("Alpha", tags=["Lore"], official=True), _entry("Beta", tags=["Combat"]),
        _entry("Other Game", game="fallout4")]}))
    opened = []
    host = QMainWindow()
    host.resize(1400, 900)
    view = bv.WabbajackBrowserView(
        SimpleNamespace(name=game_name), cache_path=cache, files_dir=tmp_path / "lists",
        on_file_ready=opened.append, notify=lambda *a: None)
    host.setCentralWidget(view)
    host.show()
    return host, view, opened


def test_view_lists_filters_and_follows_a_game_switch(qapp, tmp_path, monkeypatch):
    host, view, _ = _view(qapp, tmp_path, monkeypatch)
    try:
        _wait_until(qapp, lambda: len(view._cards) == 2)
        assert sorted(c.entry.title for c in view._cards) == ["Alpha", "Beta"]
        assert [view._tags.item(i).text() for i in range(view._tags.count())] == ["Combat", "Lore"]
        view._tags.item(1).setSelected(True)                      # Lore
        assert [c.entry.title for c in view._cards] == ["Alpha"]
        view._clear_filters()
        view._official_cb.setChecked(True)
        assert [c.entry.title for c in view._cards] == ["Alpha"]
        view._clear_filters()
        view.set_game(SimpleNamespace(name="Fallout 4"))
        assert [c.entry.title for c in view._cards] == ["Other Game"]
        view.set_game(SimpleNamespace(name="Cyberpunk 2077"))
        assert view._cards == [] and "no modlists" in view._empty.text()
    finally:
        _teardown(qapp, host)


def test_card_click_opens_details_and_install_fetches_and_hands_on(qapp, tmp_path, monkeypatch):
    fetched = tmp_path / "Alpha.wabbajack"
    fetched.write_bytes(b"x")

    def fake_fetch(entry, folder, progress_cb=None, cancel=None):
        progress_cb(50, 100)
        return fetched
    monkeypatch.setattr(g, "fetch_modlist_file", fake_fetch)
    host, view, opened = _view(qapp, tmp_path, monkeypatch)
    try:
        _wait_until(qapp, lambda: len(view._cards) == 2)
        card = next(c for c in view._cards if c.entry.title == "Alpha")
        QTest.mouseClick(card, Qt.LeftButton)
        _wait_until(qapp, lambda: view._details is not None)
        details = view._details
        assert details.entry.title == "Alpha" and details._install.isEnabled()
        QTest.mouseClick(details._install, Qt.LeftButton)
        _wait_until(qapp, lambda: opened == [fetched])
        assert view._details is None
    finally:
        _teardown(qapp, host)


def test_close_during_download_cancels_it(qapp, tmp_path, monkeypatch):
    started = threading.Event()

    def slow_fetch(entry, folder, progress_cb=None, cancel=None):
        started.set()
        cancel.wait(5)
        raise g.GalleryError("cancelled")
    monkeypatch.setattr(g, "fetch_modlist_file", slow_fetch)
    host, view, opened = _view(qapp, tmp_path, monkeypatch)
    try:
        _wait_until(qapp, lambda: len(view._cards) == 2)
        view._open_details(view._cards[0].entry)
        details = view._details
        QTest.mouseClick(details._install, Qt.LeftButton)
        _wait_until(qapp, started.is_set)
        assert details._close.text() == "Cancel"
        QTest.mouseClick(details._close, Qt.LeftButton)
        _wait_until(qapp, lambda: details._install.isEnabled())
        assert "cancelled" in details._status.text().lower() and opened == []
        details._finish(None)
    finally:
        _teardown(qapp, host)

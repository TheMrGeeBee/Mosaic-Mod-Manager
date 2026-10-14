"""Offscreen test of MissingReqsView's multi-select "Install selected" bar."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from gui_qt.views.missing_reqs_view import MissingReqsView


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class FakeApi:
    def get_mod_requirements(self, domain, mod_id):
        return [SimpleNamespace(mod_id=i, mod_name=f"Req {i}", notes="",
                                url="", is_external=False, game_domain="1")
                for i in (11, 12, 13)]


def test_install_selected(qapp):
    got = []
    view = MissingReqsView(
        FakeApi(), SimpleNamespace(nexus_game_domain="cyberpunk2077"),
        [{"mod_name": "Owner", "mod_id": 5, "domain": "cyberpunk2077",
          "missing_ids": {11, 12, 13}}],
        set(), lambda s: None, lambda: None,
        install_many_fn=got.append)
    end = time.monotonic() + 5
    while len(view._cards) < 3 and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(view._cards) == 3
    assert not view._install_sel_btn.isEnabled()

    view._toggle_select_all()
    assert view._install_sel_btn.isEnabled()
    assert "3" in view._install_sel_btn.text()

    view._cards[12].select_cb.setChecked(False)
    view._install_selected()
    assert sorted(m for m, _d, _n in got[0]) == [11, 13]
    assert got[0][0][1] == "cyberpunk2077"
    assert not view._selected_reqs()
    view.deleteLater()
    qapp.processEvents()


def test_stop_queue_button(qapp):
    stopped = []
    view = MissingReqsView(
        FakeApi(), SimpleNamespace(nexus_game_domain="cyberpunk2077"),
        [{"mod_name": "Owner", "mod_id": 5, "domain": "cyberpunk2077",
          "missing_ids": {11}}],
        set(), lambda s: None, lambda: None,
        install_many_fn=lambda _i: None, stop_queue_fn=lambda: stopped.append(1))
    assert view._stop_queue_btn.isHidden()
    view.set_queue_status(True, 3)
    assert not view._stop_queue_btn.isHidden()
    assert "3" in view._stop_queue_btn.text()
    view._stop_queue_btn.click()
    assert stopped == [1]
    view.set_queue_status(False)
    assert view._stop_queue_btn.isHidden()
    view.deleteLater()
    qapp.processEvents()


def test_watcher_quiet_hint(monkeypatch, tmp_path):
    from Nexus import manual_download_watch as m
    monkeypatch.setattr(m, "_QUIET_HINT_S", 0.0)
    monkeypatch.setattr(m, "scan_download_dirs", lambda: [tmp_path])
    f = SimpleNamespace(file_name="x.zip", name="x", file_id=1,
                        size_in_bytes=100, size_kb=0)
    seen = []
    w = m.ManualDownloadWatcher(mod_id=1, files=[f], on_found=lambda *a: None,
                                on_quiet=seen.append, poll_s=0.01)
    w.start()
    end = time.monotonic() + 3
    while not seen and time.monotonic() < end:
        time.sleep(0.01)
    w.stop()
    assert seen == [[tmp_path]]


def test_move_into_cache(tmp_path):
    from Nexus.manual_download_watch import move_into_cache
    dl, cache = tmp_path / "dl", tmp_path / "cache" / "Game"
    dl.mkdir()
    a = dl / "a.zip"
    a.write_bytes(b"1234")
    new = move_into_cache(a, cache)
    assert new == cache / "a.zip" and new.exists() and not a.exists()
    assert move_into_cache(new, cache) == new            # already there
    b = dl / "a.zip"
    b.write_bytes(b"1234")                               # same archive again
    assert move_into_cache(b, cache) == new and not b.exists()
    c = dl / "a.zip"
    c.write_bytes(b"different-size")                     # name clash, new content
    assert move_into_cache(c, cache) == c and c.exists()
    assert move_into_cache(c, None) == c

"""Nexus browser "Download selected": which files a batch pre-ticks, what the
review overlay hands back, and how the browser view sequences the downloads.

The file-picking rule is the part that can silently install the wrong thing: a
mod with several MAIN files usually ships variants (SE/AE, 1K/2K, male/female),
so only an unambiguous single main file may be ticked for the user."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from Nexus.nexus_api import NexusModFile  # noqa: E402
from gui_qt.nexus.nexus_batch_chooser import NexusBatchChooser, plan_mod_files  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def f(fid, cat, ts=0, name=""):
    return NexusModFile(file_id=fid, name=name or f"file{fid}", version="1.0",
                        category_name=cat, file_name=f"file{fid}.7z",
                        uploaded_timestamp=ts)


def entry(mod_id, name=""):
    return SimpleNamespace(
        mod_id=mod_id, name=name or f"Mod {mod_id}", domain_name="skyrimspecialedition",
        picture_url="", author="", category_name="", updated_at="", created_at="",
        summary="", endorsement_count=0, downloads_total=0, file_size_kb=0,
        contains_adult_content=False, uploaded_by="", uploader_id=0, version="")


# -- plan_mod_files -----------------------------------------------------------

def test_single_main_is_preticked_and_optionals_are_offered_unticked():
    offered, ticked = plan_mod_files([f(2, "OPTIONAL"), f(1, "MAIN"), f(3, "MISCELLANEOUS")])
    assert [x.file_id for x in offered] == [1, 2, 3]          # main first
    assert ticked == {1}


def test_several_main_files_are_never_preticked():
    offered, ticked = plan_mod_files([f(1, "MAIN", ts=1), f(2, "MAIN", ts=2), f(3, "OPTIONAL")])
    assert [x.file_id for x in offered] == [2, 1, 3]          # newest main first
    assert ticked == set()


def test_old_versions_and_updates_are_not_offered():
    offered, ticked = plan_mod_files([f(1, "MAIN"), f(2, "OLD_VERSION"), f(3, "UPDATE")])
    assert [x.file_id for x in offered] == [1]
    assert ticked == {1}


def test_a_lone_non_main_file_is_preticked_but_two_are_not():
    assert plan_mod_files([f(5, "OPTIONAL")])[1] == {5}
    assert plan_mod_files([f(5, "OPTIONAL"), f(6, "MISCELLANEOUS")])[1] == set()


def test_no_files():
    assert plan_mod_files([]) == ([], set())
    assert plan_mod_files(None) == ([], set())


# -- the review overlay -------------------------------------------------------

def _children(chooser, i):
    return chooser._rows[i][2]


def test_chooser_plan_defaults_and_skips(app):
    host = QWidget()
    host.resize(1000, 800)
    got = []
    mods = [
        (entry(1), [f(10, "MAIN"), f(11, "OPTIONAL")]),       # auto main
        (entry(2), [f(20, "MAIN"), f(21, "MAIN")]),           # needs a choice
        (entry(3), None),                                     # list fetch failed
    ]
    c = NexusBatchChooser(host, mods, got.append)
    plan = c.plan()
    assert [(e.mod_id, [x.file_id for x in fs]) for e, fs in plan] == [(1, [10])]
    assert c._go.isEnabled()
    assert "skipped" in c._summary.text()
    # the ambiguous mod is opened so its choice is visible; the easy one isn't
    assert c._rows[1][1].isExpanded() and not c._rows[0][1].isExpanded()

    # tick the optional of mod 1 and the second main of mod 2
    _children(c, 0)[1].setCheckState(0, Qt.Checked)
    _children(c, 1)[1].setCheckState(0, Qt.Checked)
    c._finish(c.plan())
    assert len(got) == 1
    assert [(e.mod_id, [x.file_id for x in fs]) for e, fs in got[0]] == [
        (1, [10, 11]), (2, [21])]


def test_chooser_download_disabled_with_nothing_ticked_and_escape_cancels(app):
    host = QWidget()
    host.resize(1000, 800)
    got = []
    c = NexusBatchChooser(host, [(entry(2), [f(20, "MAIN"), f(21, "MAIN")])], got.append)
    assert not c._go.isEnabled()
    c._finish(None)
    assert got == [None]


# -- the browser view ---------------------------------------------------------

class FakeApi:
    def get_game_categories(self, domain):
        return []

    def get_top_mods(self, *a, **k):
        return []


@pytest.fixture
def view(app):
    from gui_qt.nexus.nexus_browser_view import NexusBrowserView
    installs = []
    v = NexusBrowserView(FakeApi(), "skyrimspecialedition", None,
                         install_fn=lambda paths, metas=None: installs.append((paths, metas)))
    v._installs = installs
    yield v
    v.deleteLater()


def test_selection_survives_a_card_rebuild_and_clears_on_game_switch(view):
    e1, e2 = entry(1), entry(2)
    view._entries = [e1, e2]
    view._rebuild_cards()
    view._cards[0]._select_cb.setChecked(True)
    assert list(view._selected) == [1]
    assert view._sel_bar.isVisibleTo(view)
    view._rebuild_cards()                                     # e.g. next page and back
    assert view._cards[0]._select_cb.isChecked()
    assert not view._cards[1]._select_cb.isChecked()
    view.set_game(None, "fallout4")
    assert view._selected == {}
    assert not view._sel_bar.isVisibleTo(view)


def test_premium_batch_installs_a_mods_files_together_in_chooser_order(view, monkeypatch):
    keys = iter(["k1", "k2", "k3"])
    monkeypatch.setattr(view, "_start_download",
                        lambda e, file, sem=None: next(keys))
    monkeypatch.setattr(view, "_progress_fn", lambda *a: None)
    e1, e2 = entry(1), entry(2)
    view._start_batch_downloads([(e1, [f(10, "MAIN"), f(11, "OPTIONAL")]),
                                 (e2, [f(20, "MAIN")])])
    # mod 2's single file installs as soon as it lands
    view._on_download_done("/dl/c.7z", None, "k3")
    assert view._installs == [(["/dl/c.7z"], None)]
    # mod 1's optional lands before its main: nothing installs yet...
    view._on_download_done("/dl/b.7z", "meta-b", "k2")
    assert len(view._installs) == 1
    # ...then both go together, main first
    view._on_download_done("/dl/a.7z", "meta-a", "k1")
    assert view._installs[1] == (["/dl/a.7z", "/dl/b.7z"],
                                 {"/dl/a.7z": "meta-a", "/dl/b.7z": "meta-b"})
    assert view._batch_groups == {}


def test_premium_batch_group_with_a_failed_file_installs_the_rest(view, monkeypatch):
    keys = iter(["k1", "k2"])
    monkeypatch.setattr(view, "_start_download", lambda e, file, sem=None: next(keys))
    monkeypatch.setattr(view, "_progress_fn", lambda *a: None)
    view._start_batch_downloads([(entry(1), [f(10, "MAIN"), f(11, "OPTIONAL")])])
    view._on_download_done(None, None, "k1")                  # main failed
    view._on_download_done("/dl/b.7z", None, "k2")
    assert view._installs == [(["/dl/b.7z"], None)]


class FakeWatcher:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def _fake_open(view, opened):
    """Stand-in for _open_manual_file: records the file, registers a watch
    under a fresh key (as the real one does) and returns that key."""
    def _open(e, file):
        view.cancel_manual_watch(e.mod_id)
        key = f"man-{len(opened)}"
        opened.append((e.mod_id, file.file_id, key))
        view._manual_watchers[e.mod_id] = (FakeWatcher(), key)
        return key
    return _open


def test_manual_batch_opens_one_file_at_a_time(view, monkeypatch):
    opened = []
    monkeypatch.setattr(view, "_open_manual_file", _fake_open(view, opened))
    monkeypatch.setattr(view, "_progress_fn", lambda *a: None)
    plan = [(entry(1), [f(10, "MAIN"), f(11, "OPTIONAL")]), (entry(2), [f(20, "MAIN")])]
    view._start_manual_batch(plan)
    assert [o[:2] for o in opened] == [(1, 10)]
    assert "file 1 of 3" in view._sel_label.text()

    # first file arrives (the watcher pops itself before signalling)
    view._manual_watchers.pop(1)
    view._on_manual_watch_ended(1, "man-0")
    assert [o[:2] for o in opened] == [(1, 10), (1, 11)]      # same mod, next file

    # the user cancels the second from the card → moves on, doesn't stall
    view.cancel_manual_watch(1)
    assert [o[:2] for o in opened] == [(1, 10), (1, 11), (2, 20)]

    # a late/stale end for an older key must not skip ahead
    view._on_manual_watch_ended(1, "man-0")
    assert len(opened) == 3

    view._manual_watchers.pop(2)
    view._on_manual_watch_ended(2, "man-2")
    assert view._manual_batch_key == "" and view._manual_batch_total == 0
    assert not view._sel_bar.isVisibleTo(view)


def test_stopping_a_manual_batch_keeps_the_current_watch(view, monkeypatch):
    opened = []
    monkeypatch.setattr(view, "_open_manual_file", _fake_open(view, opened))
    monkeypatch.setattr(view, "_progress_fn", lambda *a: None)
    view._start_manual_batch([(entry(1), [f(10, "MAIN")]), (entry(2), [f(20, "MAIN")])])
    watcher, _key = view._manual_watchers[1]
    view._on_selection_action()                               # "Stop after this file"
    assert view._manual_batch == [] and view._manual_batch_key == ""
    assert not watcher.stopped                                # current download still watched
    view._manual_watchers.pop(1)
    view._on_manual_watch_ended(1, "man-0")
    assert len(opened) == 1                                   # nothing else opened

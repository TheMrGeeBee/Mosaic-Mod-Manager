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


def test_menu_keeps_missing_reqs_for_whole_mod_ignore(tmp_path, monkeypatch):
    """Ticking "Ignore requirements" clears the ⚠ flag; the right-click entry
    must stay so the panel can still be opened (un-ignore / install)."""
    from gui_qt.modlist import modlist_menu as mm
    from gui_qt.modlist.modlist_data import FLAG_MISSING_REQS

    meta = SimpleNamespace(missing_requirements="4197:TweakXL",
                           ignored_requirements="")
    monkeypatch.setattr(mm, "_read_mod_meta", lambda v, n: meta)
    view = SimpleNamespace(
        model=lambda: SimpleNamespace(_flags={}),
        ignored_missing_reqs=frozenset())
    assert not mm._has_missing_reqs(view, "Owner")            # nothing flagged
    view.ignored_missing_reqs = frozenset({"Owner"})
    assert mm._has_missing_reqs(view, "Owner")                # ignored mod-wide
    assert not mm._has_missing_reqs(view, "Other")
    meta.missing_requirements = ""
    assert not mm._has_missing_reqs(view, "Owner")            # nothing to show
    view.model = lambda: SimpleNamespace(_flags={"Owner": FLAG_MISSING_REQS})
    assert mm._has_missing_reqs(view, "Owner")                # flagged as before
    # a mod with ONLY off-site requirements: no flag, but the panel is reachable
    view.model = lambda: SimpleNamespace(_flags={})
    view.ignored_missing_reqs = frozenset()
    meta.external_requirements = '[{"name": "Store pack", "url": "", "notes": ""}]'
    assert mm._has_missing_reqs(view, "Owner")


def test_external_requirements_listed_with_link(qapp):
    """Off-site requirements show as cards with a link: not selectable, no
    Install button, and a mod with ONLY off-site requirements still opens."""
    ext = [{"name": "Virtual Atelier store pack", "url": "https://example.org/pack",
            "notes": "soft requirement"},
           {"name": "No link", "url": "", "notes": ""}]
    view = MissingReqsView(
        FakeApi(), SimpleNamespace(nexus_game_domain="cyberpunk2077"),
        [{"mod_name": "Store", "mod_id": 5, "domain": "cyberpunk2077",
          "missing_ids": set(), "external": ext}],
        set(), lambda s: None, lambda: None, install_many_fn=lambda _i: None)
    end = time.monotonic() + 5
    while len(view._ext_cards) < 2 and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(view._ext_cards) == 2 and not view._cards
    assert not view._selectable()                    # never part of Install selected
    first = view._ext_cards[0]
    assert first.select_cb is None
    from PySide6.QtWidgets import QLabel
    assert any("Off-site" in l.text() for l in first.findChildren(QLabel))
    from PySide6.QtWidgets import QPushButton
    labels = {b.text(): b for b in first.findChildren(QPushButton)}
    assert "Install" not in labels and labels["View"].isEnabled()
    nolink = {b.text(): b for b in view._ext_cards[1].findChildren(QPushButton)}
    assert not nolink["View"].isEnabled()
    view.deleteLater()
    qapp.processEvents()


def test_external_requirements_meta_roundtrip():
    from Nexus.nexus_meta import format_external_reqs, parse_external_reqs
    R = SimpleNamespace
    reqs = [R(is_external=False, mod_name="Nexus mod", url="", notes=""),
            R(is_external=True, mod_name="Store; pack", url="https://x/y%20z", notes="n" * 900),
            R(is_external=True, mod_name="store; PACK", url="https://x/y%20z", notes="dup"),
            R(is_external=True, mod_name="", url="", notes="empty")]
    raw = format_external_reqs(reqs)
    got = parse_external_reqs(raw)
    assert len(got) == 1 and got[0]["name"] == "Store; pack"
    assert got[0]["url"] == "https://x/y%20z" and len(got[0]["notes"]) == 500
    assert format_external_reqs([reqs[0]]) == ""
    assert parse_external_reqs("not json") == [] and parse_external_reqs("") == []


def test_external_requirements_survive_meta_ini(tmp_path):
    from Nexus.nexus_meta import (NexusModMeta, read_meta, write_meta,
                                  format_external_reqs, parse_external_reqs)
    R = SimpleNamespace
    m = NexusModMeta(mod_id=1)
    m.external_requirements = format_external_reqs(
        [R(is_external=True, mod_name="A", url="https://x/a%20b", notes="line1\nline2")])
    p = tmp_path / "mod" / "meta.ini"
    write_meta(p, m)
    got = parse_external_reqs(read_meta(p).external_requirements)
    assert got == [{"name": "A", "url": "https://x/a%20b", "notes": "line1\nline2"}]


def test_off_site_entry_that_is_a_nexus_mod_becomes_installable(qapp):
    """The Virtual Atelier case: off-site entries whose link is a Nexus mod of
    this game get Install + a checkbox, but stay un-ignorable and unflagged;
    other-game and non-Nexus links stay informational; a link naming a real
    (flagged) requirement doesn't duplicate its card."""
    from PySide6.QtWidgets import QPushButton
    ext = [
        {"name": "Linked mod", "url": "https://www.nexusmods.com/cyberpunk2077/mods/27772", "notes": "n"},
        {"name": "Twin of real", "url": "https://www.nexusmods.com/cyberpunk2077/mods/11", "notes": ""},
        {"name": "Other game", "url": "https://www.nexusmods.com/skyrimspecialedition/mods/5", "notes": ""},
        {"name": "Elsewhere", "url": "https://example.org/x", "notes": ""},
    ]
    ignored = []
    view = MissingReqsView(
        FakeApi(), SimpleNamespace(nexus_game_domain="cyberpunk2077"),
        [{"mod_name": "Store", "mod_id": 5, "domain": "cyberpunk2077",
          "missing_ids": {11}, "external": ext}],
        set(), lambda s: None, lambda: None,
        install_many_fn=lambda _i: None,
        ignore_req_fn=lambda *a: ignored.append(a))
    end = time.monotonic() + 5
    while len(view._cards) + len(view._ext_cards) < 3 and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert sorted(view._cards) == [11, 27772]          # real + linked, no twin
    assert len(view._ext_cards) == 2                   # other game + elsewhere
    linked = view._cards[27772]
    from PySide6.QtWidgets import QLabel
    assert any("Soft requirement" in l.text() for l in linked.findChildren(QLabel))
    assert not any("Soft requirement" in l.text()
                   for l in view._cards[11].findChildren(QLabel))
    names = {b.text() for b in linked.findChildren(QPushButton)}
    assert "Install" in names and linked.select_cb is not None
    from PySide6.QtWidgets import QCheckBox
    boxes = {c.text() for c in linked.findChildren(QCheckBox)}
    assert "Ignore" not in boxes                       # soft: not ignorable
    real = {c.text() for c in view._cards[11].findChildren(QCheckBox)}
    assert "Ignore" in real
    # Select all covers the real requirement only, not the soft linked mod
    view._toggle_select_all()
    assert view._cards[11].select_cb.isChecked()
    assert not linked.select_cb.isChecked()
    assert view._sel_all_btn.text() == "Select none"
    linked.select_cb.setChecked(True)                  # individual tick still works
    assert len(view._selected_reqs()) == 2
    view._toggle_select_all()                          # Select none clears everything
    assert not view._selected_reqs()
    assert view._sel_all_btn.text() == "Select all"
    view.deleteLater()
    qapp.processEvents()


def test_nexus_mod_from_url():
    from Nexus.nexus_meta import nexus_mod_from_url
    assert nexus_mod_from_url("https://www.nexusmods.com/cyberpunk2077/mods/27772") == ("cyberpunk2077", 27772)
    assert nexus_mod_from_url("http://nexusmods.com/Cyberpunk2077/mods/5?tab=files") == ("cyberpunk2077", 5)
    assert nexus_mod_from_url("https://example.org/mods/5") is None
    assert nexus_mod_from_url("") is None

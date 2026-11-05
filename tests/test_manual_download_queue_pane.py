"""The manual-download queue pane, driven through a real Qt event loop.

``test_manual_download_queue.py`` covers ``pages_to_open`` on its own; this
file covers the parts that only exist once the widget is real: the pane
mounted inside the preflight view, its folder watch running on worker threads,
and the auto-open tab budget.

Three behaviours here are regressions that unit tests cannot see:

* **out-of-order pickup** -- the 40th of 40 archives landing first is picked up
  immediately. This is the whole reason ``MultiDownloadFinder`` exists, and it
  is invisible on the old one-at-a-time install path.
* **the pane surviving ``set_checks()``** -- it is mounted inside a check row,
  and the view's rebuild detaches and ``deleteLater``s every row. Without
  lifting the pane clear first, its live folder watch dies on every re-check.
* **no tabs on first paint** -- a remembered "open automatically" must not
  throw browser tabs at the user the moment the preflight screen appears.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from Utils.wabbajack.wabbajack_hash import hash_file  # noqa: E402
from Utils.wabbajack.wabbajack_manifest import ManualState, parse_modlist  # noqa: E402
from Utils.wabbajack.wabbajack_preflight import (  # noqa: E402
    FIX_MANUAL_DOWNLOADS,
    run_preflight,
)
from gui_qt.wabbajack.manual_download_queue import ManualDownloadQueue  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Keep the auto-open settings out of the user's real mosaic.ini."""
    from Utils import ui_config
    ini = tmp_path / "mosaic.ini"
    monkeypatch.setattr(ui_config, "get_ui_config_path", lambda: ini)
    return ini


@pytest.fixture
def opened(monkeypatch):
    """Collect the URLs the pane would have opened in a browser."""
    import Utils.xdg
    urls: list[str] = []
    monkeypatch.setattr(Utils.xdg, "open_url", urls.append)
    return urls


def _watch(monkeypatch, *folders: Path):
    """Point the pane's folder watch at *folders* instead of ~/Downloads.

    The pane imports ``download_folders`` inside ``__init__``, so patching the
    module attribute is enough; the autouse conftest fixture has already
    pointed it at nothing.
    """
    from Utils.wabbajack import manual_downloads
    monkeypatch.setattr(manual_downloads, "download_folders",
                        lambda: [Path(f) for f in folders])


def _manual(*specs):
    """A parsed modlist's ``ManualState`` archives from ``(hash, name, size)``."""
    archives = [{"Hash": h, "Name": n, "Size": s,
                 "State": {"$type": "ManualDownloader, Wabbajack.Lib",
                           "Url": f"https://example.invalid/{n}",
                           "Prompt": "Grab it from the page"}}
                for h, n, s in specs]
    modlist = parse_modlist({"GameType": "SkyrimSpecialEdition",
                             "Directives": [], "Archives": archives})
    return modlist, [a for a in modlist.archives if type(a.state) is ManualState]


def _wait(cond, timeout: float = 15.0) -> bool:
    """Spin the event loop until *cond* holds -- the scan runs on a thread."""
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return bool(cond())


# ---------------------------------------------------------------------------
# mounted in the preflight view
# ---------------------------------------------------------------------------

def test_the_manual_check_offers_the_queue(tmp_path):
    """The check is what mounts the pane, so it has to carry the fix id."""
    modlist, manual = _manual(("aaa", "One.7z", 100), ("bbb", "Two.7z", 200))
    assert len(manual) == 2
    checks = run_preflight(modlist, active_game_name="Skyrim Special Edition",
                           staging_root=None, cache_dir=None, nexus_premium=True,
                           loverslab_logged_in=True, manual_resolved=0)
    manual_check = [c for c in checks if c.key == "manual"][0]
    assert manual_check.fix == FIX_MANUAL_DOWNLOADS
    assert not manual_check.ok
    assert not manual_check.blocking        # a warning, never a hard stop


def test_the_pane_survives_a_set_checks_rebuild(app, isolated_settings,
                                                tmp_path, monkeypatch):
    """The controller re-runs ``set_checks()`` on *every* resolution, and that
    detaches and ``deleteLater``s each check row. The pane lives inside one, so
    its live folder watch has to come through the rebuild intact.

    Asserted through behaviour, not object identity: a destroyed C++ widget can
    still be the same Python object, and a surviving-but-stopped pane would
    satisfy an identity check while picking nothing up.
    """
    import shiboken6
    from gui_qt.wabbajack.wabbajack_import_view import WabbajackImportView

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    _watch(monkeypatch, downloads)

    # Staged outside the watched folder so the pane's first scan finds nothing;
    # it is moved in only after the rebuild.
    staged = tmp_path / "One.7z"
    staged.write_bytes(b"n" * 100)
    real_hash = hash_file(staged)

    modlist, _archives = _manual((real_hash, "One.7z", 100), ("bbb", "Two.7z", 200))
    checks = run_preflight(modlist, active_game_name="Skyrim Special Edition",
                           staging_root=None, cache_dir=None, nexus_premium=True,
                           loverslab_logged_in=True, manual_resolved=0)
    resolved: dict[str, Path] = {}
    view = WabbajackImportView(modlist, checks, on_install=lambda *_: None,
                               on_login=lambda *_: None,
                               on_manual=lambda h, p: resolved.update({h: p}))
    try:
        pane = view._manual_pane
        assert isinstance(pane, ManualDownloadQueue)

        view.set_checks(checks)
        QApplication.processEvents()

        assert view._manual_pane is pane, "the pane was rebuilt instead of reused"
        assert shiboken6.isValid(pane), "set_checks() destroyed the pane"

        # The watch still works: a file arriving after the rebuild is reported.
        assert resolved == {}
        staged.rename(downloads / "One.7z")
        assert _wait(lambda: real_hash in resolved), \
            f"the folder watch did not survive the set_checks() rebuild: {resolved}"
    finally:
        view.stop_manual_queue()
        view.deleteLater()
        QApplication.processEvents()


def test_the_pane_survives_a_rebuild_that_drops_the_manual_row(
        app, isolated_settings, tmp_path, monkeypatch):
    """An ordinary re-check happens to save the pane by re-adopting it into the
    new manual row before the deferred delete runs. A rebuild whose checks have
    no manual row does not, and the pane would go down with the old row --
    which is what lifting it clear in ``set_checks()`` is actually for.
    """
    import shiboken6
    from PySide6.QtCore import QEvent
    from gui_qt.wabbajack.wabbajack_import_view import WabbajackImportView

    def row_holding(view, widget):
        box = view._checks_box
        for i in range(box.count()):
            row = box.itemAt(i).widget()
            if row is not None and row.isAncestorOf(widget):
                return row
        return None

    _watch(monkeypatch, tmp_path)
    modlist, _archives = _manual(("aaa", "One.7z", 100), ("bbb", "Two.7z", 200))
    checks = run_preflight(modlist, active_game_name="Skyrim Special Edition",
                           staging_root=None, cache_dir=None, nexus_premium=True,
                           loverslab_logged_in=True, manual_resolved=0)
    view = WabbajackImportView(modlist, checks, on_install=lambda *_: None,
                               on_login=lambda *_: None, on_manual=lambda *_: None)
    try:
        pane = view._manual_pane
        assert isinstance(pane, ManualDownloadQueue)
        old_row = row_holding(view, pane)
        assert old_row is not None, "the pane is not mounted inside a check row"

        view.set_checks([c for c in checks if c.key != "manual"])
        # Run the row's own deferred delete, and only that one: flushing every
        # pending DeferredDelete would reach widgets other Qt tests still hold
        # in this shared QApplication, and abort the interpreter.
        QApplication.sendPostedEvents(old_row, QEvent.DeferredDelete)

        assert shiboken6.isValid(pane), \
            "the pane was destroyed with the check row that held it"
    finally:
        view.stop_manual_queue()
        view.deleteLater()
        QApplication.processEvents()


# ---------------------------------------------------------------------------
# the folder watch
# ---------------------------------------------------------------------------

def test_a_file_in_a_watched_folder_is_picked_up(app, isolated_settings,
                                                 tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    payload = downloads / "BLOODLINES_EXTREME_2.1.8.zip"
    payload.write_bytes(b"x" * 4096)
    real_hash = hash_file(payload)

    _watch(monkeypatch, downloads)
    _modlist, manual = _manual((real_hash, payload.name, payload.stat().st_size),
                               ("zzz", "Other.7z", 999),
                               ("yyy", "Third.7z", 1234))

    resolved: dict[str, Path] = {}
    pane = ManualDownloadQueue(manual, on_resolved=lambda h, p: resolved.update({h: p}))
    try:
        # The pane kicks off a scan immediately, so a file downloaded before
        # the screen opened is not held up for a whole interval.
        assert _wait(lambda: real_hash in resolved), f"never found the file: {resolved}"
        assert resolved[real_hash] == payload
        assert len(pane.pending()) == 2
        assert pane.resolved() == {real_hash: payload}
    finally:
        pane.stop()


def test_the_fortieth_of_forty_is_picked_up_out_of_order(app, isolated_settings,
                                                         tmp_path, monkeypatch):
    """The regression this pane exists for: on the serial install path, a file
    at the back of the queue stays invisible until its turn comes round."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    late = downloads / "Fortieth.7z"
    late.write_bytes(b"q" * 8192)
    late_hash = hash_file(late)

    _watch(monkeypatch, downloads)
    specs = [(f"h{i}", f"Mod{i}.7z", 100 + i) for i in range(39)]
    specs.append((late_hash, late.name, late.stat().st_size))
    _modlist, manual = _manual(*specs)
    assert len(manual) == 40

    resolved: dict[str, Path] = {}
    pane = ManualDownloadQueue(manual, on_resolved=lambda h, p: resolved.update({h: p}))
    try:
        assert _wait(lambda: late_hash in resolved), \
            f"the 40th archive was not picked up out of order: {resolved}"
        assert len(pane.pending()) == 39
        # One of the forty is done, so the header counts the user forward even
        # though the file that arrived was the last in the list.
        assert pane._head.text() == "Manual download 2 of 40"
        assert pane.pending()[0].name == "Mod0.7z"   # still the next to fetch
    finally:
        pane.stop()


# ---------------------------------------------------------------------------
# the auto-open tab budget
# ---------------------------------------------------------------------------

def test_auto_open_holds_to_the_tab_budget(app, isolated_settings, opened,
                                           tmp_path, monkeypatch):
    _watch(monkeypatch)        # no folders: nothing resolves behind our back
    _modlist, manual = _manual(*[(f"h{i}", f"Mod{i}.7z", 100 + i) for i in range(10)])

    pane = ManualDownloadQueue(manual)
    try:
        pane._auto.setChecked(True)
        pane._tabs.setValue(3)
        assert len(opened) == 3, opened

        # Every slot is still held, so a second pass opens nothing more.
        pane._maybe_auto_open()
        assert len(opened) == 3, f"reopened pages that were already open: {opened}"

        # Skipping frees exactly one slot, so exactly one more page opens.
        pane._skip_current()
        assert len(opened) == 4, opened
        assert len(pane.pending()) == 9
    finally:
        pane.stop()


def test_no_tabs_open_on_first_paint(app, isolated_settings, opened,
                                     tmp_path, monkeypatch):
    """A remembered auto-open setting is restored, but must not act until the
    user does: four tabs appearing the instant a modlist loads is not a
    download queue, it is an ambush."""
    from Utils.ui_config import (load_wabbajack_manual_settings,
                                 save_wabbajack_manual_settings)
    save_wabbajack_manual_settings(True, 4)
    assert load_wabbajack_manual_settings() == {"auto_open": True, "tabs_at_once": 4}

    _watch(monkeypatch)
    _modlist, manual = _manual(*[(f"h{i}", f"M{i}.7z", 10 + i) for i in range(6)])

    pane = ManualDownloadQueue(manual)
    try:
        QApplication.processEvents()
        assert pane._auto.isChecked() and pane._tabs.value() == 4, \
            "the remembered settings were not restored"
        assert opened == [], f"tabs opened before the user asked for any: {opened}"

        pane._skip_current()                   # the first thing the user does
        assert len(opened) == 4, opened
    finally:
        pane.stop()


def test_manual_settings_default_to_the_old_behaviour(isolated_settings):
    """No stored settings means one page at a time and nothing opened for you
    -- identical to how the manual path behaved before the queue existed."""
    from Utils.ui_config import load_wabbajack_manual_settings
    assert not isolated_settings.exists()
    assert load_wabbajack_manual_settings() == {"auto_open": False, "tabs_at_once": 1}

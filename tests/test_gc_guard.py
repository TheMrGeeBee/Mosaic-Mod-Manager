"""The GUI-thread GC guard (gui_qt/gc_guard.py).

PySide6 6.12 on Python 3.14 segfaults when the cyclic GC runs on a worker thread
(a stale entry in Shiboken's referredObjects reaches the GC's visit callback). The
guard keeps collections on the GUI thread.
"""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_qt import gc_guard  # noqa: E402

SCRIPT = Path(__file__).with_name("gc_guard_race_script.py")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def guard(app):
    was = gc.isenabled()
    g = gc_guard.install(app)
    yield g
    g.uninstall()
    g.deleteLater()
    (gc.enable if was else gc.disable)()


def test_install_turns_automatic_gc_off_and_uninstall_restores_it(app):
    gc.enable()
    g = gc_guard.install(app)
    try:
        assert not gc.isenabled()
        assert g._timer.isActive()
    finally:
        g.uninstall()
        g.deleteLater()
    assert gc.isenabled() and not g._timer.isActive()


def test_uninstall_leaves_gc_off_if_it_was_off_before(app):
    gc.disable()
    try:
        g = gc_guard.install(app)
        g.uninstall()
        g.deleteLater()
        assert not gc.isenabled()
    finally:
        gc.enable()


def test_a_tick_is_a_young_pass_and_every_nth_is_a_full_pass(app, monkeypatch):
    calls = []
    monkeypatch.setattr(gc, "collect", lambda gen=2: calls.append(gen))
    was = gc.isenabled()
    g = gc_guard.GuiThreadGC(app, full_every=3)
    try:
        for _ in range(6):
            g.collect()
    finally:
        g.uninstall()
        g.deleteLater()
        (gc.enable if was else gc.disable)()
    assert calls == [1, 1, 2, 1, 1, 2]


def test_the_timer_collects_on_the_gui_thread_only(app, guard, monkeypatch):
    threads = []
    monkeypatch.setattr(gc, "collect", lambda gen=2: threads.append(threading.get_ident()))
    guard._timer.setInterval(5)
    end = time.time() + 2
    while len(threads) < 3 and time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    assert len(threads) >= 3
    assert set(threads) == {threading.get_ident()}


def test_garbage_cycles_are_still_collected(app, guard):
    class Cyc:
        def __init__(self):
            self.me = self
    ref = weakref.ref(Cyc())
    assert ref() is not None                     # a cycle: refcounting alone cannot free it
    guard.collect()
    for _ in range(guard._full_every):
        guard.collect()                          # ensure a full pass happens
    assert ref() is None


def _race(mode: str, rounds: int) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), mode, str(rounds)],
                          capture_output=True, text=True, timeout=240)


def test_closing_a_bain_picker_while_the_worker_starts_a_pool_does_not_crash():
    """The crash seen in a collection install, replayed: segfaults within ~20
    rounds without the guard; with it, every round completes."""
    r = _race("guard", 60)
    assert r.returncode == 0, f"exit {r.returncode}\n{r.stderr[-1500:]}"
    assert "rounds=60" in r.stdout

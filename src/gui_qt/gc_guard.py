"""Run Python's cyclic garbage collector on the GUI thread only.

Why: PySide6 6.12 on Python 3.14 can crash with a segmentation fault *inside the
garbage collector* when it runs on a worker thread. Shiboken's traverse step walks
the table of objects a wrapper keeps alive (``referredObjects``) and hands the GC
a pointer that is no longer valid; the fault is in the GC's visit callback.
The same crash (same offset in libshiboken6) hit a collection install three times in an
hour, always on the ``col-install`` thread straight after a BAIN picker tab was
closed, and earlier on other worker threads (conflict builds, Wabbajack parsing).

A cyclic collection starts on whichever thread happens to allocate when the
counters trip, so with worker threads busy it often lands on one of them. Qt
widget wrappers must only ever be finalised on the GUI thread anyway. So the
automatic collector is switched off and a timer on the GUI thread runs it instead.
Reference counting still frees everything that is not in a cycle immediately;
cycles just wait up to a fraction of a second.

Reproduced headlessly (tests/gc_guard_race_script.py): the real ``BainPickerView``
closed while a worker thread starts a thread pool segfaults within ~20 rounds
without this guard and ran 450 rounds clean with it.
"""

from __future__ import annotations

import gc

from PySide6.QtCore import QObject, QTimer

# A young-generation pass is ~free; a full pass cost ~12 ms on a freshly loaded
# window, so it runs every few seconds rather than on every tick.
YOUNG_INTERVAL_MS = 200
FULL_EVERY_TICKS = 25            # 25 * 200 ms = a full collection every ~5 s


class GuiThreadGC(QObject):
    """Owns the collection timer. Create it on the GUI thread, parented to the app."""

    def __init__(self, parent: "QObject | None" = None,
                 interval_ms: int = YOUNG_INTERVAL_MS,
                 full_every: int = FULL_EVERY_TICKS):
        super().__init__(parent)
        self._full_every = max(1, int(full_every))
        self._ticks = 0
        self._was_enabled = gc.isenabled()
        gc.disable()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.collect)
        self._timer.start()

    def collect(self) -> None:
        """One timer tick: a young pass, or a full pass every ``full_every`` ticks."""
        self._ticks += 1
        if self._ticks % self._full_every == 0:
            gc.collect()
        else:
            gc.collect(1)

    def uninstall(self) -> None:
        """Stop the timer and give the interpreter its automatic collector back."""
        self._timer.stop()
        if self._was_enabled:
            gc.enable()


def install(parent: "QObject | None" = None) -> GuiThreadGC:
    """Turn automatic GC off and start collecting from the GUI thread's event loop."""
    return GuiThreadGC(parent)

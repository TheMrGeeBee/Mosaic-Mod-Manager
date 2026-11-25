"""How many downloaded archives the Wabbajack build works on at once.

Settings > Extraction > "Install workers":

* ``auto`` (default) -- found while the build runs. Every few seconds the
  controller looks at how fast hash-checked files are actually finishing (MB/s)
  and adds a worker while that keeps improving, steps back when one more made
  it slower, and tries again now and then, as the mix of archives changes. A
  hard drive or a slow filesystem stops improving early, a fast SSD keeps
  going: the measurement covers the disk, the filesystem and the CPU at once.
* ``light`` -- the same, but at most a quarter of the CPU cores, with the
  extractors at low priority: for using the computer while it installs.
* ``1`` .. ``N`` -- exactly that many.

Whatever the mode, two limits win over speed: when free memory runs low, or
the install's drive is nearly full, workers are taken away (down to one) until
it recovers. The current count and the reason are reported, so the overlay
can show them.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

# Measuring window, and how long a found best is kept before probing again.
WINDOW_S = 8.0
HOLD_WINDOWS = 6
# A worker must raise throughput by this much to be kept.
GAIN = 1.05
# Memory and disk floors.
MIN_FREE_RAM_FRACTION = 0.15
MIN_FREE_RAM_BYTES = 2 << 30
MIN_FREE_DISK_BYTES = 5 << 30
MAX_WORKERS = 16


def cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 2


def parse_mode(value: "str | int | None") -> "tuple[str, int]":
    """``(kind, n)`` from the saved setting: ("auto", 0), ("light", 0) or
    ("fixed", n). Anything unreadable is auto."""
    v = str(value if value is not None else "auto").strip().lower()
    if v in ("auto", "light"):
        return v, 0
    try:
        n = int(v)
    except ValueError:
        return "auto", 0
    return ("fixed", max(1, min(MAX_WORKERS, n))) if n > 0 else ("auto", 0)


class WorkerController:
    """The worker limit for one build. Workers report finished bytes through
    :meth:`record`; the scheduler calls :meth:`tick` about once a second and
    starts archives while it has fewer running than :attr:`limit`."""

    def __init__(self, mode: "str | int | None" = "auto", *, disk_paths=(),
                 cpus: "int | None" = None, memory=None, disk_free=None,
                 clock=time.monotonic):
        self.kind, fixed = parse_mode(mode)
        cpus = cpus or cpu_count()
        if self.kind == "fixed":
            self.max_limit = fixed
        elif self.kind == "light":
            self.max_limit = max(1, cpus // 4)
        else:
            self.max_limit = max(1, min(MAX_WORKERS, cpus))
        self.low_priority = self.kind == "light"
        self.limit = fixed if self.kind == "fixed" else min(2, self.max_limit)
        self.reason = "set" if self.kind == "fixed" else "starting"
        self._disk_paths = [Path(p) for p in disk_paths if p]
        self._memory = memory or _memory
        self._disk_free = disk_free or _disk_free
        self._clock = clock
        self._lock = threading.Lock()
        self._bytes = 0
        self._window_start = clock()
        self._best_limit = self.limit
        self._best_tp = 0.0
        self._hold = 0
        self._guarded = False

    def record(self, nbytes: int) -> None:
        """A file of *nbytes* was built and hash-checked."""
        with self._lock:
            self._bytes += max(0, int(nbytes))

    def tick(self) -> bool:
        """Re-evaluate at the end of each window. True if the limit changed."""
        now = self._clock()
        if now - self._window_start < WINDOW_S:
            return False
        with self._lock:
            done, self._bytes = self._bytes, 0
        tp = done / (now - self._window_start)
        self._window_start = now
        before = self.limit

        # Hard limits first: memory, then disk space.
        avail, total = self._memory()
        if avail < max(MIN_FREE_RAM_BYTES, MIN_FREE_RAM_FRACTION * total):
            self.limit, self.reason, self._guarded = max(1, self.limit - 1), "memory", True
            return self.limit != before
        if any(self._disk_free(p) < MIN_FREE_DISK_BYTES for p in self._disk_paths):
            self.limit, self.reason, self._guarded = 1, "disk space", True
            return self.limit != before
        if self._guarded:                   # recovered: start measuring afresh
            self._guarded = False
            self._best_tp, self._hold = 0.0, 0
            if self.kind == "fixed":
                self.limit, self.reason = self.max_limit, "set"
                return self.limit != before

        if self.kind == "fixed":
            return False
        if self._hold > 0:                  # keeping the best found for a while
            self._hold -= 1
            if self._hold == 0 and self.limit < self.max_limit:
                self._best_tp = tp          # probe one more against today's speed
                self._best_limit = self.limit
                self.limit, self.reason = self.limit + 1, "trying one more"
            return self.limit != before
        if tp > self._best_tp * GAIN:       # this many beat the best so far
            self._best_tp, self._best_limit = tp, self.limit
            if self.limit < self.max_limit:
                self.limit, self.reason = self.limit + 1, "faster with more"
            else:
                self.reason = "at the maximum"
                self._hold = HOLD_WINDOWS
        else:                               # no better: go back, keep it a while
            self.limit = self._best_limit
            self.reason = "fastest found"
            self._hold = HOLD_WINDOWS
        return self.limit != before


def _memory() -> "tuple[int, int]":
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.available, vm.total
    except Exception:
        return 1 << 62, 1 << 62


def _disk_free(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        st = os.statvfs(p)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 1 << 62

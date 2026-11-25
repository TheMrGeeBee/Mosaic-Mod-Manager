"""The Wabbajack build's worker count: fixed, light, or found while it runs.

Measured on 120 real LoreRim archives (2026-11-25): Auto went 2 -> 5, found
5 no faster than 4, settled at 4, and built the sample 2.5x faster than one
worker (1,235 vs 486 files/s)."""
from __future__ import annotations

import threading

from Utils.ui_config import load_extraction_settings, save_install_workers
from Utils.wabbajack import wabbajack_install as wi
from Utils.wabbajack.wabbajack_workers import WINDOW_S, WorkerController, parse_mode

GB = 1 << 30
PLENTY = (64 * GB, 64 * GB)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _ctl(mode="auto", cpus=16, memory=PLENTY, disk=lambda p: 500 * GB):
    clock = Clock()
    c = WorkerController(mode, cpus=cpus, disk_paths=["/staging"],
                         memory=lambda: memory, disk_free=disk, clock=clock)
    return c, clock


def _window(c, clock, mb_per_s):
    c.record(int(mb_per_s * (1 << 20) * WINDOW_S))
    clock.t += WINDOW_S
    c.tick()


def test_modes_from_the_setting():
    assert parse_mode("auto") == ("auto", 0)
    assert parse_mode("LIGHT") == ("light", 0)
    assert parse_mode("6") == ("fixed", 6)
    assert parse_mode("0") == ("auto", 0)
    assert parse_mode("nonsense") == ("auto", 0)
    assert parse_mode(None) == ("auto", 0)


def test_auto_adds_workers_while_it_helps_then_keeps_the_fastest():
    c, clock = _ctl()
    assert c.limit == 2
    speed = {2: 100, 3: 150, 4: 190, 5: 185}          # 5 is no faster than 4
    for _ in range(4):
        _window(c, clock, speed[c.limit])
    assert c.limit == 4 and c.reason == "fastest found"


def test_auto_tries_again_after_holding():
    c, clock = _ctl()
    speed = {2: 100, 3: 150, 4: 190, 5: 185, 6: 300}  # the archive mix changed
    for _ in range(4):
        _window(c, clock, speed[c.limit])
    for _ in range(6):                                 # hold the best a while
        _window(c, clock, speed[c.limit])
    assert c.limit == 5 and c.reason == "trying one more"
    _window(c, clock, 260)                             # 5 is better now
    assert c.limit == 6


def test_a_slow_disk_stays_low():
    c, clock = _ctl()
    for _ in range(3):
        _window(c, clock, {2: 80, 3: 60}[c.limit])     # more workers fight over the disk
    assert c.limit == 2


def test_low_memory_takes_workers_away_whatever_the_mode():
    c, clock = _ctl("6", memory=(GB, 32 * GB))
    assert c.limit == 6
    _window(c, clock, 100)
    _window(c, clock, 100)
    assert c.limit == 4 and c.reason == "memory"


def test_a_nearly_full_drive_drops_to_one_worker():
    c, clock = _ctl(disk=lambda p: GB)
    _window(c, clock, 100)
    assert c.limit == 1 and c.reason == "disk space"


def test_light_uses_at_most_a_quarter_of_the_cores_at_low_priority():
    c, _ = _ctl("light", cpus=16)
    assert c.max_limit == 4 and c.low_priority
    assert _ctl("light", cpus=2)[0].max_limit == 1


def test_fixed_stays_put():
    c, clock = _ctl("3")
    for mb in (100, 300, 50):
        _window(c, clock, mb)
    assert c.limit == 3


def test_batches_run_in_parallel_up_to_the_limit():
    c, _ = _ctl("3")
    running, peak, lock = [0], [0], threading.Lock()
    release = threading.Event()
    done = []

    def work(root, batch):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(2)
        with lock:
            running[0] -= 1
            done.append(root)

    class CB:
        def on_workers(self, n, r):
            pass

    threading.Timer(0.3, release.set).start()
    batches = [(f"a{i}", [i]) for i in range(7)]
    wi._run_batches(batches, work, c, type("I", (), {"concurrency": 1})(),
                    threading.Event(), CB(), lambda m: None)
    assert sorted(done) == sorted(r for r, _ in batches)
    assert peak[0] == 3


def test_cancel_stops_new_batches():
    c, _ = _ctl("1")
    cancel = threading.Event()
    done = []

    def work(root, batch):
        done.append(root)
        cancel.set()

    class CB:
        def on_workers(self, n, r):
            pass

    wi._run_batches([("a", [1]), ("b", [2]), ("c", [3])], work, c,
                    type("I", (), {"concurrency": 1})(), cancel, CB(), lambda m: None)
    assert done == ["a"]


def test_the_setting_is_saved_and_read(tmp_path, monkeypatch):
    import Utils.ui_config as uc
    monkeypatch.setattr(uc, "get_ui_config_path", lambda: tmp_path / "ui.ini")
    assert load_extraction_settings()["install_workers"] == "auto"
    save_install_workers("light")
    assert load_extraction_settings()["install_workers"] == "light"
    save_install_workers("4")
    assert load_extraction_settings()["install_workers"] == "4"

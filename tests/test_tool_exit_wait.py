"""After a wizard tool's launcher exits, Mosaic waits for the real .exe to be gone
before running its cleanup (wineserver -k, modlist refresh). That wait was a fixed
~20 s even in the normal case, where the user simply closed the tool: the wizard
sat on its "Done" page for 19 s after BethINI had closed, and the cleanup then hit
whatever the user had started in the meantime (Play, on 2026-09-26).

A launcher that detaches from a still-open tool does so within seconds; one that
ran longer ended because the tool was closed."""
from __future__ import annotations

from Utils.exe_launch.exe_launch import await_real_tool_exit


class Sim:
    """A fake world: `alive` answers per call from a script; sleep advances a clock."""

    def __init__(self, alive_script):
        self.script = list(alive_script)
        self.calls = 0
        self.slept = 0.0
        self.logs: list[str] = []

    def alive(self, _name):
        self.calls += 1
        return self.script.pop(0) if self.script else False

    def sleep(self, s):
        self.slept += s

    def run(self, ran_for: float, **kw):
        await_real_tool_exit("Bethini.exe", "BethINI Pie", self.logs.append, launched_at=0.0,
                             alive_fn=self.alive, sleep_fn=self.sleep, clock=lambda: ran_for, **kw)
        return self


def test_a_tool_the_user_closed_after_a_while_returns_immediately():
    """The regression: launcher ran 30 s, tool is gone -> no 20 s wait."""
    sim = Sim([False]).run(ran_for=30.0)
    assert sim.slept == 0.0 and sim.calls == 1 and not sim.logs


def test_a_detached_tool_that_appears_late_is_still_waited_for():
    """Launcher exited 2 s after start (detached): the real exe shows up on the 6th
    poll and stays for a while, then closes. Cleanup must wait for it."""
    sim = Sim([False] * 5 + [True] * 4 + [False]).run(ran_for=2.0)
    assert any("still running" in m for m in sim.logs)
    assert sim.calls == 10 and sim.slept > 1.0


def test_a_quick_exit_with_no_tool_gives_up_after_the_grace_period():
    sim = Sim([]).run(ran_for=1.0)                       # never appears
    assert sim.calls == 80 and abs(sim.slept - 79 * 0.25) < 1e-9 and not sim.logs


def test_a_long_lived_launcher_whose_tool_is_somehow_still_open_is_waited_for():
    sim = Sim([True, True, False]).run(ran_for=600.0)
    assert any("still running" in m for m in sim.logs) and sim.calls == 3


def test_the_detach_window_boundary():
    fast = Sim([]).run(ran_for=14.9)
    slow = Sim([]).run(ran_for=15.0)
    assert fast.calls == 80 and slow.calls == 1

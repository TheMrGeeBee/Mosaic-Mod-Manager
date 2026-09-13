#!/usr/bin/env python3
"""REPL driver for Mosaic Mod Manager (PySide6/Qt desktop app).

Constructs the real QApplication + MainWindow directly (bypassing
gui_qt.app.run()'s blocking app.exec() and single-instance lock, since
run() isn't designed to hand control back to a caller), pumps the Qt
event loop between commands, and exposes stdin commands to poke it:
launch, ss (screenshot), find, click, type, key, sleep, eval, quit.

Run from src/ with the project venv, under QT_QPA_PLATFORM=offscreen
(no xvfb needed - this is a native Qt app, not Electron/Chromium) and
an ISOLATED XDG_CONFIG_HOME so it never touches a real user's Mosaic
profiles/mods:

    cd src
    XDG_CONFIG_HOME=/tmp/mosaic-driver-config \
        ../.venv/bin/python3 ../.claude/skills/run-mosaic/driver.py

Designed for agents: wrap in tmux, send-keys commands, capture-pane
output. See SKILL.md in this directory for the full recipe.
"""
from __future__ import annotations

import os
import sys
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# This file lives at <repo>/.claude/skills/run-mosaic/driver.py; the
# importable app package is at <repo>/src.
_SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "..", "..", "src"))
sys.path.insert(0, _SRC)

SHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/tmp/mosaic-shots")
os.makedirs(SHOT_DIR, exist_ok=True)

state: dict = {"app": None, "win": None}


def _pump(iterations: int = 15, delay: float = 0.02) -> None:
    app = state["app"]
    if app is None:
        return
    for _ in range(iterations):
        app.processEvents()
        time.sleep(delay)


def cmd_launch(_arg: str = "") -> None:
    if state["app"] is not None:
        print("already launched")
        return
    import app_bootstrap
    app_bootstrap.setup_environment()
    from PySide6.QtWidgets import QApplication
    from gui_qt.app import MainWindow
    app = QApplication.instance() or QApplication([])
    win = MainWindow(app)
    win.show()
    state["app"] = app
    state["win"] = win
    _pump()
    print(f"launched. size={win.size().width()}x{win.size().height()}")


def cmd_ss(name: str = "") -> None:
    win = state["win"]
    if win is None:
        print("ERROR: launch first")
        return
    name = name.strip() or f"ss-{int(time.time())}"
    path = os.path.join(SHOT_DIR, name + ".png")
    win.grab().save(path)
    print("screenshot:", path)


def _iter_clickables():
    from PySide6.QtWidgets import QAbstractButton
    return state["win"].findChildren(QAbstractButton)


def cmd_find(text: str = "") -> None:
    win = state["win"]
    if win is None:
        print("ERROR: launch first")
        return
    from PySide6.QtWidgets import QWidget
    needle = text.strip().lower()
    hits = []
    for w in win.findChildren(QWidget):
        label = ""
        if hasattr(w, "text"):
            try:
                label = w.text() or ""
            except Exception:
                label = ""
        if label and needle in label.lower():
            hits.append((type(w).__name__, w.objectName(), label, w.isVisible()))
    if not hits:
        print("no matches")
        return
    for cls, objname, label, vis in hits[:40]:
        print(f"{cls} objectName={objname!r} text={label!r} visible={vis}")


def cmd_click(text: str = "") -> None:
    win = state["win"]
    if win is None:
        print("ERROR: launch first")
        return
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt
    text = text.strip()
    target = None
    for b in _iter_clickables():
        if b.isVisible() and b.text().strip() == text:
            target = b
            break
    if target is None:
        for b in _iter_clickables():
            if b.isVisible() and text.lower() in b.text().strip().lower():
                target = b
                break
    if target is None:
        print("NOT_FOUND:", text)
        return
    # Read the label BEFORE clicking - some buttons relabel themselves as a
    # side effect of the click (e.g. a wizard's primary action button going
    # from "Next ->" to "Skip"/"Finish" on the next step), so reading it
    # after would misreport what was actually clicked.
    label = target.text()
    QTest.mouseClick(target, Qt.LeftButton)
    _pump()
    print("clicked:", label)


def cmd_type(text: str = "") -> None:
    if state["win"] is None:
        print("ERROR: launch first")
        return
    from PySide6.QtTest import QTest
    focus = state["app"].focusWidget()
    if focus is None:
        print("ERROR: nothing focused")
        return
    QTest.keyClicks(focus, text)
    _pump()
    print("typed into", type(focus).__name__)


_KEYMAP: dict = {}


def cmd_key(name: str = "") -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    if not _KEYMAP:
        _KEYMAP.update({
            "Enter": Qt.Key_Return, "Return": Qt.Key_Return,
            "Escape": Qt.Key_Escape, "Esc": Qt.Key_Escape,
            "Tab": Qt.Key_Tab, "Backspace": Qt.Key_Backspace,
        })
    win = state["win"]
    if win is None:
        print("ERROR: launch first")
        return
    key = _KEYMAP.get(name.strip())
    if key is None:
        print("unknown key:", name, "- known:", ", ".join(_KEYMAP))
        return
    focus = state["app"].focusWidget() or win
    QTest.keyClick(focus, key)
    _pump()
    print("pressed", name)


def cmd_sleep(seconds: str = "1") -> None:
    """Let background worker threads (Nexus API calls, installs) finish and
    their Qt signals land - plain processEvents() alone won't wait for a
    thread, only drain what's already queued."""
    try:
        secs = float(seconds.strip() or "1")
    except ValueError:
        secs = 1.0
    app = state["app"]
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if app is not None:
            app.processEvents()
        time.sleep(0.05)
    print(f"slept {secs}s")


def cmd_eval(expr: str = "") -> None:
    try:
        result = eval(expr, {"app": state["app"], "win": state["win"]})
        print(repr(result))
    except Exception as exc:
        print("ERROR:", exc)


def cmd_quit(_arg: str = "") -> None:
    if state["win"] is not None:
        state["win"].close()
    print("bye")


COMMANDS = {
    "launch": cmd_launch,
    "ss": cmd_ss,
    "find": cmd_find,
    "click": cmd_click,
    "type": cmd_type,
    "key": cmd_key,
    "sleep": cmd_sleep,
    "eval": cmd_eval,
    "quit": cmd_quit,
    "help": lambda _a="": print("commands:", ", ".join(COMMANDS)),
}


def main() -> None:
    print("mosaic driver - 'help' for commands, 'launch' to start")
    sys.stdout.flush()
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        fn = COMMANDS.get(cmd)
        if fn is None:
            print("unknown:", cmd, "- try: help")
        else:
            try:
                fn(arg)
            except Exception:
                traceback.print_exc()
        sys.stdout.flush()
        if cmd == "quit":
            break


if __name__ == "__main__":
    main()

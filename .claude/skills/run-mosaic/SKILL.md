---
name: run-mosaic
description: Build, run, and drive Mosaic Mod Manager (the PySide6/Qt desktop app in this repo). Use when asked to start the app, launch the GUI, take a screenshot of it, click through a dialog/wizard, or verify a Qt UI change actually renders and works.
---

Mosaic Mod Manager is a native PySide6/Qt desktop app (not Electron - no
Chromium, no xvfb needed). Drive it via the Python REPL driver at
`.claude/skills/run-mosaic/driver.py`, run under `QT_QPA_PLATFORM=offscreen`
with an **isolated `XDG_CONFIG_HOME`** so it never touches a real user's
actual Mosaic profiles/installed mods. For a change scoped to a single
dialog/overlay rather than the whole app, skip the full driver and construct
just that widget directly (see "Direct widget invocation" below) - much
faster, and covers most of what PRs to this app's GUI actually touch.

All paths below are relative to the repo root.

## Prerequisites

None beyond the project's existing venv. This host's `../.venv` (relative to
`src/`) already has PySide6 6.11 and pytest installed, and the offscreen Qt
platform plugin worked with zero additional system packages - no xvfb, no
extra `apt-get` line was needed on this host. If `../.venv` doesn't exist,
`src/run_qt.sh` bootstraps it (`python3 -m venv ../.venv` then
`pip install -r requirements.txt`); the driver assumes that venv already
exists rather than rebuilding it.

## Run (agent path)

```bash
cd src
tmux new-session -d -s mosaic -x 220 -y 50
tmux send-keys -t mosaic \
  'XDG_CONFIG_HOME=/tmp/mosaic-driver-config SCREENSHOT_DIR=/tmp/mosaic-shots ../.venv/bin/python3 ../.claude/skills/run-mosaic/driver.py' Enter
timeout 15 bash -c "until tmux capture-pane -t mosaic -p | grep -q \"'launch' to start\"; do sleep 0.3; done"
tmux send-keys -t mosaic 'launch' Enter
timeout 20 bash -c 'until tmux capture-pane -t mosaic -p | grep -q "launched\."; do sleep 0.3; done'
tmux send-keys -t mosaic 'ss 01-first-look' Enter
timeout 10 bash -c 'until tmux capture-pane -t mosaic -p | grep -q "screenshot:"; do sleep 0.3; done'
tmux capture-pane -t mosaic -p
```

Then actually open the screenshot file. A fresh isolated config dir opens on
the first-run "Welcome to Mosaic Mod Manager" wizard (Step 1 of 3), not the
main mod list - that's expected and correct for an empty config, not a
launch failure.

Screenshots -> `$SCREENSHOT_DIR` (default `/tmp/mosaic-shots` if unset).

| command | what it does |
|---|---|
| `launch` | Construct `QApplication` + the real `MainWindow`, show it, pump events. Idempotent - a second `launch` is a no-op. |
| `ss [name]` | Screenshot the whole window (`QWidget.grab()`) to `<name>.png` (default `ss-<timestamp>.png`). |
| `find <text>` | List visible widgets (buttons, labels, anything with `.text()`) whose text contains `<text>`, with class/objectName/visibility. Use this before `click` to confirm the target exists and see its exact label. |
| `click <text>` | Click the first visible button whose label exactly matches `<text>`, else the first whose label *contains* it, via a real `QTest.mouseClick` (not a direct Python call to the handler). |
| `type <text>` | `QTest.keyClicks` into whatever widget currently has focus. |
| `key <name>` | Press a named key on the focused widget (or the window). Known: `Enter`/`Return`, `Escape`/`Esc`, `Tab`, `Backspace`. |
| `sleep <seconds>` | Pump the event loop for real wall-clock time - needed after triggering a background-thread action (a Nexus API call, an install) so its Qt signal has time to land; plain command dispatch only drains what's *already* queued. |
| `eval <python-expr>` | Last resort: `eval()`'d with `app` and `win` in scope. |
| `quit` | Closes the window and exits the REPL. |

## Direct widget invocation

Most PRs to this app touch one dialog/overlay, not the whole app - booting
`MainWindow` (which starts loading games/profiles) is unnecessary weight for
that. Construct the real widget class directly under a plain `QWidget` host
and drive it with `QTest`, no driver process needed:

```python
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
sys.path.insert(0, "src")   # from repo root

from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from gui_qt.overlays.text_input_overlay import TextInputOverlay   # the real class

app = QApplication([])
host = QMainWindow(); host.resize(900, 600); host.show()

overlay = TextInputOverlay(host, "Rename mod", "New name:", lambda v: None,
                           initial="Some Mod", ok_label="Rename")
app.processEvents()
host.grab().save("/tmp/overlay.png")            # look at this
btn = next(b for b in overlay.findChildren(QPushButton) if b.text() == "Rename")
QTest.mouseClick(btn, Qt.LeftButton)             # real Qt event, not overlay._confirm()
app.processEvents()
```

This is the same technique the driver uses internally, just scoped to one
widget - verified working (screenshots + real `QTest.mouseClick` + assertions
on the resulting state) while building this skill.

## Run (human path)

```bash
cd src && ./run_qt.sh
```

Opens a real window on the real display. Useless in a headless/offscreen
session - only for a human at the actual desktop. Ctrl-C or close the window
to stop.

## Test

```bash
.venv/bin/python3 -m pytest tests/ -q
```

93 tests, all pure-function unit tests (no Qt/GUI involved) - runs in well
under a second.

## Gotchas

- **`XDG_CONFIG_HOME` isolates the app's config/profiles/mods, but NOT Nexus
  OAuth login.** Launching with a fresh isolated config dir on this host
  still showed a real, already-logged-in Nexus account in the UI
  ("<username> @ NexusMods") - OAuth tokens live in the system keyring, not
  under `XDG_CONFIG_HOME`. Don't trigger real Nexus-authenticated actions
  (endorse, download, API calls that mutate anything) through the driver on
  a machine with a real login unless that's actually intended - `find`/`ss`/
  read-only navigation is safe, clicking into "Add a Game" and a real Nexus
  action is not.
- **Constructing `MainWindow(app)` directly bypasses `gui_qt.app.run()`'s
  bootstrap** - no `InstanceLock.acquire()`, no `NxmHandler.register()`. This
  is *why* the driver can hand control back to a REPL instead of blocking
  forever in `app.exec()`, and it's the right choice for driving the app
  programmatically - but it means the driver never exercises single-instance
  locking or `nxm://` handler registration. If a change touches either of
  those specifically, this driver won't catch a regression in them.
- **No `app.exec()` means events only advance when something pumps them.**
  The driver calls `processEvents()` after every command; a background
  thread's result (a Qt signal from a worker thread) only lands if you also
  give it real wall-clock time - use `sleep <n>`, not just another command,
  after triggering anything that runs on a thread.
- **A clicked button's label can change as a result of the click** (e.g. a
  wizard's primary button relabeling "Next ->" to "Skip" on the next step).
  `click` logs the label it matched *before* clicking, not after - reading
  it after would misreport what was actually clicked once this happens.
- **No xvfb needed.** This is a native Qt app, not Electron/Chromium - the
  `offscreen` Qt platform plugin (bundled in the PySide6 wheel) is enough by
  itself; don't reach for `xvfb-run`, it's not required here.

## Troubleshooting

- **`RuntimeWarning: Unexpected value in sys.prefix` / `sys.exec_prefix`**
  printed on every launch: harmless. The venv is at repo-root `.venv/` but
  invoked via the relative path `../.venv` from `src/` - Python's own site
  init compares the two and warns. Doesn't affect behavior; safe to ignore
  or filter with `grep -v`.

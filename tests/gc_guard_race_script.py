"""Subprocess helper for test_gc_guard.py (not a test itself).

Replays the collection-install hand-off that crashed Mosaic: the install worker
waits while the GUI thread shows a BAIN picker tab; clicking Install closes the tab
(deleteLater) and releases the worker, which starts a thread pool and allocates.
With PySide6 6.12 / Python 3.14 and the collector free to run on the worker thread
this segfaults inside the GC within ~20 rounds.

usage: gc_guard_race_script.py <guard|none> <rounds>
"""
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

app = QApplication([])

from gui_qt.views.bain_picker_view import BainPickerView  # noqa: E402
from Utils.installers.bain_installer import BainSubPackage  # noqa: E402

mode, rounds = sys.argv[1], int(sys.argv[2])
if mode == "guard":
    from gui_qt.gc_guard import install
    install(app)

subs = [BainSubPackage(name=f"{i:02d} Pkg{i}", display_name=f"Pkg{i}", path="/nonexistent",
                       file_keys={f"meshes/x{i}_{j}.nif" for j in range(12)}) for i in range(1, 17)]


class Bridge(QObject):
    show_picker = Signal(object)
    quit_now = Signal()


bridge = Bridge()
tabs = QTabWidget()
tabs.resize(800, 600)
tabs.show()


def on_show(payload):
    holder, ev = payload["holder"], payload["event"]

    def finish(result):
        holder["result"] = result
        idx = tabs.indexOf(view)
        if idx != -1:
            tabs.removeTab(idx)
        view.deleteLater()
        ev.set()                      # the worker is released at once, as in the app

    view = BainPickerView(payload["subpkgs"], "/nonexistent", "Mod", on_done=finish)
    tabs.addTab(view, "Install")
    QTimer.singleShot(0, view._ok)


bridge.show_picker.connect(on_show)
bridge.quit_now.connect(app.quit)
done = {"n": 0}


def worker():
    for i in range(rounds):
        holder = {"result": None}
        ev = threading.Event()
        bridge.show_picker.emit({"subpkgs": subs, "holder": holder, "event": ev})
        ev.wait()
        with ThreadPoolExecutor(max_workers=8) as pool:         # like _copy_file_list
            for _ in pool.map(lambda x: [str(x)] * 20, range(2000), chunksize=256):
                pass
        junk = [{"a": [k]} for k in range(3000)]                 # allocation churn
        del junk
        done["n"] = i + 1
    bridge.quit_now.emit()


threading.Thread(target=worker, daemon=True).start()
app.exec()
print(f"rounds={done['n']}", flush=True)

"""Modhold in the modlist right-click menu, and the Change Version flow."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication, QTableView

from gui_qt.modlist.modlist_data import FLAG_MODHOLD_UPDATE
from gui_qt.modlist.modlist_menu import build_context_menu
from gui_qt.modlist.modlist_model import ModListModel
from Modhold import modhold_key
from Modhold.modhold_meta import ModholdMeta, write_modhold_meta
from Utils.mods.modlist import ModEntry

URL = "https://modholdmods.com/g/skyrim-se/mods/12/shop"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(tmp_path, qapp):
    write_modhold_meta(tmp_path / "Shop" / "meta.ini",
                       ModholdMeta(mod_id=12, file_id=100, version="1", url=URL, name="Shop"))
    write_modhold_meta(tmp_path / "Other" / "meta.ini",
                       ModholdMeta(mod_id=13, file_id=5, version="1", url="", name="Other"))
    (tmp_path / "Plain").mkdir()
    (tmp_path / "Plain" / "meta.ini").write_text("[General]\nmodid=5\n", encoding="utf-8")
    model = ModListModel([ModEntry(n, True, False) for n in ("Shop", "Other", "Plain")])
    view = QTableView()
    view.setModel(model)
    view.staging_dir = tmp_path
    return view, model


def _menu(view, model, rows):
    sm = view.selectionModel()
    sm.clearSelection()
    for r in rows:
        sm.select(model.index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    return build_context_menu(view, model.index(rows[0], 0))


def _tree(menu):
    out = {}
    for a in menu.actions():
        if a.text():
            out[a.text().split("\t")[0]] = [x.text() for x in a.menu().actions()] if a.menu() else None
    return out


def test_a_modhold_mod_gets_modhold_actions_and_check_updates(panel):
    view, model = panel
    tree = _tree(_menu(view, model, [0]))
    assert tree["Modhold Actions"] == ["Change Version (Modhold)", "Open on Modhold"]
    assert "Check Updates" in tree
    assert "Nexus Actions" not in tree          # no Nexus id, so no Nexus submenu


def test_a_nexus_mod_is_unchanged(panel):
    view, model = panel
    tree = _tree(_menu(view, model, [2]))
    assert "Modhold Actions" not in tree
    assert "Nexus Actions" in tree and "Check Updates" in tree


def test_a_modhold_mod_without_a_stored_page_still_offers_change_version(panel):
    view, model = panel
    assert _tree(_menu(view, model, [1]))["Modhold Actions"] == ["Change Version (Modhold)"]


def test_multi_select_offers_check_updates_and_open_for_the_modhold_mods(panel):
    view, model = panel
    tree = _tree(_menu(view, model, [0, 1, 2]))
    assert tree["Modhold Actions"] == ["Check Updates (2)", "Open on Modhold (1)"]


def test_the_menu_items_call_back_into_the_window(panel, monkeypatch):
    view, model = panel
    got, opened = [], []
    view.on_change_version_modhold = got.append
    view.on_check_updates = lambda names: got.append(("check", list(names)))
    import Utils.xdg
    monkeypatch.setattr(Utils.xdg, "open_url", lambda url, *a, **k: opened.append(url))
    menu = _menu(view, model, [0])
    sub = next(a for a in menu.actions() if a.text() == "Modhold Actions").menu()
    next(a for a in sub.actions() if a.text() == "Change Version (Modhold)").trigger()
    next(a for a in sub.actions() if a.text() == "Open on Modhold").trigger()
    assert got == ["Shop"] and opened == [URL]
    next(a for a in menu.actions() if a.text() == "Check Updates").trigger()
    assert got[-1] == ("check", ["Shop"])


def test_quick_update_modhold_appears_only_when_flagged(panel):
    view, model = panel
    assert "Quick Update (Modhold)" not in _tree(_menu(view, model, [0]))
    model._flags["Shop"] = FLAG_MODHOLD_UPDATE
    done = []
    view.on_quick_update_modhold = done.append
    menu = _menu(view, model, [0])
    assert "Quick Update (Modhold)" in _tree(menu)
    next(a for a in menu.actions() if a.text() == "Quick Update (Modhold)").trigger()
    assert done == [["Shop"]]


# ---------------------------------------------------------------- Change Version flow

def test_single_choice_chooser_marks_and_preselects_the_installed_file(qapp):
    from gui_qt.modhold.modhold_file_chooser import ModholdFileChooser
    files = [{"id": 1, "kind": "main", "label": "Main file", "name": "a.zip",
              "version": "1.0", "size": 10},
             {"id": 2, "kind": "optional", "label": "Add-on", "name": "b.zip",
              "version": "2.0", "size": 20}]
    dlg = ModholdFileChooser("Shop", files, single=True, installed_id=2)
    assert [f["id"] for f in dlg.selected_files()] == [2]
    assert "installed" in dlg._boxes[1][0].text() and "installed" not in dlg._boxes[0][0].text()
    dlg._boxes[0][0].setChecked(True)               # radio buttons: one at a time
    assert [f["id"] for f in dlg.selected_files()] == [1]
    # an installed file that is gone falls back to the first one
    assert [f["id"] for f in ModholdFileChooser("S", files, single=True,
                                                installed_id=99).selected_files()] == [1]
    # the multi-file mode is unchanged: main file pre-ticked
    assert [f["id"] for f in ModholdFileChooser("S", files).selected_files()] == [1]


class _Emit:
    def __init__(self):
        self.calls, self.event = [], threading.Event()

    def emit(self, *a):
        self.calls.append(a)
        self.event.set()


def _window(tmp_path):
    from PySide6.QtWidgets import QWidget

    from gui_qt.app_parts.app_modhold import MainWindow as Mixin

    class _W(Mixin, QWidget):        # a real widget: the dialogs take it as their parent
        pass

    w = _W()
    w.notes = []
    game = SimpleNamespace(name="Skyrim SE", modhold_game_slug="skyrim-se",
                           is_configured=lambda: True)
    w._gs = SimpleNamespace(game=game, staging_dir=lambda: tmp_path)
    w._notify = lambda text, level="info", **k: w.notes.append((level, text))
    w._modhold_versions_ready = _Emit()
    w._modhold_update_ready = _Emit()
    w._install_running = False
    return w


def _files():
    return [{"id": 100, "kind": "main", "label": "Main file", "name": "a.zip",
             "version": "1.0", "size": 10, "sha256": "aa" * 32},
            {"id": 101, "kind": "optional", "label": "Add-on", "name": "b.zip",
             "version": "1.1", "size": 11, "sha256": "bb" * 32}]


def _chooser(monkeypatch, pick):
    """Answer the REAL ModholdFileChooser (only exec() is replaced) and remember the
    instance, so the production code reads a genuine ``QDialog.DialogCode`` result."""
    from PySide6.QtWidgets import QDialog

    from gui_qt.modhold.modhold_file_chooser import ModholdFileChooser

    seen = SimpleNamespace(last=None)

    def fake_exec(self):
        seen.last = self
        if pick is None:
            return QDialog.DialogCode.Rejected
        for box, f in self._boxes:
            box.setChecked(f["id"] == pick)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ModholdFileChooser, "exec", fake_exec)
    return seen


def test_change_version_rejects_a_non_modhold_mod(tmp_path, qapp):
    (tmp_path / "Plain").mkdir()
    (tmp_path / "Plain" / "meta.ini").write_text("[General]\nmodid=5\n", encoding="utf-8")
    w = _window(tmp_path)
    w._change_version_modhold("Plain")
    assert any("isn't a Modhold mod" in t for _l, t in w.notes)
    assert w._modhold_versions_ready.calls == []


def test_change_version_fetches_the_mod_off_thread(tmp_path, qapp, monkeypatch):
    write_modhold_meta(tmp_path / "Shop" / "meta.ini", ModholdMeta(mod_id=12, file_id=100))
    from Modhold import modhold_api
    monkeypatch.setattr(modhold_api.ModholdAPI, "get_mod",
                        lambda self, mid: {"id": mid, "files": _files()})
    w = _window(tmp_path)
    w._change_version_modhold("Shop")
    assert w._modhold_versions_ready.event.wait(5)
    name, detail, err = w._modhold_versions_ready.calls[0]
    assert (name, err) == ("Shop", "") and detail["id"] == 12


def test_picking_another_file_downloads_it_over_the_same_folder(tmp_path, qapp, monkeypatch):
    write_modhold_meta(tmp_path / "Shop" / "meta.ini",
                       ModholdMeta(mod_id=12, file_id=100, sha256="aa" * 32, game="skyrim-se",
                                   loadout="3"))
    monkeypatch.setattr(modhold_key, "load_modhold_key", lambda: "mhm_test")
    chooser = _chooser(monkeypatch, pick=101)
    from Modhold import modhold_update
    from Utils import config_paths
    monkeypatch.setattr(config_paths, "get_download_cache_dir_for_game", lambda n: tmp_path)
    seen = {}

    def fake_prepare(dl, detail, file, slug, *, mod_name, loadout="", **k):
        seen.update(file=file["id"], slug=slug, mod=mod_name, loadout=loadout)
        return modhold_update.UpdateJob(mod_name, "x.zip", ModholdMeta(file_id=file["id"])), ""

    monkeypatch.setattr(modhold_update, "prepare_file", fake_prepare)
    w = _window(tmp_path)
    w._on_modhold_versions_ready("Shop", {"id": 12, "files": _files(), "can_download": True}, "")
    dlg = chooser.last
    assert dlg._group is not None                                   # single-choice mode
    assert "installed" in dlg._boxes[0][0].text()                   # file 100 is the installed one
    assert w._modhold_update_ready.event.wait(5)
    queue, skipped = w._modhold_update_ready.calls[0]
    assert skipped == [] and queue[0].info.file_id == 101
    assert seen == {"file": 101, "slug": "skyrim-se", "mod": "Shop", "loadout": "3"}


def test_picking_the_installed_file_does_nothing_but_say_so(tmp_path, qapp, monkeypatch):
    write_modhold_meta(tmp_path / "Shop" / "meta.ini",
                       ModholdMeta(mod_id=12, file_id=100, sha256="aa" * 32))
    _chooser(monkeypatch, pick=100)
    w = _window(tmp_path)
    w._on_modhold_versions_ready("Shop", {"id": 12, "files": _files(), "can_download": True}, "")
    assert any("already installed" in t for _l, t in w.notes)
    assert w._modhold_update_ready.calls == []


def test_cancelling_the_chooser_does_nothing(tmp_path, qapp, monkeypatch):
    write_modhold_meta(tmp_path / "Shop" / "meta.ini", ModholdMeta(mod_id=12, file_id=100))
    _chooser(monkeypatch, pick=None)
    w = _window(tmp_path)
    w._on_modhold_versions_ready("Shop", {"id": 12, "files": _files()}, "")
    assert w.notes == [] and w._modhold_update_ready.calls == []


def test_a_gated_mod_is_refused_and_a_load_error_is_shown(tmp_path, qapp, monkeypatch):
    write_modhold_meta(tmp_path / "Shop" / "meta.ini", ModholdMeta(mod_id=12, file_id=100))
    _chooser(monkeypatch, pick=101)
    w = _window(tmp_path)
    w._on_modhold_versions_ready("Shop", {"id": 12, "files": _files(), "can_download": False}, "")
    assert any("cannot download" in t for _l, t in w.notes)
    assert w._modhold_update_ready.calls == []
    w._on_modhold_versions_ready("Shop", None, "site down")
    assert any("site down" in t for _l, t in w.notes)


def test_the_real_dialog_result_compares_with_the_code_the_app_checks(qapp):
    """The exact comparison the app makes (``exec() != QDialog.DialogCode.Accepted``),
    against the real dialog's result code. accept()/reject() set the same value exec()
    returns; a nested event loop is avoided on purpose, because the first real exec()
    in this suite trips a pre-existing crash in an unrelated test's leftover widget."""
    from PySide6.QtWidgets import QDialog

    from gui_qt.modhold.modhold_file_chooser import ModholdFileChooser
    files = [{"id": 1, "kind": "main", "label": "Main file", "name": "a.zip",
              "version": "1.0", "size": 10}]
    accepted = ModholdFileChooser("Shop", files)
    accepted.accept()
    assert accepted.result() == QDialog.DialogCode.Accepted
    rejected = ModholdFileChooser("Shop", files)
    rejected.reject()
    assert rejected.result() != QDialog.DialogCode.Accepted

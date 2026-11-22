"""Loadouts list, the external-items dialog, and the main-window glue that starts an install."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from gui_qt.modhold import modhold_loadouts_view as lv
from gui_qt.modhold.modhold_external_items import ModholdExternalItems, external_items_html
from Modhold import modhold_key
from Modhold.loadout_manifest import parse_loadout, plan_install
from Utils.collections.collection_install import CollectionInstallControl


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# ----------------------------------------------------------------------- list view

def _summary(i, **kw):
    base = {"id": i, "name": f"Loadout {i}", "summary": "s" * 400, "by": "Seth", "mods": 72,
            "installs": 38, "updated": "2026-11-20 16:04:56",
            "page": f"https://modholdmods.com/loadouts/{i}"}
    base.update(kw)
    return base


class _Api:
    def __init__(self, items=None, error=None):
        self.items, self.error, self.calls = items or [], error, []

    def list_loadouts(self, game, q=""):
        self.calls.append((game, q))
        if self.error:
            raise RuntimeError(self.error)
        return self.items


def _view(qapp, api, installed=None):
    return lv.ModholdLoadoutsView(SimpleNamespace(name="Skyrim SE"), "skyrim-se",
                                  install_fn=(installed.append if installed is not None else (lambda s: None)),
                                  api_factory=lambda: api)


def test_lists_loadouts_with_their_facts(qapp):
    api = _Api([_summary(3), _summary(6, name="Small", mods=1, installs=0)])
    v = _view(qapp, api)
    assert pump(lambda: len(v._cards) == 2)
    assert api.calls[0] == ("skyrim-se", "")
    card = v._cards[0]
    assert card.loadout["id"] == 3
    texts = [lbl.text() for lbl in card.findChildren(type(v._status))]
    assert any("by Seth · 72 mods · 38 installs · updated 2026-11-20" in t for t in texts)
    assert any(t.endswith("…") and len(t) <= 261 for t in texts)        # long summary is cut
    v._closing = True


def test_install_button_hands_the_loadout_to_the_app(qapp):
    got = []
    v = _view(qapp, _Api([_summary(3)]), installed=got)
    pump(lambda: v._cards)
    from PySide6.QtWidgets import QPushButton
    next(b for b in v._cards[0].findChildren(QPushButton) if b.text() == "Install").click()
    assert got and got[0]["id"] == 3
    v._closing = True


def test_search_sends_the_query_and_an_older_result_is_ignored(qapp):
    api = _Api([_summary(3)])
    v = _view(qapp, api)
    pump(lambda: v._cards)
    v._search.setText("estates")
    v._load()
    assert pump(lambda: api.calls[-1] == ("skyrim-se", "estates"))
    stale = v._gen - 1
    v._on_loaded({"gen": stale, "items": [_summary(99)]}, "")
    assert all(c.loadout["id"] != 99 for c in v._cards)
    v._closing = True


def test_an_empty_result_and_an_error_are_both_said(qapp):
    v = _view(qapp, _Api([]))
    assert pump(lambda: v._status.text() == "No loadouts found.")
    bad = _view(qapp, _Api(error="site down"))
    assert pump(lambda: "site down" in bad._status.text())
    v._closing = bad._closing = True


# --------------------------------------------------------------- external items dialog

def test_external_items_html_links_and_escapes():
    html = external_items_html([("SkyUI <b>", "https://n/1?a=1&b=2", "needed & required"),
                                ("No link", "", "")])
    assert 'href="https://n/1?a=1&amp;b=2"' in html and "SkyUI &lt;b&gt;" in html
    assert "needed &amp; required" in html and html.count("•") == 2
    assert "<br>" in html


def test_the_dialog_names_the_loadout_the_count_and_the_saved_list(qapp):
    dlg = ModholdExternalItems("Estates", [("SkyUI", "https://n/1", "")])
    from PySide6.QtWidgets import QLabel
    text = " ".join(lbl.text() for lbl in dlg.findChildren(QLabel))
    assert "Estates includes 1 item(s)" in text and "modhold_loadout.json" in text
    assert "SkyUI" in text


# ----------------------------------------------------------------------- app glue

def _item(pos, mod_id, fid, **kw):
    return {"position": pos, "optional": kw.get("optional", False), "note": "",
            "mod": {"id": mod_id, "name": f"Mod {mod_id}", "version": "1", "author": "A",
                    "page": f"https://modholdmods.com/m/{mod_id}"},
            "locked": None, "files": [{"id": fid, "name": f"f{fid}.zip", "version": "1",
                                       "size": 10, "sha256": "ab" * 32}], "nexus": None}


def _ext(pos, name):
    return {"position": pos, "optional": False, "note": "n",
            "mod": {"id": None, "name": name, "page": f"https://n/{name}"}, "locked": None,
            "files": [], "nexus": f"https://n/{name}"}


def _loadout(items=None, slug="skyrim-se"):
    return parse_loadout({"format": "modhold-loadout", "version": 1, "id": 3, "name": "Estates",
                          "by": "Seth", "game": {"slug": slug, "name": "Skyrim SE"},
                          "items": items if items is not None else [_item(1, 10, 100), _ext(2, "SkyUI")]})


class _Emit:
    def __init__(self):
        self.calls, self.event = [], threading.Event()

    def emit(self, *a):
        self.calls.append(a)
        self.event.set()


def _window(tmp_path, game_slug="skyrim-se"):
    from gui_qt.app_parts.app_modhold import MainWindow as Mixin

    class _W(Mixin, QWidget):
        pass

    w = _W()
    w.notes, w.logs, w.started = [], [], []
    game = SimpleNamespace(name="Skyrim SE", modhold_game_slug=game_slug,
                           is_configured=lambda: True,
                           get_profile_root=lambda: tmp_path, _active_profile_dir=None)
    w._gs = SimpleNamespace(game=game)
    w._notify = lambda text, level="info", **k: w.notes.append((level, text))
    w._append_log = w.logs.append
    w._op_log, w._col_finished = _Emit(), _Emit()
    w._modhold_loadout_ready = _Emit()
    w._modhold_nexus_ready = _Emit()
    w._ensure_nexus_api = lambda: None
    w._build_collection_callbacks = lambda: "CALLBACKS"
    w._on_col_pause_clicked = w._on_col_cancel_clicked = w._on_col_limit_changed = lambda *a: None
    w._show_modhold_external_items = lambda name, items: w.started.append(("external", name, items))
    return w


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(modhold_key, "load_modhold_key", lambda: "mhm_test")


def test_pressing_install_fetches_the_loadout_off_thread(tmp_path, qapp, keyed, monkeypatch):
    from Modhold import modhold_api
    payload = {"format": "modhold-loadout", "version": 1, "id": 3, "name": "Estates",
               "by": "S", "game": {"slug": "skyrim-se"}, "items": []}
    monkeypatch.setattr(modhold_api.ModholdAPI, "get_loadout", lambda self, i: payload)
    w = _window(tmp_path)
    w._install_modhold_loadout({"id": 3})
    assert w._modhold_loadout_ready.event.wait(5)
    lo, err = w._modhold_loadout_ready.calls[0]
    assert err == "" and lo.id == 3 and lo.name == "Estates"


def test_install_needs_a_key_and_no_other_install_running(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(modhold_key, "load_modhold_key", lambda: "")
    w = _window(tmp_path)
    w._install_modhold_loadout({"id": 3})
    assert any("Connect your Modhold account" in t for _l, t in w.notes)
    w2 = _window(tmp_path)
    w2._col_install_running = True
    w2._install_modhold_loadout({"id": 3})
    assert any("already running" in t for _l, t in w2.notes)
    assert w._modhold_loadout_ready.calls == [] and w2._modhold_loadout_ready.calls == []


def test_a_fetch_error_is_reported(tmp_path, qapp):
    w = _window(tmp_path)
    w._on_modhold_loadout_ready(None, "site down")
    assert any("site down" in t for _l, t in w.notes)


def test_a_loadout_for_another_game_is_refused(tmp_path, qapp):
    w = _window(tmp_path)
    w._on_modhold_loadout_ready(_loadout(slug="fallout-4"), "")
    assert any("another game" in t for _l, t in w.notes) and w.started == []


def _answer_preflight(monkeypatch, accept, **choices):
    from PySide6.QtWidgets import QDialog

    from gui_qt.modhold.modhold_loadout_preflight import ModholdLoadoutPreflight

    def fake_exec(self):
        for box, idx in self._optional:
            box.setChecked(choices.get("optional", True))
        return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

    monkeypatch.setattr(ModholdLoadoutPreflight, "exec", fake_exec)


def test_accepting_the_preflight_starts_the_install_with_its_choices(tmp_path, qapp, monkeypatch):
    _answer_preflight(monkeypatch, True)
    from Utils.exe_launch import game_helpers
    monkeypatch.setattr(game_helpers, "_profiles_for_game", lambda name: ["Other"])
    w = _window(tmp_path)
    w._start_modhold_loadout = lambda lo, plan, name, cont, **kw: w.started.append(
        (lo.id, name, cont, len(plan.installable)))
    w._on_modhold_loadout_ready(_loadout(), "")
    assert w.started == [(3, "Estates", False, 1)]


def test_cancelling_the_preflight_starts_nothing(tmp_path, qapp, monkeypatch):
    _answer_preflight(monkeypatch, False)
    from Utils.exe_launch import game_helpers
    monkeypatch.setattr(game_helpers, "_profiles_for_game", lambda name: [])
    w = _window(tmp_path)
    w._start_modhold_loadout = lambda *a: w.started.append("started")
    w._on_modhold_loadout_ready(_loadout(), "")
    assert w.started == []


def test_a_loadout_with_nothing_hosted_only_shows_the_list_to_fetch(tmp_path, qapp, monkeypatch):
    _answer_preflight(monkeypatch, True)
    from Utils.exe_launch import game_helpers
    monkeypatch.setattr(game_helpers, "_profiles_for_game", lambda name: [])
    w = _window(tmp_path)
    w._start_modhold_loadout = lambda *a: w.started.append("started")
    w._on_modhold_loadout_ready(_loadout([_ext(1, "SkyUI"), _ext(2, "RaceMenu")]), "")
    assert any("can be downloaded automatically" in t for _l, t in w.notes)
    assert [s[0] for s in w.started] == ["external"] and len(w.started[0][2]) == 2


def _patch_start(monkeypatch, tmp_path, runner):
    from Utils.exe_launch import game_helpers
    from gui_qt.collections import collection_install_overlay as cio
    made = []
    profile = tmp_path / "profiles" / "Estates"
    profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(game_helpers, "_create_profile",
                        lambda name, pname, profile_specific_mods=False: made.append(pname) or profile)
    overlay = []
    monkeypatch.setattr(cio.CollectionInstallOverlay, "show_over",
                        classmethod(lambda cls, win, title, **kw: overlay.append((title, kw)) or "OVERLAY"))
    import Modhold.loadout_install as li
    monkeypatch.setattr(li, "run_loadout_install", runner)
    return made, overlay, profile


def test_starting_hands_the_shared_finish_handler_the_state_it_expects(tmp_path, qapp, keyed, monkeypatch):
    seen = {}

    def runner(**kw):
        seen.update(kw)

    made, overlay, profile = _patch_start(monkeypatch, tmp_path, runner)
    w = _window(tmp_path)
    lo = _loadout()
    plan = plan_install(lo)
    w._start_modhold_loadout(lo, plan, "Estates", False)
    assert pump(lambda: "profile_dir" in seen)
    assert made == ["Estates"] and seen["profile_dir"] == profile
    assert seen["continue_existing"] is False and seen["plan"] is plan and seen["loadout"] is lo
    assert seen["callbacks"] == "CALLBACKS" and isinstance(seen["control"], CollectionInstallControl)
    assert w._col_install_running is True and w._col_created_profile is True
    assert w._col_profile_name == "Estates" and w._col_install_slug == "modhold-loadout-3"
    assert w._col_offsite == [] and w._col_install_control is seen["control"]
    assert w._col_loadout == {"loadout": lo}
    assert w._col_install_overlay == "OVERLAY" and "Estates" in overlay[0][0]


def test_continuing_reuses_the_profile_and_never_marks_it_deletable(tmp_path, qapp, keyed, monkeypatch):
    seen = {}
    made, _overlay, profile = _patch_start(monkeypatch, tmp_path, lambda **kw: seen.update(kw))
    w = _window(tmp_path)
    w._start_modhold_loadout(_loadout(), plan_install(_loadout()), "Estates", True)
    assert pump(lambda: "profile_dir" in seen)
    assert made == [] and seen["continue_existing"] is True
    assert w._col_created_profile is False                 # a cancel must not delete it


def test_continuing_a_missing_profile_is_an_error(tmp_path, qapp, keyed, monkeypatch):
    _patch_start(monkeypatch, tmp_path, lambda **kw: None)
    w = _window(tmp_path)
    w._start_modhold_loadout(_loadout(), plan_install(_loadout()), "Nope", True)
    assert any("not found" in t for _l, t in w.notes)
    assert getattr(w, "_col_install_running", False) is False


def test_an_unexpected_error_keeps_the_progress_and_reports_failed(tmp_path, qapp, keyed, monkeypatch):
    def runner(**kw):
        raise RuntimeError("kaput")

    _made, _overlay, profile = _patch_start(monkeypatch, tmp_path, runner)
    w = _window(tmp_path)
    w._start_modhold_loadout(_loadout(), plan_install(_loadout()), "Estates", False)
    assert w._col_finished.event.wait(5)
    kind, payload = w._col_finished.calls[0]
    assert kind == "failed" and payload["error"] == "kaput" and payload["profile_dir"] == str(profile)


def test_the_finish_reminder_hands_a_loadout_to_its_own_follow_up_and_clears_it(qapp):
    from gui_qt.app import MainWindow
    shown = []
    state = {"loadout": "L", "profile_dir": "P", "phase": "auto"}
    fake = SimpleNamespace(_col_loadout=state, _col_offsite=[("x", "y")],
                           _modhold_after_install=lambda st, un: shown.append((st, un)))
    MainWindow._show_offsite_reminder(fake)
    assert shown == [(state, [("x", "y")])]
    assert fake._col_loadout is None and fake._col_offsite == []
    # without a loadout the Nexus off-site behaviour is untouched (nothing listed: no dialog)
    plain = SimpleNamespace(_col_loadout=None, _col_offsite=[])
    MainWindow._show_offsite_reminder(plain)
    assert plain._col_offsite == []


# ------------------------------------------------------------ what is left after an install

def _two_ext_loadout():
    return _loadout([_item(1, 10, 100), _ext(2, "High Poly Head"), _ext(3, "LL Thing")])


def test_what_could_not_be_fetched_is_shown_as_a_list_of_links(tmp_path, qapp):
    w = _window(tmp_path)
    lo = _two_ext_loadout()
    unresolved = [(i.name, i.link) for i in lo.items if not i.hosted]
    w._modhold_after_install({"loadout": lo}, unresolved)
    (kind, name, items), = w.started
    assert kind == "external" and name == "Estates"
    assert [i.name for i in items] == ["High Poly Head", "LL Thing"]
    assert [i.note for i in items] == ["n", "n"]                 # the author's notes come along


def test_with_nothing_left_no_list_is_shown(tmp_path, qapp):
    w = _window(tmp_path)
    w._modhold_after_install({"loadout": _loadout()}, [])
    assert w.started == []


def test_the_list_follows_the_loadouts_own_order_and_ignores_unknown_entries(qapp):
    from Modhold.loadout_install import items_for_unresolved
    lo = _loadout([_ext(1, "A"), _item(2, 10, 100), _ext(3, "B"), _ext(4, "A")])   # "A" listed twice
    got = items_for_unresolved(lo, [("B", "https://n/B"), ("A", "https://n/A"), ("Nope", "https://z")])
    assert [i.name for i in got] == ["A", "B"]


def test_the_list_tells_the_user_the_links_are_not_checked(qapp):
    from PySide6.QtWidgets import QLabel
    dlg = ModholdExternalItems("Estates", [("High Poly Head", "https://x/1", "")])
    text = " ".join(lbl.text() for lbl in dlg.findChildren(QLabel))
    assert "come from the loadout's author" in text
    assert "Only download from sites you trust" in text


def test_the_finish_reminder_hands_a_loadout_to_its_follow_up_and_clears_it(qapp):
    from gui_qt.app import MainWindow
    shown = []
    state = {"loadout": "L"}
    fake = SimpleNamespace(_col_loadout=state, _col_offsite=[("x", "y")],
                           _modhold_after_install=lambda st, un: shown.append((st, un)))
    MainWindow._show_offsite_reminder(fake)
    assert shown == [(state, [("x", "y")])]
    assert fake._col_loadout is None and fake._col_offsite == []
    # without a loadout the Nexus off-site behaviour is untouched (nothing listed: no dialog)
    plain = SimpleNamespace(_col_loadout=None, _col_offsite=[])
    MainWindow._show_offsite_reminder(plain)
    assert plain._col_offsite == []


def test_the_nexus_manual_card_is_exactly_as_it_was(qapp):
    """Mosaic's own non-premium Nexus card is untouched by the loadout work."""
    import queue as _q

    from PySide6.QtWidgets import QLabel

    from gui_qt.collections.collection_manual_overlay import CollectionManualOverlay
    host = QWidget()
    host.resize(900, 700)
    ov = CollectionManualOverlay(host, "C", "P", 2, _q.Queue())
    assert any("Non-premium" in lbl.text() for lbl in ov.findChildren(QLabel))
    ov.update_mod({"name": "Required", "optional": False, "file_name": "a.7z",
                   "url": "https://x", "idx": 1, "total": 2})
    assert ov._skip_btn.isHidden() and ov._hint_lbl.text() == "Expected file: a.7z"
    ov.update_mod({"name": "Opt", "optional": True, "url": "https://x", "idx": 2, "total": 2})
    assert not ov._skip_btn.isHidden()
    ov.dismiss()
    host.deleteLater()


# ------------------------------------------------------------ the Nexus part of the flow

NEXUS_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/{}"


def _nexus_ext(pos, name, mod_id):
    return {"position": pos, "optional": False, "note": "",
            "mod": {"id": None, "name": name, "page": NEXUS_URL.format(mod_id)}, "locked": None,
            "files": [], "nexus": NEXUS_URL.format(mod_id)}


def _nexus_loadout():
    return _loadout([_item(1, 10, 100), _nexus_ext(2, "SkyUI", 12604), _ext(3, "High Poly Head")])


class _NexusApiUser:
    def __init__(self, premium=True, error=None):
        self.premium, self.error = premium, error

    def validate(self):
        if self.error:
            raise RuntimeError(self.error)
        return SimpleNamespace(is_premium=self.premium)


def _prefs(monkeypatch, *, last_premium=False, forced_manual=False):
    from Utils import ui_config
    saved = []
    monkeypatch.setattr(ui_config, "save_nexus_last_premium", saved.append)
    monkeypatch.setattr(ui_config, "load_nexus_last_premium", lambda: last_premium)
    monkeypatch.setattr(ui_config, "load_force_manual_install", lambda: forced_manual)
    return saved


def test_a_loadout_without_nexus_links_goes_straight_to_the_preflight(tmp_path, qapp):
    w = _window(tmp_path)
    w._show_modhold_preflight = lambda lo, picks, note: w.started.append(("pre", picks, note))
    w._on_modhold_loadout_ready(_loadout(), "")
    assert w.started == [("pre", None, "")] and w._modhold_nexus_ready.calls == []


def test_nexus_links_without_a_nexus_login_are_explained_and_left_on_the_list(tmp_path, qapp):
    w = _window(tmp_path)                                   # _ensure_nexus_api() -> None
    w._show_modhold_preflight = lambda lo, picks, note: w.started.append((picks, note))
    w._on_modhold_loadout_ready(_nexus_loadout(), "")
    (picks, note), = w.started
    assert picks is None and "Log in to Nexus" in note and "Premium" in note


def test_a_premium_account_gets_its_nexus_files_chosen_in_the_background(tmp_path, qapp, monkeypatch):
    from Modhold import nexus_links
    saved = _prefs(monkeypatch)
    seen = {}

    def fake_resolve(api, items, **kw):
        seen["names"] = [i.name for i in items]
        return {"picked": True}

    monkeypatch.setattr(nexus_links, "resolve_nexus_picks", fake_resolve)
    w = _window(tmp_path)
    w._ensure_nexus_api = lambda: _NexusApiUser(premium=True)
    w._on_modhold_loadout_ready(_nexus_loadout(), "")
    assert w._modhold_nexus_ready.event.wait(5)
    lo, picks, note = w._modhold_nexus_ready.calls[0]
    assert picks == {"picked": True} and note == "" and seen["names"] == ["SkyUI"]
    assert saved == [True]


def test_without_premium_nothing_is_resolved_and_the_user_is_told(tmp_path, qapp, monkeypatch):
    from Modhold import nexus_links
    _prefs(monkeypatch)
    monkeypatch.setattr(nexus_links, "resolve_nexus_picks",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not resolve")))
    w = _window(tmp_path)
    w._ensure_nexus_api = lambda: _NexusApiUser(premium=False)
    w._on_modhold_loadout_ready(_nexus_loadout(), "")
    assert w._modhold_nexus_ready.event.wait(5)
    _lo, picks, note = w._modhold_nexus_ready.calls[0]
    assert picks is None and "Premium is needed" in note


def test_a_failed_premium_check_uses_the_last_known_status(tmp_path, qapp, monkeypatch):
    from Modhold import nexus_links
    _prefs(monkeypatch, last_premium=True)
    monkeypatch.setattr(nexus_links, "resolve_nexus_picks", lambda *a, **k: {"ok": 1})
    w = _window(tmp_path)
    w._ensure_nexus_api = lambda: _NexusApiUser(error="network down")
    w._on_modhold_loadout_ready(_nexus_loadout(), "")
    assert w._modhold_nexus_ready.event.wait(5)
    assert w._modhold_nexus_ready.calls[0][1] == {"ok": 1}
    assert any("last known status" in c[0] for c in w._op_log.calls)


def test_forced_manual_mode_keeps_nexus_items_on_the_list(tmp_path, qapp, monkeypatch):
    _prefs(monkeypatch, forced_manual=True)
    w = _window(tmp_path)
    w._ensure_nexus_api = lambda: _NexusApiUser(premium=True)
    w._on_modhold_loadout_ready(_nexus_loadout(), "")
    assert w._modhold_nexus_ready.event.wait(5)
    assert w._modhold_nexus_ready.calls[0][1] is None


def test_a_listing_failure_falls_back_to_the_list_with_the_reason(tmp_path, qapp, monkeypatch):
    from Modhold import nexus_links
    _prefs(monkeypatch)
    monkeypatch.setattr(nexus_links, "resolve_nexus_picks",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graphql down")))
    w = _window(tmp_path)
    w._ensure_nexus_api = lambda: _NexusApiUser(premium=True)
    w._on_modhold_loadout_ready(_nexus_loadout(), "")
    assert w._modhold_nexus_ready.event.wait(5)
    _lo, picks, note = w._modhold_nexus_ready.calls[0]
    assert picks is None and "graphql down" in note


def test_the_resolved_choices_open_the_preflight(tmp_path, qapp):
    w = _window(tmp_path)
    w._show_modhold_preflight = lambda lo, picks, note: w.started.append((lo.id, picks, note))
    w._on_modhold_nexus_ready(_nexus_loadout(), {"x": 1}, "n")
    assert w.started == [(3, {"x": 1}, "n")]


def _preflight_with_picks(monkeypatch, tmp_path, picks):
    _answer_preflight(monkeypatch, True)
    from Utils.exe_launch import game_helpers
    monkeypatch.setattr(game_helpers, "_profiles_for_game", lambda name: [])
    w = _window(tmp_path)
    w._start_modhold_loadout = lambda lo, plan, name, cont, nexus_picks=None: w.started.append(
        ([i.name for i in plan.ordered()], nexus_picks))
    w._show_modhold_preflight(_nexus_loadout(), picks, "")
    return w


def test_accepting_with_nexus_choices_passes_them_on_and_includes_the_nexus_items(tmp_path, qapp, monkeypatch):
    from Modhold.nexus_links import NexusPick, NexusRef
    lo = _nexus_loadout()
    item = next(i for i in lo.items if i.nexus_ref)
    pick = NexusPick(item.order_index, "SkyUI", NexusRef("skyrimspecialedition", 12604),
                     SimpleNamespace(file_id=5, file_name="SkyUI-5.7z", version="5.2", name="Main"))
    w = _preflight_with_picks(monkeypatch, tmp_path, {item.order_index: pick})
    (names, passed), = w.started
    assert names == ["Mod 10", "SkyUI"] and passed == {item.order_index: pick}


def test_accepting_without_nexus_passes_nothing_and_leaves_the_items_to_the_user(tmp_path, qapp, monkeypatch):
    w = _preflight_with_picks(monkeypatch, tmp_path, None)
    (names, passed), = w.started
    assert names == ["Mod 10"] and passed is None


def test_starting_with_nexus_choices_gives_the_installer_a_nexus_connection(tmp_path, qapp, keyed, monkeypatch):
    from Modhold.nexus_links import NexusPick, NexusRef
    seen = {}
    _patch_start(monkeypatch, tmp_path, lambda **kw: seen.update(kw))
    lo = _nexus_loadout()
    item = next(i for i in lo.items if i.nexus_ref)
    picks = {item.order_index: NexusPick(item.order_index, "SkyUI",
                                         NexusRef("skyrimspecialedition", 12604),
                                         SimpleNamespace(file_id=5))}
    plan = plan_install(lo, nexus_items={item.order_index})
    api = object()
    w = _window(tmp_path)
    w._ensure_nexus_api = lambda: api
    w._start_modhold_loadout(lo, plan, "Estates", False, nexus_picks=picks)
    assert pump(lambda: "profile_dir" in seen)
    assert seen["nexus_picks"] is picks and seen["nexus_api"] is api
    assert type(seen["nexus_downloader"]).__name__ == "NexusDownloader"


def test_starting_without_nexus_items_makes_no_nexus_connection(tmp_path, qapp, keyed, monkeypatch):
    seen = {}
    _patch_start(monkeypatch, tmp_path, lambda **kw: seen.update(kw))
    w = _window(tmp_path)
    w._ensure_nexus_api = lambda: (_ for _ in ()).throw(AssertionError("must not connect"))
    w._start_modhold_loadout(_loadout(), plan_install(_loadout()), "Estates", False)
    assert pump(lambda: "profile_dir" in seen)
    assert seen["nexus_api"] is None and seen["nexus_downloader"] is None

"""ModholdBrowserView: render, filter, page, install hand-off (fake API and downloader)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from gui_qt.modhold import modhold_browser_view as bv
from Modhold import modhold_key
from Modhold.modhold_download import ModholdDownloadResult


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _mod(i, adult=False, **kw):
    base = {"id": i, "name": f"Mod {i}", "summary": "s", "version": "1.0",
            "category": "Gameplay", "author": {"handle": "a", "name": "Author"},
            "adult": adult, "access": "free", "price_cents": 0, "beta": False,
            "supporters_until": None, "numbers": {"downloads": 10, "score": None,
                                                   "scores": 0, "vouches": 0},
            "page": f"https://modholdmods.com/g/skyrim-se/mods/{i}/x", "cover": ""}
    base.update(kw)
    return base


class _FakeApi:
    def __init__(self, mods, total=None, details=None):
        self.mods, self.total = mods, total if total is not None else len(mods)
        self.details = details or {}
        self.list_calls = []

    def list_mods(self, game, sort="updated", q="", page=1, **_):
        self.list_calls.append({"game": game, "sort": sort, "q": q, "page": page})
        return {"total": self.total, "page": page, "per_page": 50, "mods": self.mods}

    def get_mod(self, mod_id):
        return self.details[mod_id]


def _auto_chooser(monkeypatch, pick):
    """Answer the REAL ModholdFileChooser: only exec() is replaced, so the code that
    reads the result (``QDialog.DialogCode.Accepted``) runs for real. *pick* is a set
    of file ids to tick, or None to cancel."""
    from PySide6.QtWidgets import QDialog

    from gui_qt.modhold.modhold_file_chooser import ModholdFileChooser

    def fake_exec(self):
        if pick is None:
            return QDialog.DialogCode.Rejected
        for box, f in self._boxes:
            box.setChecked(f["id"] in pick)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ModholdFileChooser, "exec", fake_exec)


def pump(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return cond()


@pytest.fixture
def env(app, monkeypatch, tmp_path):
    monkeypatch.setattr(modhold_key, "load_modhold_key", lambda: "mhm_test")
    monkeypatch.setattr(modhold_key, "load_modhold_user", lambda: "Sunny")
    monkeypatch.setattr(bv, "get_download_cache_dir_for_game", lambda name: tmp_path)
    made = []

    def make(api, **kw):
        installs, opened, progress = [], [], []
        v = bv.ModholdBrowserView(
            SimpleNamespace(name="Skyrim SE"), "skyrim-se",
            install_fn=lambda paths, metas=None: installs.append((paths, metas)),
            log_fn=lambda *_: None,
            progress_fn=lambda *a: progress.append(a),
            open_account_fn=lambda: opened.append(1),
            api_factory=lambda: api, **kw)
        v.installs, v.opened, v.progress = installs, opened, progress
        made.append(v)
        return v

    yield make, tmp_path
    for v in made:
        v._closing = True
        v._pool.shutdown(wait=False, cancel_futures=True)


def test_renders_cards_and_hides_adult_by_default(env):
    make, _ = env
    api = _FakeApi([_mod(1), _mod(2, adult=True), _mod(3)])
    v = make(api)
    assert pump(lambda: len(v._cards) == 2)
    assert set(v._cards) == {1, 3}
    assert "1 adult mod(s) hidden" in v._status.text()
    v._adult.setChecked(True)
    assert set(v._cards) == {1, 2, 3}


def test_first_load_asks_for_page_one_with_the_game_slug(env):
    make, _ = env
    api = _FakeApi([_mod(1)])
    v = make(api)
    assert pump(lambda: api.list_calls)
    assert api.list_calls[0] == {"game": "skyrim-se", "sort": "updated", "q": "", "page": 1}
    assert pump(lambda: "Page 1 of 1" in v._page_lbl.text())


def test_search_and_sort_are_sent(env):
    make, _ = env
    api = _FakeApi([_mod(1)])
    v = make(api)
    pump(lambda: api.list_calls)
    v._search.setText("winterhold")
    v._sort.setCurrentIndex(2)       # triggers a reload with "downloads"
    assert pump(lambda: any(c["sort"] == "downloads" for c in api.list_calls))
    v._load(1)
    assert pump(lambda: api.list_calls[-1]["q"] == "winterhold")


def test_paging_buttons_follow_the_total(env):
    make, _ = env
    api = _FakeApi([_mod(1)], total=177)
    v = make(api)
    assert pump(lambda: "Page 1 of 4" in v._page_lbl.text())
    assert not v._prev.isEnabled() and v._next.isEnabled()
    v._next.click()
    assert pump(lambda: api.list_calls[-1]["page"] == 2)
    assert pump(lambda: "Page 2 of 4" in v._page_lbl.text())
    assert v._prev.isEnabled()


def test_a_slow_older_search_does_not_overwrite_a_newer_one(env):
    make, _ = env
    v = make(_FakeApi([_mod(1)]))
    pump(lambda: v._cards)
    old_gen = v._gen
    v._gen += 1
    v._on_page({"gen": old_gen, "data": {"mods": [_mod(99)], "total": 1}}, "")
    assert 99 not in v._cards


def test_load_failure_is_shown(env):
    make, _ = env

    class _Boom(_FakeApi):
        def list_mods(self, *a, **k):
            raise RuntimeError("site down")

    v = make(_Boom([]))
    assert pump(lambda: "site down" in v._status.text())


def test_install_without_a_key_asks_to_connect(env, monkeypatch):
    make, _ = env
    v = make(_FakeApi([_mod(1)]))
    pump(lambda: v._cards)
    monkeypatch.setattr(modhold_key, "load_modhold_key", lambda: "")
    v._install_clicked(_mod(1))
    assert "Connect your Modhold account" in v._status.text()
    assert v.opened == [1]


def _detail(i, files, **kw):
    d = _mod(i, **kw)
    d.update({"files": files, "can_download": True})
    return d


_MAIN = {"id": 1508, "kind": "main", "label": "Main file", "name": "Shop 1.0.zip",
         "version": "1.0", "size": 10, "sha256": "ab" * 32}


def test_single_file_install_downloads_then_hands_off_with_modhold_meta(env, monkeypatch):
    make, tmp = env
    archive = tmp / "Shop 1.0.zip"
    archive.write_bytes(b"x" * 10)
    seen = {}

    class _Dl:
        def __init__(self, api, dest):
            seen["dest"] = dest

        def download_file(self, file_id, **kw):
            seen["file_id"], seen["kw"] = file_id, kw
            kw["progress_cb"](5, 10)
            return ModholdDownloadResult(True, archive, archive.name, file_id=file_id)

    monkeypatch.setattr(bv, "ModholdDownloader", _Dl)
    api = _FakeApi([_mod(12)], details={12: _detail(12, [_MAIN])})
    v = make(api)
    pump(lambda: v._cards)
    v._install_clicked(_mod(12))
    assert pump(lambda: v.installs)
    paths, metas = v.installs[0]
    assert paths == [str(archive)]
    info = metas[str(archive)].modhold_info
    assert (info.mod_id, info.file_id, info.game) == (12, 1508, "skyrim-se")
    assert seen["file_id"] == 1508 and seen["dest"] == tmp
    assert seen["kw"]["known_sha256"] == "ab" * 32
    assert any(p[0] == "modhold:1508" and p[3] == 10 for p in v.progress)
    assert v.progress[-1][3] == -1          # progress card is released


def test_gated_mod_the_account_cannot_download_is_not_started(env, monkeypatch):
    make, _ = env
    started = []
    monkeypatch.setattr(bv, "ModholdDownloader",
                        lambda *a, **k: started.append(1))
    gated = _detail(197, [_MAIN], beta=True)
    gated["can_download"] = False
    v = make(_FakeApi([_mod(197)], details={197: gated}))
    pump(lambda: v._cards)
    v._install_clicked(_mod(197))
    assert pump(lambda: "cannot download" in v._status.text())
    assert started == [] and v.installs == []


def test_not_entitled_download_is_reported_not_installed(env, monkeypatch):
    make, _ = env

    class _Dl:
        def __init__(self, *a):
            pass

        def download_file(self, file_id, **kw):
            return ModholdDownloadResult(False, error="Pass required", file_id=file_id,
                                         not_entitled=True)

    monkeypatch.setattr(bv, "ModholdDownloader", _Dl)
    v = make(_FakeApi([_mod(12)], details={12: _detail(12, [_MAIN])}))
    pump(lambda: v._cards)
    v._install_clicked(_mod(12))
    assert pump(lambda: "Pass required" in v._status.text())
    assert v.installs == []


def test_mod_without_files_is_reported(env):
    make, _ = env
    v = make(_FakeApi([_mod(5)], details={5: _detail(5, [])}))
    pump(lambda: v._cards)
    v._install_clicked(_mod(5))
    assert pump(lambda: "no downloadable files" in v._status.text())


def test_multi_file_mod_uses_the_chooser_selection(env, monkeypatch):
    make, tmp = env
    opt = {"id": 587, "kind": "optional", "label": "Add-on", "name": "Add.zip",
           "version": "1.0", "size": 5, "sha256": "cd" * 32}
    downloaded = []

    class _Dl:
        def __init__(self, *a):
            pass

        def download_file(self, file_id, **kw):
            downloaded.append(file_id)
            p = tmp / f"f{file_id}.zip"
            p.write_bytes(b"z")
            return ModholdDownloadResult(True, p, p.name, file_id=file_id)

    monkeypatch.setattr(bv, "ModholdDownloader", _Dl)
    _auto_chooser(monkeypatch, pick={1508, 587})       # the user ticks everything
    v = make(_FakeApi([_mod(12)], details={12: _detail(12, [_MAIN, opt])}))
    pump(lambda: v._cards)
    v._install_clicked(_mod(12))
    assert pump(lambda: len(v.installs) == 2)
    assert sorted(downloaded) == [587, 1508]


def test_cancelling_the_chooser_downloads_nothing(env, monkeypatch):
    make, tmp = env
    opt = {"id": 587, "kind": "optional", "label": "Add-on", "name": "Add.zip",
           "version": "1.0", "size": 5, "sha256": "cd" * 32}
    started = []
    monkeypatch.setattr(bv, "ModholdDownloader", lambda *a, **k: started.append(1))
    _auto_chooser(monkeypatch, pick=None)
    v = make(_FakeApi([_mod(12)], details={12: _detail(12, [_MAIN, opt])}))
    pump(lambda: v._cards)
    v._install_clicked(_mod(12))
    pump(lambda: False, timeout=0.5)
    assert started == [] and v.installs == []


def test_a_mod_with_only_optional_files_offers_them_all_and_preticks_the_first(env, monkeypatch):
    """Real case (Modhold mod 1220): four files, none marked main."""
    make, tmp = env
    files = [{"id": 1905 + i, "kind": "optional", "label": f"File {i}", "name": f"f{i}.zip",
              "version": "1", "size": 5, "sha256": "ab" * 32} for i in range(4)]
    from gui_qt.modhold.modhold_file_chooser import ModholdFileChooser
    dlg = ModholdFileChooser("Lydia", files)
    assert [f["id"] for f in dlg.selected_files()] == [1905]
    downloaded = []

    class _Dl:
        def __init__(self, *a):
            pass

        def download_file(self, file_id, **kw):
            downloaded.append(file_id)
            p = tmp / f"d{file_id}.zip"
            p.write_bytes(b"z")
            return ModholdDownloadResult(True, p, p.name, file_id=file_id)

    monkeypatch.setattr(bv, "ModholdDownloader", _Dl)
    _auto_chooser(monkeypatch, pick={1905, 1906})
    v = make(_FakeApi([_mod(1220)], details={1220: _detail(1220, files)}))
    pump(lambda: v._cards)
    v._install_clicked(_mod(1220))
    assert pump(lambda: len(v.installs) == 2)
    assert sorted(downloaded) == [1905, 1906]


def test_teardown_stops_further_signals(env):
    make, _ = env
    v = make(_FakeApi([_mod(1)]))
    pump(lambda: v._cards)
    v._on_destroyed()
    assert v._closing
    v._on_page({"gen": v._gen, "data": {"mods": [_mod(2)], "total": 1}}, "")  # no crash


def test_progress_for_a_file_over_two_gigabytes_does_not_overflow(env, qapp=None):
    """Regression: the progress signal carried byte counts as C ints (Mortum Castle is 4.9 GB)."""
    make, _tmp = env
    v = make(_FakeApi([_mod(1)]))
    pump(lambda: v._cards)
    v._progress_sig.emit("modhold:1", "Mortum Castle", 4_910_442_966, 4_910_442_966)
    assert pump(lambda: v.progress and v.progress[-1][2:] == (4_910_442_966, 4_910_442_966))


"""Modhold updates: preparing one mod's update, and the main window's glue."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
from types import SimpleNamespace

import pytest

from Modhold import modhold_key
from Modhold.modhold_api import ModholdError
from Modhold.modhold_download import ModholdDownloadResult
from Modhold.modhold_meta import ModholdMeta, read_modhold_meta, write_modhold_meta
from Modhold.modhold_update import UpdateJob, prepare_update

SHA = "ab" * 32


def _file(fid, **kw):
    f = {"id": fid, "kind": "main", "label": "Main file", "version": "1.1", "size": 7,
         "sha256": SHA, "name": f"Shop {fid}.zip"}
    f.update(kw)
    return f


class _Api:
    def __init__(self, detail=None, error=None):
        self.detail, self.error = detail, error

    def get_mod(self, mod_id):
        if self.error:
            raise self.error
        return self.detail


class _Dl:
    def __init__(self, result):
        self.result, self.calls = result, []

    def download_file(self, file_id, **kw):
        self.calls.append((file_id, kw))
        return self.result


def _detail(files, can_download=True):
    return {"id": 12, "name": "Falkreath Shop", "version": "1.1", "files": files,
            "can_download": can_download, "page": "https://modholdmods.com/g/skyrim-se/mods/12/x",
            "author": {"handle": "Seth", "name": "Seth"}, "beta": False}


@pytest.fixture
def staging(tmp_path):
    s = tmp_path / "mods"
    write_modhold_meta(s / "Shop" / "meta.ini", ModholdMeta(
        mod_id=12, file_id=100, version="1.0", sha256="cc" * 32, file_label="Main file",
        file_kind="main", game="skyrim-se", loadout="3", name="Falkreath Shop"))
    return s


def _ok(tmp_path):
    p = tmp_path / "Shop 150.zip"
    p.write_bytes(b"x" * 7)
    return ModholdDownloadResult(True, p, p.name, file_id=150)


def test_prepare_update_downloads_the_newer_file_and_builds_its_meta(staging, tmp_path):
    dl = _Dl(_ok(tmp_path))
    job, reason = prepare_update(_Api(_detail([_file(150)])), dl, staging, "Shop", "skyrim-se")
    assert reason == "" and isinstance(job, UpdateJob)
    assert job.mod_name == "Shop" and job.archive.endswith("Shop 150.zip")
    assert (job.info.mod_id, job.info.file_id, job.info.version) == (12, 150, "1.1")
    assert job.info.loadout == "3" and job.info.game == "skyrim-se"
    fid, kw = dl.calls[0]
    assert fid == 150 and kw["known_sha256"] == SHA and kw["known_name"] == "Shop 150.zip"


def test_not_a_modhold_mod(tmp_path):
    (tmp_path / "mods" / "Plain").mkdir(parents=True)
    job, reason = prepare_update(_Api(), _Dl(None), tmp_path / "mods", "Plain", "skyrim-se")
    assert job is None and "not a Modhold mod" in reason


def test_api_error_is_a_reason_not_a_crash(staging):
    job, reason = prepare_update(_Api(error=ModholdError("site down")), _Dl(None),
                                 staging, "Shop", "skyrim-se")
    assert job is None and "site down" in reason


def test_installed_file_gone(staging):
    job, reason = prepare_update(_Api(_detail([_file(900, label="Other", kind="optional")])),
                                 _Dl(None), staging, "Shop", "skyrim-se")
    assert job is None and "no longer on Modhold" in reason


def test_already_up_to_date_downloads_nothing(staging):
    dl = _Dl(None)
    job, reason = prepare_update(_Api(_detail([_file(100, sha256="cc" * 32)])), dl,
                                 staging, "Shop", "skyrim-se")
    assert job is None and reason == "already up to date" and dl.calls == []


def test_gated_update_the_account_cannot_download_is_not_attempted(staging):
    dl = _Dl(None)
    job, reason = prepare_update(_Api(_detail([_file(150)], can_download=False)), dl,
                                 staging, "Shop", "skyrim-se")
    assert job is None and "cannot download" in reason and dl.calls == []


def test_download_failures_are_reasons(staging):
    fail = ModholdDownloadResult(False, error="Checksum mismatch for x")
    job, reason = prepare_update(_Api(_detail([_file(150)])), _Dl(fail), staging,
                                 "Shop", "skyrim-se")
    assert job is None and "Checksum mismatch" in reason
    gated = ModholdDownloadResult(False, error="Pass required", not_entitled=True)
    job, reason = prepare_update(_Api(_detail([_file(150)])), _Dl(gated), staging,
                                 "Shop", "skyrim-se")
    assert job is None and reason == "Pass required"          # Modhold's own message


def test_the_old_install_is_untouched_by_preparing(staging, tmp_path):
    prepare_update(_Api(_detail([_file(150)])), _Dl(_ok(tmp_path)), staging, "Shop", "skyrim-se")
    assert read_modhold_meta(staging / "Shop" / "meta.ini").file_id == 100


# ---------------------------------------------------------------- main-window glue

class _Emit:
    def __init__(self):
        self.calls, self.event = [], threading.Event()

    def emit(self, *a):
        self.calls.append(a)
        self.event.set()


def _window(tmp_path, staging, game_slug="skyrim-se"):
    from gui_qt.app_parts.app_modhold import MainWindow as Mixin

    class _W(Mixin):
        def tr(self, s):
            return s

    w = _W()
    w.notes, w.logs, w.installs = [], [], []
    game = SimpleNamespace(name="Skyrim SE", modhold_game_slug=game_slug,
                           is_configured=lambda: True)
    w._gs = SimpleNamespace(game=game, staging_dir=lambda: staging)
    w._notify = lambda text, level="info", **k: w.notes.append((level, text))
    w._append_log = w.logs.append
    w._op_log = _Emit()
    w._modhold_update_ready = _Emit()
    w._open_modhold_account = lambda: w.notes.append(("opened", "account"))
    w._install_paths = lambda paths, **kw: w.installs.append((paths, kw))
    return w


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(modhold_key, "load_modhold_key", lambda: "mhm_test")


def test_update_without_a_key_asks_to_connect(tmp_path, staging, monkeypatch):
    monkeypatch.setattr(modhold_key, "load_modhold_key", lambda: "")
    w = _window(tmp_path, staging)
    w._update_modhold_mods(["Shop"])
    assert any("Connect your Modhold account" in t for _lvl, t in w.notes)
    assert w._modhold_update_ready.calls == []


def test_update_for_an_unmapped_game_is_refused(tmp_path, staging, keyed):
    w = _window(tmp_path, staging, game_slug="")
    w._update_modhold_mods(["Shop"])
    assert any("does not list" in t for _lvl, t in w.notes)


def test_update_runs_off_thread_then_installs_over_the_old_folder(tmp_path, staging, keyed,
                                                                  monkeypatch):
    from Modhold import modhold_update
    from Utils import config_paths

    monkeypatch.setattr(config_paths, "get_download_cache_dir_for_game", lambda n: tmp_path)
    job = UpdateJob("Shop", str(tmp_path / "Shop 150.zip"),
                    ModholdMeta(mod_id=12, file_id=150, version="1.1"))
    monkeypatch.setattr(modhold_update, "prepare_update",
                        lambda api, dl, st, nm, slug, **k: (job, ""))
    w = _window(tmp_path, staging)
    w._update_modhold_mods(["Shop"])
    assert w._modhold_update_ready.event.wait(5)
    queue, skipped = w._modhold_update_ready.calls[0]
    assert queue == [job] and skipped == []

    w._on_modhold_update_ready(queue, skipped)
    (paths, kw), = w.installs
    assert paths == [job.archive]
    assert kw["preferred_names"] == {job.archive: "Shop"}          # silent replace, same slot
    assert kw["metas"][job.archive].modhold_info.file_id == 150     # Modhold keys get stamped
    assert w._modhold_updating is False


def test_skipped_mods_are_reported_and_nothing_installs(tmp_path, staging, keyed):
    w = _window(tmp_path, staging)
    w._modhold_updating = True
    w._on_modhold_update_ready([], [("Shop", "its file is no longer on Modhold")])
    assert w.installs == [] and w._modhold_updating is False
    assert any("no longer on Modhold" in t for _lvl, t in w.notes)


def test_a_second_update_while_one_runs_is_refused(tmp_path, staging, keyed):
    w = _window(tmp_path, staging)
    w._modhold_updating = True
    w._update_modhold_mods(["Shop"])
    assert any("already running" in t for _lvl, t in w.notes)
    assert w._modhold_update_ready.calls == []


def test_a_worker_crash_still_releases_the_lock(tmp_path, staging, keyed, monkeypatch):
    from Modhold import modhold_update
    from Utils import config_paths

    monkeypatch.setattr(config_paths, "get_download_cache_dir_for_game", lambda n: tmp_path)

    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(modhold_update, "prepare_update", boom)
    w = _window(tmp_path, staging)
    w._update_modhold_mods(["Shop"])
    assert w._modhold_update_ready.event.wait(5)
    queue, skipped = w._modhold_update_ready.calls[0]
    assert queue == [] and skipped and "kaput" in skipped[0][1]
    w._on_modhold_update_ready(queue, skipped)
    assert w._modhold_updating is False

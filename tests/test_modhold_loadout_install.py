"""run_loadout_install: order, caching/resume, failures, pause/cancel, reporting."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from Modhold.loadout_install import (
    LOADOUT_RECORD_NAME,
    _Job,
    build_jobs,
    meta_for,
    run_loadout_install,
    staged_file_ids,
)
from Modhold.loadout_manifest import parse_loadout, plan_install
from Modhold.modhold_download import ModholdDownloadResult
from Modhold.modhold_meta import read_modhold_meta, write_modhold_meta
from Utils.collections.collection_install import (
    CollectionInstallCallbacks,
    CollectionInstallControl,
)
from Utils.mods.modlist import read_modlist

SHA = "ab" * 32


def _file(fid, size=10):
    return {"id": fid, "name": f"f{fid}.zip", "version": "1.0", "size": size, "sha256": SHA}


def _hosted(pos, mod_id, files, name=None, **kw):
    return {"position": pos, "optional": kw.get("optional", False), "note": "",
            "mod": {"id": mod_id, "name": name or f"Mod {mod_id}", "version": "1.0",
                    "author": "Seth", "page": f"https://modholdmods.com/m/{mod_id}"},
            "locked": kw.get("locked"), "files": files, "nexus": None}


def _external(pos, name, url):
    return {"position": pos, "optional": False, "note": "needed",
            "mod": {"id": None, "name": name, "page": url}, "locked": None,
            "files": [], "nexus": url}


def _loadout(items):
    return parse_loadout({"format": "modhold-loadout", "version": 1, "id": 3,
                          "name": "Estates", "by": "Seth", "game": {"slug": "skyrim-se"},
                          "page": "https://modholdmods.com/loadouts/3", "items": items})


class _Game:
    name = "Skyrim SE"
    steam_id = ""

    def __init__(self, staging):
        self.staging, self.active = staging, None

    def set_active_profile_dir(self, p):
        self.active = p

    def load_paths(self):
        pass

    def get_effective_mod_staging_path(self):
        return self.staging


class _Downloader:
    """Fake ModholdDownloader: writes a file per download, can fail or wait."""

    def __init__(self, tmp, fail=None, not_entitled=None, gate=None):
        self.tmp, self.fail, self.not_entitled = tmp, fail or set(), not_entitled or set()
        self.gate = gate or {}          # file id -> Event that must be set before returning
        self.calls = []
        self.lock = threading.Lock()

    def download_file(self, file_id, *, known_name="", known_size=0, known_sha256="",
                      progress_cb=None, cancel=None):
        with self.lock:
            self.calls.append(file_id)
        if file_id in self.gate:
            self.gate[file_id].wait(5)
        if file_id in self.not_entitled:
            return ModholdDownloadResult(False, error="Pass required", file_id=file_id,
                                         not_entitled=True)
        if file_id in self.fail:
            return ModholdDownloadResult(False, error="Checksum mismatch", file_id=file_id)
        p = self.tmp / known_name
        p.write_bytes(b"x")
        if progress_cb:
            progress_cb(1, 1)
        return ModholdDownloadResult(True, p, p.name, file_id=file_id)


def _install_fn(staging, log=None, returns=None, raises=None):
    """Fake install_collection_archive: stages a folder with the stamped meta."""
    log = log if log is not None else []

    def fn(path, game, profile_dir, *, preferred_name="", prebuilt_meta=None, **kw):
        log.append({"name": preferred_name, "kw": kw, "meta": prebuilt_meta})
        if raises and preferred_name in raises:
            raise RuntimeError(raises[preferred_name])
        if returns is not None and preferred_name in returns:
            return returns[preferred_name]
        (staging / preferred_name).mkdir(parents=True, exist_ok=True)
        if getattr(prebuilt_meta, "modhold_info", None) is not None:
            write_modhold_meta(staging / preferred_name / "meta.ini", prebuilt_meta.modhold_info)
        return preferred_name

    fn.log = log
    return fn


class _Events:
    def __init__(self):
        self.done, self.paused, self.cancelled = [], [], []
        self.rows, self.starts, self.logs = [], [], []
        self.cb = CollectionInstallCallbacks(
            on_done=lambda *a: self.done.append(a),
            on_paused=lambda *a: self.paused.append(a),
            on_cancelled=lambda p: self.cancelled.append(p),
            on_row_installed=self.rows.append,
            on_dl_mod_start=lambda f, n, s: self.starts.append(f),
            on_log=self.logs.append,
            resolve_fomod=lambda *a: None, resolve_bain=lambda *a: None)


@pytest.fixture
def env(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    profile = tmp_path / "profiles" / "Estates"
    profile.mkdir(parents=True)
    return SimpleNamespace(tmp=tmp_path, staging=staging, profile=profile,
                           game=_Game(staging), ev=_Events(), ctl=CollectionInstallControl())


def _run(env, items, downloader=None, install_fn=None, plan=None, **kw):
    lo = _loadout(items)
    plan = plan or plan_install(lo)
    dl = downloader or _Downloader(env.tmp)
    fn = install_fn or _install_fn(env.staging)
    run_loadout_install(game=env.game, loadout=lo, plan=plan, downloader=dl,
                        profile_dir=env.profile, callbacks=env.ev.cb, control=env.ctl,
                        install_fn=fn, **kw)
    return lo, plan, dl, fn


def _modlist(env):
    return [e.name for e in read_modlist(env.profile / "modlist.txt") if not e.is_separator]


def test_installs_everything_in_loadout_order_and_reports_done(env):
    items = [_hosted(2, 20, [_file(200)]), _hosted(1, 10, [_file(100)]),
             _hosted(3, 30, [_file(300)])]
    _lo, _plan, dl, fn = _run(env, items)
    assert env.ev.done == [(3, 0, 3, "Estates")]
    assert [c["name"] for c in fn.log] == ["Mod 10", "Mod 20", "Mod 30"]     # position order
    assert _modlist(env) == ["Mod 10", "Mod 20", "Mod 30"]
    assert sorted(dl.calls) == [100, 200, 300]
    assert sorted(env.ev.rows) == [1, 2, 3]                   # one row per job, small unique keys
    assert env.ctl.failed_mods == []
    assert env.game.active == env.profile


def test_install_order_holds_even_when_a_later_download_finishes_first(env):
    gate = {100: threading.Event()}
    dl = _Downloader(env.tmp, gate=gate)
    items = [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)])]

    def release_first_after_second_downloaded():
        for _ in range(200):
            if 200 in dl.calls:
                break
            threading.Event().wait(0.01)
        gate[100].set()

    threading.Thread(target=release_first_after_second_downloaded, daemon=True).start()
    _run(env, items, downloader=dl)
    assert _modlist(env) == ["Mod 10", "Mod 20"]


def test_external_items_are_listed_not_downloaded_and_written_to_the_profile(env):
    items = [_hosted(1, 10, [_file(100)]),
             _external(2, "SkyUI", "https://www.nexusmods.com/skyrimspecialedition/mods/12604")]
    _run(env, items)
    assert env.ctl.unresolved_offsite == [
        ("SkyUI", "https://www.nexusmods.com/skyrimspecialedition/mods/12604")]
    record = json.loads((env.profile / LOADOUT_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["id"] == 3 and record["external"][0]["name"] == "SkyUI"
    assert record["external"][0]["note"] == "needed"
    assert env.ev.done == [(1, 0, 1, "Estates")]


def test_a_failed_download_is_reported_and_the_rest_still_install(env):
    items = [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)]),
             _hosted(3, 30, [_file(300)])]
    _run(env, items, downloader=_Downloader(env.tmp, fail={200}))
    assert env.ev.done == [(2, 1, 3, "Estates")]
    assert _modlist(env) == ["Mod 10", "Mod 30"]
    (failed,) = env.ctl.failed_mods
    assert failed["name"] == "Mod 20" and failed["file_id"] == 200
    assert failed["retryable"] and "Checksum mismatch" in failed["reason"]
    assert (env.profile / "install_report.json").is_file()


class _Refusing(_Downloader):
    """Refuses chosen files the way Modhold does, with its own message."""

    def __init__(self, tmp, refusals):
        super().__init__(tmp)
        self.refusals = refusals

    def download_file(self, file_id, **kw):
        if file_id in self.refusals:
            msg, flags = self.refusals[file_id]
            return ModholdDownloadResult(False, error=msg, file_id=file_id, **flags)
        return super().download_file(file_id, **kw)


def test_refused_files_show_modholds_message_and_only_the_changeable_ones_can_be_retried(env):
    items = [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)], locked="beta"),
             _hosted(3, 30, [_file(300)]), _hosted(4, 40, [_file(400)]),
             _hosted(5, 50, [_file(500)])]
    dl = _Refusing(env.tmp, {
        200: ("Supporter beta: supporters only until Dec 1.", {"not_entitled": True}),
        300: ("Still being scanned.", {"not_ready": True}),
        400: ("A Modhold key is needed.", {"not_connected": True}),
        500: ("Replaced by 1.2.", {"gone": True}),
    })
    _run(env, items, downloader=dl)
    by = {f["name"]: f for f in env.ctl.failed_mods}
    assert env.ev.done == [(1, 4, 5, "Estates")]
    assert by["Mod 20"]["reason"] == "Supporter beta: supporters only until Dec 1."
    assert not by["Mod 20"]["retryable"]                          # needs access, not a retry
    assert by["Mod 30"]["reason"] == "Still being scanned. Try again in a few minutes."
    assert by["Mod 30"]["retryable"]                              # the scan will finish
    assert by["Mod 40"]["retryable"] and "key is needed" in by["Mod 40"]["reason"]
    assert by["Mod 50"]["reason"] == "Replaced by 1.2." and not by["Mod 50"]["retryable"]


def test_an_install_that_stages_nothing_or_raises_is_a_failure_not_a_crash(env):
    items = [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)]),
             _hosted(3, 30, [_file(300)])]
    fn = _install_fn(env.staging, returns={"Mod 10": None}, raises={"Mod 20": "disk full"})
    _run(env, items, install_fn=fn)
    assert env.ev.done == [(1, 2, 3, "Estates")]
    by = {f["name"]: f for f in env.ctl.failed_mods}
    assert by["Mod 10"]["status"] == "stage_empty" and not by["Mod 10"]["retryable"]
    assert by["Mod 20"]["status"] == "install_failed" and "disk full" in by["Mod 20"]["reason"]


def test_a_downloader_exception_does_not_stop_the_batch(env):
    class _Boom(_Downloader):
        def download_file(self, file_id, **kw):
            if file_id == 100:
                raise RuntimeError("socket exploded")
            return super().download_file(file_id, **kw)

    items = [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)])]
    _run(env, items, downloader=_Boom(env.tmp))
    assert env.ev.done == [(1, 1, 2, "Estates")]
    assert "socket exploded" in env.ctl.failed_mods[0]["reason"]


def test_continue_keeps_staged_files_and_does_not_fetch_them_again(env):
    write_modhold_meta(env.staging / "Mod 10" / "meta.ini",
                       meta_for(build_jobs(plan_install(_loadout(
                           [_hosted(1, 10, [_file(100)])])))[0], _loadout([])))
    items = [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)])]
    _lo, _plan, dl, fn = _run(env, items, continue_existing=True)
    assert dl.calls == [200]
    assert [c["name"] for c in fn.log] == ["Mod 20"]
    assert env.ev.done == [(2, 0, 2, "Estates")]
    assert _modlist(env) == ["Mod 10", "Mod 20"]                # the kept one keeps its slot
    assert 1 in env.ev.rows                                      # the kept mod still gets its row


def test_without_continue_a_staged_file_is_installed_again(env):
    write_modhold_meta(env.staging / "Mod 10" / "meta.ini",
                       meta_for(build_jobs(plan_install(_loadout(
                           [_hosted(1, 10, [_file(100)])])))[0], _loadout([])))
    _lo, _plan, dl, _fn = _run(env, [_hosted(1, 10, [_file(100)])])
    assert dl.calls == [100]


def test_cancel_reports_cancelled_and_never_done(env):
    env.ctl.cancel.set()
    env.ctl.stop.set()
    _run(env, [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)])])
    assert env.ev.cancelled == [env.profile]
    assert env.ev.done == [] and env.ev.paused == []


def test_pause_after_the_first_install_keeps_what_is_done(env):
    items = [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)])]
    fn = _install_fn(env.staging)
    real = fn

    def pausing(path, game, profile_dir, **kw):
        out = real(path, game, profile_dir, **kw)
        env.ctl.pause.set()
        env.ctl.stop.set()
        return out

    _run(env, items, install_fn=pausing)
    assert env.ev.paused == [(1, "Estates")] and env.ev.done == []
    assert _modlist(env) == ["Mod 10"]                          # a coherent, resumable profile


def test_fomod_and_bain_questions_are_routed_to_the_app_one_at_a_time(env):
    _lo, _plan, _dl, fn = _run(env, [_hosted(1, 10, [_file(100)])])
    kw = fn.log[0]["kw"]
    assert kw["fomod_no_choice_action"] == "wizard"
    assert kw["resolve_fomod"] is env.ev.cb.resolve_fomod
    assert kw["resolve_bain"] is env.ev.cb.resolve_bain
    assert kw["defer_interactive_fomod"] is False and kw["concurrent_workers"] == 1
    assert kw["skip_index_update"] is True


def test_installed_files_are_stamped_with_their_modhold_record_and_loadout(env):
    _lo, _plan, _dl, fn = _run(env, [_hosted(1, 10, [_file(100)], locked="beta")])
    info = fn.log[0]["meta"].modhold_info
    assert (info.mod_id, info.file_id, info.loadout, info.game) == (10, 100, "3", "skyrim-se")
    assert info.locked == "beta" and info.sha256 == SHA
    saved = read_modhold_meta(env.staging / "Mod 10" / "meta.ini")
    assert saved.file_id == 100 and saved.loadout == "3"


def test_nothing_to_install_still_finishes_cleanly(env):
    _run(env, [_external(1, "SkyUI", "https://n/1")])
    assert env.ev.done == [(0, 0, 0, "Estates")]
    assert env.ctl.unresolved_offsite == [("SkyUI", "https://n/1")]


def test_two_items_with_the_same_name_get_separate_folders(env):
    lo = _loadout([_hosted(1, 10, [_file(100)], name="Same"),
                   _hosted(2, 20, [_file(200)], name="Same")])
    assert [j.name for j in build_jobs(plan_install(lo))] == ["Same", "Same (2)"]


def test_a_multi_file_item_is_split_by_version_and_names_are_made_safe(env):
    f1, f2 = _file(100), _file(101)
    f2["version"] = "2.0"
    lo = _loadout([_hosted(1, 10, [f1, f2], name='Bad: name/with*chars?')])
    names = [j.name for j in build_jobs(plan_install(lo))]
    assert names == ["Bad_ name_with_chars_ - 1.0", "Bad_ name_with_chars_ - 2.0"]
    assert [j.rank for j in build_jobs(plan_install(lo))] == [0, 1]


def test_staged_file_ids_finds_modhold_mods_only(env):
    (env.staging / "Plain").mkdir()
    (env.staging / "Plain" / "meta.ini").write_text("[General]\nmodid=5\n", encoding="utf-8")
    write_modhold_meta(env.staging / "Shop" / "meta.ini",
                       meta_for(_Job(0, plan_install(_loadout([_hosted(1, 10, [_file(100)])]))
                                     .installable[0],
                                     plan_install(_loadout([_hosted(1, 10, [_file(100)])]))
                                     .installable[0].files[0], "Shop"), _loadout([])))
    assert staged_file_ids(env.staging) == {100: "Shop"}
    assert staged_file_ids(env.tmp / "missing") == {}


# ---------------------------------------------------------------- real installer

class _RealGame(_Game):
    """Just enough game for the genuine install_collection_archive."""
    supports_bain = True
    plugin_extensions = None


def _zip(tmp, name, files):
    import zipfile
    p = tmp / name
    with zipfile.ZipFile(p, "w") as zf:
        for rel, text in files.items():
            zf.writestr(rel, text)
    return p


class _ZipDownloader:
    """Serves real zip archives, so the real installer has something to stage."""

    def __init__(self, tmp, archives):
        self.tmp, self.archives, self.calls = tmp, archives, []

    def download_file(self, file_id, *, known_name="", **kw):
        self.calls.append(file_id)
        p = _zip(self.tmp, known_name, self.archives[file_id])
        return ModholdDownloadResult(True, p, p.name, file_id=file_id)


def test_with_the_real_installer_mods_are_staged_stamped_and_ordered(env, monkeypatch):
    """No fake install function: the genuine one stages real zips, never touches the
    network, and leaves the Modhold keys (and no Nexus identity) in each meta.ini."""
    import socket

    def no_network(*_a, **_k):
        raise AssertionError("the install must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    env.game = _RealGame(env.staging)
    # The archive names look like Nexus ones ("Name-53000-1-6-2"): they must NOT be
    # taken for a Nexus identity.
    archives = {
        100: {"SKSE/Plugins/versionlib.bin": "lib"},
        200: {"Textures/a.dds": "tex", "Meshes/a.nif": "mesh"},
    }
    items = [_hosted(1, 10, [{**_file(100), "name": "Address Library-32444-13-1.zip"}],
                     name="Address Library"),
             _hosted(2, 20, [_file(200)], name="Pretty Textures")]
    lo = _loadout(items)
    run_loadout_install(
        game=env.game, loadout=lo, plan=plan_install(lo),
        downloader=_ZipDownloader(env.tmp, archives), profile_dir=env.profile,
        callbacks=env.ev.cb, control=env.ctl)
    assert env.ev.done == [(2, 0, 2, "Estates")], env.ev.logs
    assert _modlist(env) == ["Address Library", "Pretty Textures"]
    assert (env.staging / "Address Library" / "SKSE" / "Plugins" / "versionlib.bin").is_file()
    assert (env.staging / "Pretty Textures" / "Textures" / "a.dds").is_file()
    meta = read_modhold_meta(env.staging / "Address Library" / "meta.ini")
    assert (meta.mod_id, meta.file_id, meta.loadout, meta.game) == (10, 100, "3", "skyrim-se")
    ini = (env.staging / "Address Library" / "meta.ini").read_text(encoding="utf-8")
    assert "modid = 32444" not in ini and "modid=32444" not in ini     # no Nexus identity guessed
    assert env.ctl.failed_mods == []


# ------------------------------------------------------------- items fetched from Nexus

NEXUS = "https://www.nexusmods.com/skyrimspecialedition/mods/{}"


def _nf(file_id, name="Thing-1-0.7z", **kw):
    base = dict(file_id=file_id, file_name=name, name="Main", version="1.0",
                size_in_bytes=10, size_kb=0, category_name="MAIN", is_primary=True,
                uploaded_timestamp=5)
    base.update(kw)
    return SimpleNamespace(**base)


class _NexusDownloader:
    """Fake NexusDownloader: writes the file, or fails with a status code."""

    def __init__(self, tmp, fail=None):
        self.tmp, self.fail, self.calls = tmp, fail or {}, []

    def download_file(self, *, game_domain, mod_id, file_id, known_file_name="",
                      expected_size_bytes=0, progress_cb=None, cancel=None, **kw):
        self.calls.append((game_domain, mod_id, file_id, known_file_name))
        if mod_id in self.fail:
            err, status, removed = self.fail[mod_id]
            return SimpleNamespace(success=False, file_path=None, error=err,
                                   status_code=status, mod_removed=removed, from_cache=False)
        p = self.tmp / known_file_name
        p.write_bytes(b"n")
        return SimpleNamespace(success=True, file_path=p, error="", status_code=0,
                               mod_removed=False, from_cache=False)


class _NexusApi:
    def get_mod(self, domain, mod_id):
        return SimpleNamespace(name=f"Nexus Mod {mod_id}", version="1.0", author="Auth",
                               uploaded_by="Auth", summary="s", category_id=1,
                               category_name="Utilities")


def _picks(lo, files):
    """NexusPick per Nexus item, from ``{nexus mod id: file}`` (None = nothing chosen)."""
    from Modhold.nexus_links import NexusPick
    out = {}
    for item in lo.items:
        if item.nexus_ref is not None:
            f = files.get(item.nexus_ref.mod_id)
            out[item.order_index] = NexusPick(
                item.order_index, item.name, item.nexus_ref, f,
                reason="" if f else "it has no main file")
    return out


def _run_nexus(env, items, files, nexus_dl=None, install_fn=None, with_downloader=True, **kw):
    lo = _loadout(items)
    nexus_idx = {i.order_index for i in lo.items if i.nexus_ref}
    plan = plan_install(lo, nexus_items=nexus_idx)
    fn = install_fn or _install_fn(env.staging)
    ndl = nexus_dl or _NexusDownloader(env.tmp)
    run_loadout_install(
        game=env.game, loadout=lo, plan=plan, downloader=_Downloader(env.tmp),
        profile_dir=env.profile, callbacks=env.ev.cb, control=env.ctl, install_fn=fn,
        nexus_picks=_picks(lo, files), nexus_api=_NexusApi(),
        nexus_downloader=ndl if with_downloader else None, **kw)
    return lo, plan, ndl, fn


def test_nexus_and_modhold_items_install_together_in_loadout_order(env):
    items = [_external(1, "SkyUI", NEXUS.format(12604)), _hosted(2, 10, [_file(100)]),
             _external(3, "SKSE", NEXUS.format(30379)),
             _external(4, "High Poly Head", "https://vectorplexis.com/files/file/283/")]
    lo, plan, ndl, fn = _run_nexus(env, items, {12604: _nf(555, "SkyUI-5.7z"),
                                                30379: _nf(777, "SKSE-2-3-1.7z")})
    assert [c["name"] for c in fn.log] == ["SkyUI", "Mod 10", "SKSE"]
    assert _modlist(env) == ["SkyUI", "Mod 10", "SKSE"]
    assert env.ev.done == [(3, 0, 3, "Estates")]
    assert ndl.calls == [("skyrimspecialedition", 12604, 555, "SkyUI-5.7z"),
                         ("skyrimspecialedition", 30379, 777, "SKSE-2-3-1.7z")]
    assert env.ctl.unresolved_offsite == [("High Poly Head", "https://vectorplexis.com/files/file/283/")]


def test_nexus_items_are_stamped_with_their_real_nexus_identity_not_a_modhold_one(env):
    items = [_external(1, "SkyUI", NEXUS.format(12604)), _hosted(2, 10, [_file(100)])]
    _lo, _plan, _ndl, fn = _run_nexus(env, items, {12604: _nf(555, "SkyUI-5.7z")})
    nexus_meta = fn.log[0]["meta"]
    assert (nexus_meta.mod_id, nexus_meta.file_id, nexus_meta.game_domain) == (
        12604, 555, "skyrimspecialedition")
    assert nexus_meta.nexus_name == "Nexus Mod 12604" and nexus_meta.author == "Auth"
    assert getattr(nexus_meta, "modhold_info", None) is None
    assert fn.log[1]["meta"].modhold_info.mod_id == 10              # the Modhold one is unchanged


def test_overlay_rows_never_collide_when_both_sites_use_the_same_file_id(env):
    items = [_external(1, "SkyUI", NEXUS.format(1)), _hosted(2, 10, [_file(100)])]
    _run_nexus(env, items, {1: _nf(100, "SkyUI.7z")})                # Nexus file 100 == Modhold file 100
    assert sorted(env.ev.rows) == [1, 2]
    assert env.ev.done == [(2, 0, 2, "Estates")]


def test_row_keys_always_fit_a_c_int_whatever_the_file_ids_are(env):
    """Regression: a Nexus key of 10_000_000_000 + file id overflowed the app's Signal(int)
    (OverflowError for every Nexus item, inside the install loop)."""
    from Modhold.nexus_links import NexusPick, NexusRef
    lo = _loadout([_external(1, "Huge Nexus", NEXUS.format(7)), _hosted(2, 10, [_file(9_999_999_999)]),
                   _hosted(3, 11, [_file(5_000_000_000)])])
    nexus_idx = {i.order_index for i in lo.items if i.nexus_ref}
    plan = plan_install(lo, nexus_items=nexus_idx)
    pick = NexusPick(0, "Huge Nexus", NexusRef("skyrimspecialedition", 7), _nf(4_000_000_000, "x.7z"))
    jobs = build_jobs(plan, {0: pick})
    keys = [j.key for j in jobs]
    assert len(set(keys)) == len(keys) == 3
    assert all(0 < k < 2 ** 31 for k in keys)
    # and through a REAL Qt signal typed like the app's _col_row
    from PySide6.QtCore import QObject, Signal

    class _Probe(QObject):
        row = Signal(int)

    got = []
    probe = _Probe()
    probe.row.connect(got.append)
    for k in keys:
        probe.row.emit(int(k))
    assert got == keys


def test_every_key_the_installer_hands_the_callbacks_fits_a_c_int(env):
    seen = []
    cb = env.ev.cb
    cb.on_row_installed = seen.append
    cb.on_dl_mod_start = lambda k, n, s: seen.append(k)
    cb.on_extract_add = lambda k, n: seen.append(k)
    items = [_external(1, "SkyUI", NEXUS.format(12604)), _hosted(2, 10, [_file(9_999_999_999)])]
    _run_nexus(env, items, {12604: _nf(4_000_000_000, "SkyUI.7z")})
    assert seen and all(0 < k < 2 ** 31 for k in seen)


def test_a_nexus_refusal_is_reported_with_its_reason_and_its_link_goes_on_the_list(env):
    items = [_external(1, "Premium Only", NEXUS.format(1)), _external(2, "Gone", NEXUS.format(2)),
             _external(3, "Fine", NEXUS.format(3))]
    ndl = _NexusDownloader(env.tmp, fail={1: ("403 Forbidden", 403, False),
                                          2: ("mod removed", 404, True)})
    _run_nexus(env, items, {1: _nf(11, "a.7z"), 2: _nf(22, "b.7z"), 3: _nf(33, "c.7z")}, nexus_dl=ndl)
    by = {f["name"]: f for f in env.ctl.failed_mods}
    assert env.ev.done == [(1, 2, 3, "Estates")]
    assert "Nexus: 403 Forbidden" in by["Premium Only"]["reason"] and not by["Premium Only"]["retryable"]
    assert "mod removed" in by["Gone"]["reason"] and not by["Gone"]["retryable"]
    assert ("Premium Only", NEXUS.format(1)) in env.ctl.unresolved_offsite
    assert ("Gone", NEXUS.format(2)) in env.ctl.unresolved_offsite
    assert all(n != "Fine" for n, _u in env.ctl.unresolved_offsite)


def test_a_network_error_from_nexus_can_be_retried(env):
    ndl = _NexusDownloader(env.tmp, fail={1: ("connection reset", 0, False)})
    _run_nexus(env, [_external(1, "Flaky", NEXUS.format(1))], {1: _nf(11, "a.7z")}, nexus_dl=ndl)
    (failed,) = env.ctl.failed_mods
    assert failed["retryable"] and "connection reset" in failed["reason"]


def test_without_a_nexus_connection_the_items_fail_loudly_and_keep_their_links(env):
    items = [_external(1, "SkyUI", NEXUS.format(1)), _hosted(2, 10, [_file(100)])]
    _run_nexus(env, items, {1: _nf(11, "a.7z")}, with_downloader=False)
    assert env.ev.done == [(1, 1, 2, "Estates")]
    assert "not connected" in env.ctl.failed_mods[0]["reason"]
    assert ("SkyUI", NEXUS.format(1)) in env.ctl.unresolved_offsite


def test_an_item_with_no_usable_choice_is_reported_and_never_downloaded(env):
    ndl = _NexusDownloader(env.tmp)
    _run_nexus(env, [_external(1, "Optional Only", NEXUS.format(1))], {1: None}, nexus_dl=ndl)
    assert ndl.calls == []
    assert "no main file" in env.ctl.failed_mods[0]["reason"]


def test_continue_does_not_fetch_a_nexus_mod_that_is_already_staged(env):
    from Nexus.nexus_meta import NexusModMeta, write_meta
    write_meta(env.staging / "SkyUI" / "meta.ini",
               NexusModMeta(game_domain="skyrimspecialedition", mod_id=12604, file_id=555))
    items = [_external(1, "SkyUI", NEXUS.format(12604)), _external(2, "SKSE", NEXUS.format(30379))]
    _lo, _plan, ndl, fn = _run_nexus(env, items, {12604: _nf(555, "SkyUI.7z"),
                                                  30379: _nf(777, "SKSE.7z")},
                                     continue_existing=True)
    assert [c[1] for c in ndl.calls] == [30379]
    assert [c["name"] for c in fn.log] == ["SKSE"]
    assert env.ev.done == [(2, 0, 2, "Estates")] and _modlist(env) == ["SkyUI", "SKSE"]


def test_a_different_file_of_the_same_nexus_mod_is_not_mistaken_for_the_staged_one(env):
    from Nexus.nexus_meta import NexusModMeta, write_meta
    write_meta(env.staging / "SkyUI" / "meta.ini",
               NexusModMeta(game_domain="skyrimspecialedition", mod_id=12604, file_id=555))
    _lo, _plan, ndl, _fn = _run_nexus(env, [_external(1, "SkyUI", NEXUS.format(12604))],
                                      {12604: _nf(999, "SkyUI-new.7z")}, continue_existing=True)
    assert [c[2] for c in ndl.calls] == [999]


def test_the_profile_record_names_the_nexus_files_that_were_chosen(env):
    items = [_external(1, "SkyUI", NEXUS.format(12604)),
             _external(2, "High Poly Head", "https://vectorplexis.com/x")]
    _run_nexus(env, items, {12604: _nf(555, "SkyUI-5.7z")})
    record = json.loads((env.profile / LOADOUT_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["nexus"] == [{"name": "SkyUI", "url": NEXUS.format(12604), "file": "SkyUI-5.7z"}]
    assert [e["name"] for e in record["external"]] == ["High Poly Head"]


def test_with_the_real_installer_a_nexus_mod_keeps_its_nexus_identity(env, monkeypatch):
    import socket

    from Nexus.nexus_meta import read_meta

    monkeypatch.setattr(socket.socket, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    env.game = _RealGame(env.staging)

    class _ZipNexus(_NexusDownloader):
        def download_file(self, *, game_domain, mod_id, file_id, known_file_name="", **kw):
            self.calls.append((game_domain, mod_id, file_id, known_file_name))
            p = _zip(self.tmp, known_file_name, {"SKSE/Plugins/x.dll": "dll"})
            return SimpleNamespace(success=True, file_path=p, error="", status_code=0,
                                   mod_removed=False, from_cache=False)

    lo = _loadout([_external(1, "My Plugin", NEXUS.format(5000))])
    plan = plan_install(lo, nexus_items={0})
    run_loadout_install(game=env.game, loadout=lo, plan=plan,
                        downloader=_Downloader(env.tmp), profile_dir=env.profile,
                        callbacks=env.ev.cb, control=env.ctl,
                        nexus_picks=_picks(lo, {5000: _nf(8, "Plugin-5000-1-0.zip")}),
                        nexus_api=_NexusApi(), nexus_downloader=_ZipNexus(env.tmp))
    assert env.ev.done == [(1, 0, 1, "Estates")], env.ev.logs
    assert (env.staging / "My Plugin" / "SKSE" / "Plugins" / "x.dll").is_file()
    meta = read_meta(env.staging / "My Plugin" / "meta.ini")
    assert (meta.mod_id, meta.file_id) == (5000, 8)
    assert not (env.staging / "My Plugin" / "meta.ini").read_text(encoding="utf-8").lower().count("modholdmodid")


# ------------------------------------------------------------- the mod index stays current

def _index(profile):
    """The mod index without its built-in [Overwrite] entry."""
    from Utils.filemap import read_mod_index
    idx = read_mod_index(profile / "modindex.bin") or {}
    return {k: v for k, v in idx.items() if k != "[Overwrite]"}


def test_a_continue_run_puts_the_new_mod_in_the_index_so_its_plugin_lists(env, monkeypatch):
    """The reported bug: High Poly Head was installed on a Continue run into a profile that
    already had an index, so it was in modlist.txt but missing from the file map (no
    plugin in the panel, nothing to deploy) until Refresh."""
    from Utils.filemap import rebuild_mod_index

    class _Game2(_Game):
        supports_bain = True
        plugin_extensions = None

    env.game = _Game2(env.staging)
    # First run: one mod installed, index built (as the app does when it first loads a profile).
    first = _loadout([_hosted(1, 10, [_file(100)])])
    run_loadout_install(game=env.game, loadout=first, plan=plan_install(first),
                        downloader=_ZipDownloader(env.tmp, {100: {"Textures/a.dds": "t"}}),
                        profile_dir=env.profile, callbacks=env.ev.cb, control=env.ctl)
    assert set(_index(env.profile)) == {"Mod 10"}
    rebuild_mod_index(env.profile / "modindex.bin", env.staging)         # the stale index to come
    # Second run (Continue): the author fixed a mod; it is installed into the same profile.
    second = _loadout([_hosted(1, 10, [_file(100)]),
                       _hosted(2, 20, [_file(200)], name="High Poly Head")])
    env.ev, env.ctl = _Events(), CollectionInstallControl()
    run_loadout_install(
        game=env.game, loadout=second, plan=plan_install(second),
        downloader=_ZipDownloader(env.tmp, {100: {"Textures/a.dds": "t"},
                                            200: {"High Poly Head.esm": "esm", "Head.bsa": "b"}}),
        profile_dir=env.profile, callbacks=env.ev.cb, control=env.ctl, continue_existing=True)
    index = _index(env.profile)
    assert set(index) == {"Mod 10", "High Poly Head"}, env.ev.logs
    normal, _root = index["High Poly Head"]
    assert any(name.lower().endswith("high poly head.esm") for name in normal)
    assert _modlist(env) == ["Mod 10", "High Poly Head"]


def test_the_index_is_rebuilt_once_after_the_installs_not_per_mod(env, monkeypatch):
    import Utils.filemap as fm
    calls = []
    monkeypatch.setattr(fm, "rebuild_mod_index", lambda *a, **k: calls.append((a, k)))
    _run(env, [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)]), _hosted(3, 30, [_file(300)])])
    assert len(calls) == 1
    (index_path, staging_root), kw = calls[0]
    assert index_path.name == "modindex.bin" and staging_root == env.staging
    assert index_path.parent == env.profile                  # fake game has no filemap path: profile dir
    assert kw["normalize_folder_case"] is True
    assert env.ev.done == [(3, 0, 3, "Estates")]


def test_the_index_follows_the_effective_filemap_folder(env, monkeypatch):
    import Utils.filemap as fm
    calls = []
    monkeypatch.setattr(fm, "rebuild_mod_index", lambda *a, **k: calls.append(a))
    elsewhere = env.tmp / "shared"
    elsewhere.mkdir()
    env.game.get_effective_filemap_path = lambda: elsewhere / "filemap.txt"
    _run(env, [_hosted(1, 10, [_file(100)])])
    assert calls[0][0] == elsewhere / "modindex.bin"


def test_no_rebuild_when_nothing_new_was_installed(env, monkeypatch):
    import Utils.filemap as fm
    calls = []
    monkeypatch.setattr(fm, "rebuild_mod_index", lambda *a, **k: calls.append(a))
    _run(env, [_hosted(1, 10, [_file(100)])], downloader=_Downloader(env.tmp, fail={100}))
    assert calls == []
    # and a Continue that finds everything already staged has nothing new to index either
    write_modhold_meta(env.staging / "Mod 10" / "meta.ini", meta_for(build_jobs(plan_install(
        _loadout([_hosted(1, 10, [_file(100)])])))[0], _loadout([])))
    env.ev, env.ctl = _Events(), CollectionInstallControl()
    _run(env, [_hosted(1, 10, [_file(100)])], continue_existing=True)
    assert calls == []


def test_a_paused_install_is_indexed_but_a_cancelled_one_is_not(env, monkeypatch):
    import Utils.filemap as fm
    calls = []
    monkeypatch.setattr(fm, "rebuild_mod_index", lambda *a, **k: calls.append(a))
    fn = _install_fn(env.staging)

    def pausing(path, game, profile_dir, **kw):
        out = fn(path, game, profile_dir, **kw)
        env.ctl.pause.set()
        env.ctl.stop.set()
        return out

    _run(env, [_hosted(1, 10, [_file(100)]), _hosted(2, 20, [_file(200)])], install_fn=pausing)
    assert env.ev.paused and len(calls) == 1
    env2 = SimpleNamespace(**{**env.__dict__, "ctl": CollectionInstallControl(), "ev": _Events()})
    env2.ctl.cancel.set()
    env2.ctl.stop.set()
    _run(env2, [_hosted(1, 10, [_file(100)])])
    assert env2.ev.cancelled and len(calls) == 1             # still just the paused run's rebuild


def test_a_failing_index_rebuild_never_breaks_the_install(env, monkeypatch):
    import Utils.filemap as fm
    monkeypatch.setattr(fm, "rebuild_mod_index",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    _run(env, [_hosted(1, 10, [_file(100)])])
    assert env.ev.done == [(1, 0, 1, "Estates")]
    assert any("Mod index rebuild skipped: disk full" in m for m in env.ev.logs)

"""Checking an installed Wabbajack profile against its modlist.

Real case (2026-11-07): LoreRim reported a clean install over 3975 bad files.
A Repair only rebuilds missing or wrong-size files, so a right-size file with
the wrong contents needs this check to find it and remove it first."""
from __future__ import annotations

import os
import time

from test_wabbajack_install_pipeline import FakeGame, _build, _fake_http, _run

from Utils.wabbajack import wabbajack_install as wi
from Utils.wabbajack import wabbajack_verify as wv


def _installed(tmp_path, monkeypatch):
    wj, modlist, src = _build(tmp_path)
    report, staging, profile, _ = _run(tmp_path, wj, modlist, monkeypatch,
                                       downloader=_fake_http(src))
    assert report.ok
    return wj, modlist, src, staging, profile


def _damage(path, data: bytes) -> None:
    """Write ``data`` as the install would have: an hour back, before the
    install finished. A file written later is the game's (see below)."""
    path.write_bytes(data)
    old = time.time() - 3600
    os.utime(path, (old, old))


def _check(wj, modlist, staging, profile, **kw):
    return wv.verify_install(modlist, wj, FakeGame(staging), [(profile, "Main")], staging,
                             **kw)


def test_a_clean_install_passes(tmp_path, monkeypatch):
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    r = _check(wj, modlist, staging, profile)
    assert r.ok and r.problems == []
    assert r.checked == 3            # ModA.esp, a.dds, ModB/config.json
    assert "All 3 mod files match" in wv.verify_summary(r)


def test_missing_wrong_size_and_changed_files_are_reported_per_mod(tmp_path, monkeypatch):
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    _damage(staging / "ModA" / "ModA.esp", b"ESP BYTEZ")              # same size
    (staging / "ModA" / "textures" / "a.dds").unlink()
    _damage(staging / "ModB" / "config.json", b"{}")

    r = _check(wj, modlist, staging, profile)
    got = {p.path: p.reason for p in r.problems}
    assert got == {"ModA/ModA.esp": wv.CHANGED,
                   "ModA/textures/a.dds": wv.MISSING,
                   # a settings file: most likely changed on purpose
                   "ModB/config.json": wv.SETTINGS}
    assert sorted(r.by_mod()) == ["ModA", "ModB"]
    assert {p.path for p in r.repairable} == {"ModA/ModA.esp", "ModA/textures/a.dds"}
    assert not r.ok


def test_sizes_only_misses_same_size_changes(tmp_path, monkeypatch):
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    (staging / "ModA" / "ModA.esp").write_bytes(b"ESP BYTEZ")
    assert _check(wj, modlist, staging, profile, hash_files=False).ok


def test_meta_ini_is_not_held_to_the_modlist(tmp_path, monkeypatch):
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    # the install wrote its own [Wabbajack] section into each meta.ini
    assert "Test List" in (staging / "ModA" / "meta.ini").read_text()
    assert _check(wj, modlist, staging, profile).ok


def test_paths_match_case_insensitively(tmp_path, monkeypatch):
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    (staging / "ModA" / "textures").rename(staging / "ModA" / "Textures")
    assert _check(wj, modlist, staging, profile).ok


def test_a_file_swapped_for_another_of_the_modlists_is_not_damage(tmp_path, monkeypatch):
    # LoreRim's main-menu randomizer copies a random theme over the menu
    # files at each launch, through the deploy links into the mods folder.
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    (staging / "ModA" / "textures" / "a.dds").write_bytes(b"ESP BYTES")
    r = _check(wj, modlist, staging, profile)
    (p,) = r.problems
    assert p.reason == wv.SWAPPED and p.note == "mods/ModA/ModA.esp"
    assert r.ok and r.repairable == []
    assert "changed since the install" in wv.verify_summary(r)
    assert wv.remove_bad_files(r, staging) == 0


def test_profiles_sharing_a_mods_folder_check_each_file_once(tmp_path, monkeypatch):
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    twice = wv.verify_install(modlist, wj, FakeGame(staging),
                              [(profile, "Main"), (profile, "Main")], staging)
    assert twice.checked == 3


def test_removing_bad_files_lets_a_repair_rebuild_them(tmp_path, monkeypatch):
    wj, modlist, src, staging, profile = _installed(tmp_path, monkeypatch)
    _damage(staging / "ModA" / "ModA.esp", b"ESP BYTEZ")        # Repair alone keeps this
    r = _check(wj, modlist, staging, profile)
    assert wv.remove_bad_files(r, staging) == 1
    assert not (staging / "ModA" / "ModA.esp").exists()

    report, *_ = _run(tmp_path, wj, modlist, monkeypatch,
                      downloader=_fake_http(src), resume=True)
    assert report.ok
    assert (staging / "ModA" / "ModA.esp").read_bytes() == b"ESP BYTES"
    assert _check(wj, modlist, staging, profile).ok


def test_remove_bad_files_never_leaves_the_staging_folder(tmp_path):
    staging = tmp_path / "mods"
    (staging / "ModA").mkdir(parents=True)
    outside = tmp_path / "precious.esp"
    outside.write_text("keep")
    (staging / "ModA" / "link.esp").symlink_to(outside)
    r = wv.VerifyReport(problems=[wv.VerifyProblem("ModA/link.esp", wv.CHANGED)])
    assert wv.remove_bad_files(r, staging) == 0
    assert outside.read_text() == "keep"


def test_without_the_recorded_mo2_profile_the_profiles_modlist_decides(tmp_path, monkeypatch):
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    to = {d.to for d in wv.installed_directives(modlist, wj, FakeGame(staging), profile)}
    assert to >= {"ModA/ModA.esp", "ModA/textures/a.dds", "ModB/config.json"}


def test_cancel_stops_the_check(tmp_path, monkeypatch):
    import threading
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    stop = threading.Event()
    stop.set()
    r = _check(wj, modlist, staging, profile, cancel=stop)
    assert r.cancelled and r.checked == 0
    assert "cancelled" in wv.verify_summary(r)


def test_layout_is_shared_with_the_installer():
    assert wi.layout_root_builder_mods is wv.layout_root_builder_mods


def test_files_the_game_rewrote_after_the_install_are_not_damage(tmp_path, monkeypatch):
    """Real case (2026-11-26): Nuclear Sunset's first launch rewrote New Vegas
    Reloaded's shader cache and Cell Offset Generator's .fco files through the
    deploy links; the check called 449 of them damaged."""
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    assert wv._installed_at([profile]) > 0            # the install recorded it
    tex = staging / "ModA" / "textures" / "a.dds"
    tex.write_bytes(b"\0" * tex.stat().st_size)                       # written now
    r = _check(wj, modlist, staging, profile)
    (p,) = r.problems
    assert p.reason == wv.WRITTEN_LATER
    assert r.ok and r.repairable == []
    assert "rebuilt caches" in wv.verify_summary(r)
    assert wv.remove_bad_files(r, staging) == 0


def test_without_a_recorded_install_time_the_meta_ini_dates_decide(tmp_path, monkeypatch):
    """A profile installed before installed_at was recorded. Real data: the
    game also re-saved a settings file with the same contents after its
    cache writes, so "the newest matching file" can't be the reference."""
    from Utils.profile.profile_state import (
        read_wabbajack_modlist_info,
        write_wabbajack_modlist_info,
    )
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    info = read_wabbajack_modlist_info(profile)
    info.pop("installed_at")
    write_wabbajack_modlist_info(profile, info)
    # the install's files and meta.ini stamps are an hour old; the game
    # rewrites one file five minutes later; one is damaged at install time
    old = time.time() - 3600
    for f in staging.rglob("*"):
        if f.is_file():
            os.utime(f, (old, old))
    tex = staging / "ModA" / "textures" / "a.dds"
    tex.write_bytes(b"\0" * tex.stat().st_size)
    os.utime(tex, (old + 300, old + 300))
    _damage(staging / "ModA" / "ModA.esp", b"ESP BYTEZ")
    got = {p.path: p.reason for p in _check(wj, modlist, staging, profile).problems}
    assert got == {"ModA/textures/a.dds": wv.WRITTEN_LATER,
                   "ModA/ModA.esp": wv.CHANGED}


def test_a_damaged_plugin_is_damage_whatever_its_date(tmp_path, monkeypatch):
    """Deploying sets plugin dates to fix the load order, so a plugin the
    install got wrong can carry a date after the install."""
    wj, modlist, _, staging, profile = _installed(tmp_path, monkeypatch)
    (staging / "ModA" / "ModA.esp").write_bytes(b"ESP BYTEZ")          # dated now
    (p,) = _check(wj, modlist, staging, profile).problems
    assert p.reason == wv.CHANGED and p.repairable

"""Fallout 3 after the Anniversary Patcher, and a list's manual-install files.

Real cases, 2026-11-25:
* Mosaic's Downgrade wizard left the patcher waiting at its final "pause"
  with nothing to answer it; the game's prefix stayed busy and Steam never
  started the game.
* Fallout Rebirth's older fose_loader, swapped in for the launcher, rejected
  the patched Fallout3.exe (CRC 6E3D50D1) -- the patched exe loads FOSE itself.
* Waters of Life ships FOSE, Ogg Vorbis libraries and the patcher in
  "__Files Requiring Manual Install"; the installer skipped the folder."""
from __future__ import annotations

from pathlib import Path

from Games.Bethesda.fallout_3_goty import Fallout3_GOTY
from Utils.mods.modlist import read_modlist
from Utils.wabbajack import wabbajack_install as wi
from Utils.wabbajack.wabbajack_manifest import FromArchiveDirective, InlineFileDirective, ModList
from wizards_qt import fallout_downgrade_view as dv


def _game(tmp_path, patched: bool):
    root = tmp_path / "Fallout 3 goty"
    root.mkdir()
    (root / "Fallout3Launcher.exe").write_bytes(b"bethesda launcher")
    (root / "fose_loader.exe").write_bytes(b"old fose loader")
    (root / "Fallout3.exe").write_bytes(b"patched" if patched else b"1.7.0.4")
    if patched:
        (root / "Fallout3_backup.exe").write_bytes(b"1.7.0.4")
    g = Fallout3_GOTY()
    g._game_path = root
    return g, root


def test_a_patched_game_keeps_its_own_launcher(tmp_path):
    g, root = _game(tmp_path, patched=True)
    logs = []
    g.swap_launcher(logs.append)
    assert (root / "Fallout3Launcher.exe").read_bytes() == b"bethesda launcher"
    assert not (root / "Fallout3Launcher.bak").exists()
    assert any("loads FOSE itself" in m for m in logs)


def test_an_unpatched_game_still_gets_the_loader(tmp_path):
    g, root = _game(tmp_path, patched=False)
    g.swap_launcher(lambda m: None)
    assert (root / "Fallout3Launcher.exe").read_bytes() == b"old fose loader"
    assert (root / "Fallout3Launcher.bak").read_bytes() == b"bethesda launcher"


class _Proc:
    """A patcher that never exits by itself."""
    pid = 4242
    returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise dv.subprocess.TimeoutExpired("patcher", timeout)
        return self.returncode


def test_a_patcher_sitting_at_its_prompt_after_patching_is_ended(tmp_path, monkeypatch):
    t = [0.0]
    ended = []
    monkeypatch.setattr(dv, "_end_session", lambda p: ended.append(p.pid))

    def clock():
        t[0] += 5
        if t[0] >= 15:                            # the patch lands after 15 s
            (tmp_path / "Fallout3_backup.exe").write_bytes(b"orig")
        return t[0]

    logs = []
    dv._wait_for_patcher(_Proc(), tmp_path, logs.append, poll=0, linger=20, clock=clock)
    assert ended == [4242]
    assert any("still waiting after patching" in m for m in logs)


def test_a_patcher_still_working_is_left_alone_until_the_timeout(tmp_path, monkeypatch):
    t = [0.0]
    ended = []
    monkeypatch.setattr(dv, "_end_session", lambda p: ended.append(p.pid))

    def clock():
        t[0] += 60
        return t[0]

    logs = []
    dv._wait_for_patcher(_Proc(), tmp_path, logs.append, poll=0, timeout=600, clock=clock)
    assert ended == [4242]
    assert any("took too long" in m for m in logs)
    assert t[0] > 600


def _modlist():
    return ModList(directives=[
        FromArchiveDirective(to="mods\\ModA\\a.esp", archive_hash_path=["h", "a.esp"]),
        FromArchiveDirective(to="__Files Requiring Manual Install\\fose_loader.exe",
                             archive_hash_path=["h", "fose_loader.exe"]),
        FromArchiveDirective(to="__Files Requiring Manual Install\\src\\fose\\x.cpp",
                             archive_hash_path=["h", "x.cpp"]),
        InlineFileDirective(to="profiles\\Main\\modlist.txt", source_data_id="m"),
    ])


def test_manual_install_files_become_an_enabled_root_mod(tmp_path):
    plan = wi.classify_directives(_modlist())
    name = "Files Requiring Manual Install"
    assert plan.extra_root_mods == [name]
    assert sorted(d.to for d in plan.mod_directives if d.to.startswith(name)) == [
        f"{name}/fose_loader.exe", f"{name}/src/fose/x.cpp"]
    assert not any("Manual" in u for u in plan.unplaced)
    # the profile's modlist.txt doesn't list it; it survives the filter
    assert wi.limit_to_profile_mods(plan, {"ModA"}) == []
    assert {d.to.split("/")[0] for d in plan.mod_directives} == {"ModA", name}

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "modlist.txt").write_text("+ModA\n")
    profile = tmp_path / "profile"
    profile.mkdir()
    wi._write_profile(profile, scratch, {"ModA", name}, wi.WabbajackInstallReport(),
                      lambda m: None, enabled_extra={name})
    entries = {e.name: e.enabled for e in read_modlist(profile / "modlist.txt")}
    assert entries == {"ModA": True, name: True}


def test_dragon_age_prefixing_leaves_the_root_mod_alone():
    plan = wi.classify_directives(_modlist())
    wi._prefix_mo2_data_subdir(plan, "Dragon Age Origins")
    assert "Files Requiring Manual Install/fose_loader.exe" in {d.to for d in plan.mod_directives}
    assert "ModA/packages/core/override/a.esp" in {d.to for d in plan.mod_directives}


def test_the_waters_of_life_folder_layout_lands_in_the_game_root():
    # Its files sit directly in the folder (Patcher.exe, fose_loader.exe, ...)
    plan = wi.classify_directives(ModList(directives=[
        FromArchiveDirective(to="__Files Requiring Manual Install\\Patcher.exe",
                             archive_hash_path=["h", "Patcher.exe"])]))
    assert [d.to for d in plan.mod_directives] == ["Files Requiring Manual Install/Patcher.exe"]
    assert Path(plan.mod_directives[0].to).parts[1] == "Patcher.exe"

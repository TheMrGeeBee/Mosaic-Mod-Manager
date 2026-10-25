"""Tests for Utils.steam.acf and Utils.steam.depot_versions. Fixtures follow
real data: the user's Fallout 4 appmanifest layout and the content_log lines
Steam wrote for a real `download_depot` on 2026-10-08."""
from __future__ import annotations

import os

import pytest

from Utils.steam import depot_versions as dv
from Utils.steam.acf import VdfError, get_ci, parse_vdf

APPMANIFEST = """
"AppState"
{
	"appid"		"377160"
	"buildid"		"24564252"
	"InstalledDepots"
	{
		"377161"
		{
			"manifest"		"5983086794954940044"
			"size"		"6087603510"
		}
		"377162"
		{
			"manifest"		"4737695048810094469"
			"size"		"55343016"
		}
		"435881"
		{
			"manifest"		"1207717296920736193"
			"size"		"2890058024"
			"dlcappid"		"435881"
		}
	}
}
"""

# Exactly as Steam logged it (mixed separators included).
CONTENT_LOG = """\
[2026-10-08 03:07:55] Downloading 63 chunks for depot 377162 (5847529232406005096)
[2026-10-08 03:07:55] Downloading depot 377162 (1 files, 48 MB) ...
[2026-10-08 03:07:57] Depot download complete : "{root}/steamrt64\\steamapps\\content\\app_377160\\depot_377162" (manifest 5847529232406005096)
[2026-10-08 03:11:03] Downloading depot 435871 (2 files, 109 MB) ...
[2026-10-08 03:11:05] Depot download complete : "{root}/steamrt64\\steamapps\\content\\app_377160\\depot_435871" (manifest 5106118861901111234)
"""


def test_parse_real_appmanifest_shape():
    m = parse_vdf(APPMANIFEST)
    assert get_ci(m, "appstate", "APPID") == "377160"
    depots = get_ci(m, "AppState", "InstalledDepots")
    assert depots["435881"]["dlcappid"] == "435881"
    with pytest.raises(VdfError):
        parse_vdf('"a" { "b" "c"')


def test_read_installed_depots(tmp_path):
    game = tmp_path / "steamapps" / "common" / "Fallout 4"
    game.mkdir(parents=True)
    (tmp_path / "steamapps" / "appmanifest_377160.acf").write_text(APPMANIFEST)
    assert dv.read_installed_depots(game, "377160") == {
        "377161": "5983086794954940044", "377162": "4737695048810094469",
        "435881": "1207717296920736193"}
    assert dv.read_installed_depots(tmp_path / "elsewhere", "377160") == {}


def test_logs_are_found_with_their_rotations(tmp_path):
    logs = tmp_path / "Steam" / "logs"
    logs.mkdir(parents=True)
    for n in ("console_log.txt", "console_log.previous.txt", "content_log.txt", "other.txt"):
        (logs / n).write_text("x")
    names = [p.name for p in dv.content_log_paths([tmp_path / "Steam"])]
    assert names == ["console_log.previous.txt", "console_log.txt", "content_log.txt"]


def test_completed_downloads_from_the_real_log(tmp_path):
    folder = tmp_path / "steamrt64" / "steamapps" / "content" / "app_377160" / "depot_377162"
    folder.mkdir(parents=True)
    log = CONTENT_LOG.format(root=tmp_path)
    done = dv.completed_downloads(log)
    assert done[("377162", "5847529232406005096")] == folder
    assert dv.download_sizes(log) == {"377162": (1, "48 MB"), "435871": (2, "109 MB")}
    assert dv.find_downloaded_depot("377160", "377162", "5847529232406005096",
                                    log_text=log) == folder
    # Only the logged manifest counts; the folder alone isn't proof.
    assert dv.find_downloaded_depot("377160", "377162", "1", log_text=log) is None
    # Logged but the folder is gone (e.g. deleted since).
    assert dv.find_downloaded_depot("377160", "435871", "5106118861901111234",
                                    log_text=log) is None


def test_shipped_fallout4_table_and_plan_skips_matching_depots():
    table = dv.load_table("377160")
    entry = table.get("1.10.163")
    assert table.exe_file == "Fallout4.exe" and entry.exe_version == "1.10.163.0"
    assert entry.depots["377162"] == "5847529232406005096"
    assert "393899" not in entry.depots            # the suspect duplicate stays out
    installed = {"377161": "5983086794954940044", "377162": "4737695048810094469",
                 "435881": "1207717296920736193", "999999": "1"}
    plan = dv.plan_switch(entry, installed)
    assert plan.download == [("377161", "7497069378349273908"),
                             ("377162", "5847529232406005096")]
    assert plan.unchanged == ["435881"]            # Far Harbor didn't change
    assert plan.not_in_version == ["999999"]
    assert dv.console_command("377160", *plan.download[1]) == \
        "download_depot 377160 377162 5847529232406005096"


def test_user_entries_and_paste(tmp_path):
    paste = """377161\t7497069378349273908\tsome date
    download_depot 377160 377162 5847529232406005096
    noise line"""
    assert dv.parse_depot_paste(paste) == {"377161": "7497069378349273908",
                                           "377162": "5847529232406005096"}
    user = tmp_path / "steam_versions.user.json"
    dv.save_user_entry(user, dv.VersionEntry(label="1.11.221", depots={"377162": "42" * 8},
                                             exe_version="1.11.221.0"), exe_file="Fallout4.exe")
    table = dv.load_table("377160", user)
    assert table.get("1.11.221").user and table.get("1.10.163") is not None


# ---------------------------------------------------------------------------
# apply / revert on a fake game folder
# ---------------------------------------------------------------------------

def _setup(tmp_path):
    game = tmp_path / "game"
    (game / "Data").mkdir(parents=True)
    (game / "Fallout4.exe").write_bytes(b"exe 1.11")
    (game / "Data" / "Fallout4.esm").write_bytes(b"esm 1.11")
    (game / "Data" / "Only1.11.ba2").write_bytes(b"newer-build-only")
    (game / "Data" / "Untouched.esp").write_bytes(b"keep")
    depot = tmp_path / "content" / "depot_377162"
    (depot / "Data").mkdir(parents=True)
    (depot / "Fallout4.exe").write_bytes(b"exe 1.10")
    (depot / "Data" / "Fallout4.esm").write_bytes(b"esm 1.10")
    (depot / "Data" / "OldOnly.ba2").write_bytes(b"older-build-only")
    entry = dv.VersionEntry(label="1.10.163", depots={"377162": "5847529232406005096"},
                            remove=["Data/Only1.11.ba2"])
    return game, {"377162": depot}, entry, tmp_path / "state"


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_apply_then_revert_round_trip(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    before = _snapshot(game)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    assert (game / "Fallout4.exe").read_bytes() == b"exe 1.10"
    assert (game / "Data" / "Fallout4.esm").read_bytes() == b"esm 1.10"
    assert (game / "Data" / "OldOnly.ba2").is_file()
    assert not (game / "Data" / "Only1.11.ba2").exists()
    assert (game / "Data" / "Untouched.esp").read_bytes() == b"keep"
    assert not list(game.rglob("*.mosaic-version"))
    assert dv.load_state(state)["current"] == "1.10.163"

    assert dv.revert_version(game, state) == "1.11.240"
    assert _snapshot(game) == before
    assert dv.load_state(state) is None


def test_apply_rolls_back_when_a_swap_fails(tmp_path, monkeypatch):
    game, dirs, entry, state = _setup(tmp_path)
    before = _snapshot(game)
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2 and str(src).endswith(".mosaic-version"):
            raise OSError("disk hiccup")
        return real(src, dst)
    monkeypatch.setattr(dv.os, "replace", flaky)
    with pytest.raises(dv.VersionSwitchError, match="disk hiccup"):
        dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    assert _snapshot(game) == before
    assert dv.load_state(state) is None


def test_apply_refuses_when_deployed_running_or_already_switched(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    with pytest.raises(dv.VersionSwitchError, match="running"):
        dv.apply_version(game, entry, dirs, state, from_label="x", game_running=True)
    (game / "Data_Core").mkdir()
    with pytest.raises(dv.VersionSwitchError, match="deployed"):
        dv.apply_version(game, entry, dirs, state, from_label="x")
    (game / "Data_Core").rmdir()
    # Fallout 4's real half-deployed state: Data clean, a root file still linked.
    (game / "f4se_loader.exe").symlink_to(tmp_path / "staging_f4se_loader.exe")
    with pytest.raises(dv.VersionSwitchError, match="linked"):
        dv.apply_version(game, entry, dirs, state, from_label="x")
    (game / "f4se_loader.exe").unlink()
    (game / "Fallout4Launcher.bak").write_bytes(b"real launcher")
    with pytest.raises(dv.VersionSwitchError, match="Fallout4Launcher.bak"):
        dv.apply_version(game, entry, dirs, state, from_label="x")
    (game / "Fallout4Launcher.bak").unlink()
    with pytest.raises(dv.VersionSwitchError, match="record"):
        dv.apply_version(game, entry, dirs, state, from_label="x", deploy_active=True)
    dv.apply_version(game, entry, dirs, state, from_label="x")
    with pytest.raises(dv.VersionSwitchError, match="Switch back first"):
        dv.apply_version(game, entry, dirs, state, from_label="x")


def test_apply_checks_the_exe_version_and_rolls_back(tmp_path, monkeypatch):
    from Utils.wizard_support import pe_version
    game, dirs, entry, state = _setup(tmp_path)
    before = _snapshot(game)
    entry.exe_version = "1.10.163.0"
    monkeypatch.setattr(pe_version, "read_file_version", lambda p: (1, 11, 221, 0))
    with pytest.raises(dv.VersionSwitchError, match="not 1.10.163.0"):
        dv.apply_version(game, entry, dirs, state, from_label="x", exe_file="Fallout4.exe")
    assert _snapshot(game) == before


def test_revert_refuses_a_damaged_backup_without_touching_anything(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    after = _snapshot(game)
    backup = dv.load_state(state)["backup_dir"]
    (tmp_path / backup / "Data" / "Fallout4.esm").write_bytes(b"corrupted")
    with pytest.raises(dv.VersionSwitchError, match="Verify integrity"):
        dv.revert_version(game, state)
    assert _snapshot(game) == after


def test_depot_paths_match_the_game_folder_case_insensitively(tmp_path):
    """The depot says Data/Fallout4.esm; this game folder has data/fallout4.esm.
    The existing file must be replaced, not a second copy created."""
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "data" / "fallout4.esm").write_bytes(b"old")
    depot = tmp_path / "depot"
    (depot / "Data").mkdir(parents=True)
    (depot / "Data" / "Fallout4.esm").write_bytes(b"new")
    dv.apply_version(game, dv.VersionEntry(label="v", depots={}), {"1": depot},
                     tmp_path / "state", from_label="old")
    assert sorted(p.relative_to(game).as_posix() for p in game.rglob("*") if p.is_file()) == \
        ["data/fallout4.esm"]
    assert (game / "data" / "fallout4.esm").read_bytes() == b"new"
    dv.revert_version(game, tmp_path / "state")
    assert (game / "data" / "fallout4.esm").read_bytes() == b"old"


def test_a_folder_only_counts_for_the_latest_manifest_completed_into_it(tmp_path):
    """Real sequence (2026-10-08): depot 377162 downloaded at the 1.10.163
    manifest, then at the current one into the same folder. The 1.10.163
    line must no longer count."""
    folder = tmp_path / "steamrt64" / "steamapps" / "content" / "app_377160" / "depot_377162"
    folder.mkdir(parents=True)
    path = f"{tmp_path}/steamrt64\\\\steamapps\\\\content\\\\app_377160\\\\depot_377162"
    log = (f'[2026-10-08 03:07:57] Depot download complete : "{path}" (manifest 5847529232406005096)\n'
           f'[2026-10-08 03:55:37] Depot download complete : "{path}" (manifest 4737695048810094469)\n')
    assert dv.find_downloaded_depot("377160", "377162", "5847529232406005096", log_text=log) is None
    assert dv.find_downloaded_depot("377160", "377162", "4737695048810094469", log_text=log) == folder


# ---------------------------------------------------------------------------
# Trusting a depot folder (Steam doesn't shorten files that got smaller)
# ---------------------------------------------------------------------------

def _line(ts, root, depot, manifest):
    return (f'[{ts}] Depot download complete : "{root}/steamrt64\\\\steamapps\\\\content'
            f'\\\\app_377160\\\\depot_{depot}" (manifest {manifest})\n')


def test_depot_status_follows_clean_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(dv, "_now_stamp", lambda: "2026-10-08 04:17:02")
    root, state = tmp_path / "Steam", tmp_path / "state"
    folder = root / "steamrt64" / "steamapps" / "content" / "app_377160" / "depot_377162"
    want = "387388833281246371"

    # Absent: a clean start is recorded.
    st = dv.depot_status("377160", "377162", want, state_dir=state, log_text="", roots=[root])
    assert st.state == "waiting"

    # Completed after the clean start, only the wanted manifest: trusted.
    folder.mkdir(parents=True)
    log = _line("2026-10-08 04:20:00", root, "377162", want)
    assert dv.depot_status("377160", "377162", want, state_dir=state, log_text=log,
                           roots=[root]).state == "downloaded"

    # Another manifest written into it afterwards: no longer trusted.
    log += _line("2026-10-08 04:25:00", root, "377162", "4737695048810094469")
    st = dv.depot_status("377160", "377162", want, state_dir=state, log_text=log, roots=[root])
    assert (st.state, st.holds) == ("needs_clear", "4737695048810094469")


def test_reused_folder_without_a_clean_start_must_be_cleared(tmp_path, monkeypatch):
    """Tonight's real sequence: 1.10.163 downloaded, then 1.11.221 written
    into the same folder -- the 1.11.221 line alone must not be trusted."""
    monkeypatch.setattr(dv, "_now_stamp", lambda: "2026-10-08 04:17:02")
    root, state = tmp_path / "Steam", tmp_path / "state"
    folder = root / "steamrt64" / "steamapps" / "content" / "app_377160" / "depot_377162"
    folder.mkdir(parents=True)
    (folder / "Fallout4.exe").write_bytes(b"old")
    (folder.parent / "state_377160_377162.patch").write_bytes(b"resume")
    log = (_line("2026-10-08 03:07:57", root, "377162", "5847529232406005096")
           + _line("2026-10-08 04:05:26", root, "377162", "387388833281246371"))
    st = dv.depot_status("377160", "377162", "387388833281246371", state_dir=state,
                         log_text=log, roots=[root])
    assert (st.state, st.holds) == ("needs_clear", "387388833281246371")
    dv.clear_depot_folder(state, st.folder)
    assert not folder.exists() and not (folder.parent / "state_377160_377162.patch").exists()
    # The old lines predate the clean start, so nothing counts yet.
    folder.mkdir()
    assert dv.depot_status("377160", "377162", "387388833281246371", state_dir=state,
                           log_text=log, roots=[root]).state == "waiting"
    log += _line("2026-10-08 04:30:00", root, "377162", "387388833281246371")
    assert dv.depot_status("377160", "377162", "387388833281246371", state_dir=state,
                           log_text=log, roots=[root]).state == "downloaded"


def test_deployment_traces_ignores_a_proton_prefix_in_the_game_folder(tmp_path):
    """Real case (Cyberpunk 2077, 2026-10-23): bin/x64/prefix_proton-cachyos-slr
    holds Wine's own symlinks (dosdevices/c:, Application Data), which are not
    mod files and made the version check fail after a clean restore."""
    prefix = tmp_path / "bin" / "x64" / "prefix_proton-cachyos-slr" / "pfx"
    (prefix / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    (prefix / "dosdevices").mkdir()
    os.symlink("../drive_c", prefix / "dosdevices" / "c:")
    assert dv.deployment_traces(tmp_path) == []
    # A real linked mod file next to it is still caught.
    (tmp_path / "mod.dll").write_bytes(b"x")
    os.symlink(tmp_path / "mod.dll", tmp_path / "bin" / "x64" / "linked.dll")
    [trace] = dv.deployment_traces(tmp_path)
    assert "linked.dll" in trace

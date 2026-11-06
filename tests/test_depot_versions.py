"""Tests for Utils.steam.acf and Utils.steam.depot_versions. Fixtures follow
real data: the user's Fallout 4 appmanifest layout and the content_log lines
Steam wrote for a real `download_depot` on 2026-10-08."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil

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


def test_shipped_skyrim_se_table_covers_the_builds_people_downgrade_to():
    """Skyrim SE's table exists so nobody has to go to SteamDB and work out
    depot and manifest IDs by hand -- the step that made this wizard hard to
    use. The three content depots are the whole base game; SE has no separate
    DLC depots."""
    table = dv.load_table("489830")
    assert table.exe_file == "SkyrimSE.exe"
    assert [v.label for v in table.versions] == ["1.5.97", "1.6.640", "1.6.1170"]
    for entry in table.versions:
        assert set(entry.depots) == {"489831", "489832", "489833"}, entry.label
        assert all(m.isdigit() for m in entry.depots.values()), entry.label
        assert entry.exe_version.startswith(entry.label + "."), entry.label
        assert entry.source, "every shipped entry says where its manifests came from"
    # 1.6.1170 is the build skyrim_runtime.py's SRS patches also land on, so
    # the two routes to it must not disagree about what it is called.
    from Utils.modding_tools import skyrim_runtime
    assert str(skyrim_runtime.STEAM_APP_ID) == table.app_id
    assert skyrim_runtime.VERSION_TARGET == table.exe_file


def test_every_shipped_version_table_parses_and_is_self_consistent():
    """A table with a duplicate label would silently shadow an entry, and one
    whose filename doesn't match its app_id would never be found."""
    import json

    for path in sorted(dv.VERSIONS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["app_id"] == path.stem, path.name
        table = dv.load_table(path.stem)
        assert table.exe_file, path.name
        labels = [v.label for v in table.versions]
        assert labels and len(labels) == len(set(labels)), path.name
        for entry in table.versions:
            assert entry.depots, f"{path.name}: {entry.label} has no depots"


# ---------------------------------------------------------------------------
# The version store: switching between builds with no download
# ---------------------------------------------------------------------------

def _depot(tmp_path, name, **files):
    d = tmp_path / "content" / name
    for rel, data in files.items():
        p = d / rel.replace("|", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


def test_a_round_trip_needs_no_second_download(tmp_path):
    """The point of the store: after switching away and back, the downgraded
    build is still Mosaic's to hand out, even though Steam's depot folder has
    been emptied (it only ever holds one manifest)."""
    game, dirs, entry, state = _setup(tmp_path)
    newer = _snapshot(game)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    older = _snapshot(game)

    assert dv.revert_version(game, state) == "1.11.240"
    assert _snapshot(game) == newer
    shutil.rmtree(dirs["377162"])            # Steam's folder is reused/gone

    assert dv.stored_labels(state) == ["1.10.163"]
    assert dv.swap_to(game, state, "1.10.163") == "1.10.163"
    assert _snapshot(game) == older
    assert dv.stored_labels(state) == ["1.11.240"]
    assert dv.current_label(state) == "1.10.163"
    # And back again, still with nothing to download.
    assert dv.swap_to(game, state, "1.11.240") == "1.11.240"
    assert _snapshot(game) == newer


def test_switching_to_the_build_already_installed_does_nothing(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    before = _snapshot(game)
    assert dv.swap_to(game, state, "1.10.163") == "1.10.163"
    assert _snapshot(game) == before


def test_the_state_file_follows_the_store(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    assert dv.load_state(state)["current"] == "1.10.163"
    dv.swap_to(game, state, "1.11.240")
    # Back on the build Steam installed: nothing is switched any more.
    assert dv.load_state(state) is None
    dv.swap_to(game, state, "1.10.163")
    back = dv.load_state(state)
    assert (back["current"], back["from"]) == ("1.10.163", "1.11.240")
    assert dv.revert_version(game, state) == "1.11.240"


def test_a_third_build_keeps_the_other_two_whole(tmp_path):
    """The subtle one. A path becomes "managed" the first time any switch
    touches it, so a build stored before that has no entry for it -- and a
    missing entry means "that build has no such file", which would delete it.
    Here 1.10.163 never touches the esm and must keep the one it was built on.
    """
    game, dirs, entry, state = _setup(tmp_path)
    exe_only = _depot(tmp_path, "exe_only", **{"Fallout4.exe": b"exe 1.10"})
    both = _depot(tmp_path, "both", **{"Fallout4.exe": b"exe 1.09",
                                       "Data|Fallout4.esm": b"esm 1.09"})
    e10 = dv.VersionEntry(label="1.10.163", depots={"1": "x"})
    e09 = dv.VersionEntry(label="1.09.000", depots={"1": "y"})

    dv.apply_version(game, e10, {"1": exe_only}, state, from_label="1.11.240")
    assert dv.read_index(state)["managed"] == ["Fallout4.exe"]
    dv.swap_to(game, state, "1.11.240")
    dv.apply_version(game, e09, {"1": both}, state, from_label="1.11.240")
    assert dv.read_index(state)["managed"] == ["Data/Fallout4.esm", "Fallout4.exe"]

    assert dv.swap_to(game, state, "1.10.163") == "1.10.163"
    assert (game / "Fallout4.exe").read_bytes() == b"exe 1.10"
    assert (game / "Data" / "Fallout4.esm").read_bytes() == b"esm 1.11"
    assert sorted(dv.stored_labels(state)) == ["1.09.000", "1.11.240"]
    assert dv.swap_to(game, state, "1.09.000") == "1.09.000"
    assert (game / "Fallout4.exe").read_bytes() == b"exe 1.09"
    assert (game / "Data" / "Fallout4.esm").read_bytes() == b"esm 1.09"


def test_a_file_only_one_build_has_comes_and_goes(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    assert (game / "Data" / "OldOnly.ba2").is_file()
    assert not (game / "Data" / "Only1.11.ba2").exists()
    dv.swap_to(game, state, "1.11.240")
    assert not (game / "Data" / "OldOnly.ba2").exists()
    assert (game / "Data" / "Only1.11.ba2").is_file()


def test_swap_refuses_a_damaged_slot_and_an_unknown_build(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    after = _snapshot(game)
    with pytest.raises(dv.VersionSwitchError, match="doesn't keep"):
        dv.swap_to(game, state, "1.09.000")
    slot = dv.read_index(state)["slots"]["1.11.240"]["dir"]
    (pathlib.Path(slot) / "Data" / "Fallout4.esm").write_bytes(b"corrupted")
    with pytest.raises(dv.VersionSwitchError, match="Verify integrity"):
        dv.swap_to(game, state, "1.11.240")
    assert _snapshot(game) == after


def test_swap_refuses_while_deployed_or_running(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    with pytest.raises(dv.VersionSwitchError, match="running"):
        dv.swap_to(game, state, "1.11.240", game_running=True)
    (game / "Data_Core").mkdir()
    with pytest.raises(dv.VersionSwitchError, match="deployed"):
        dv.swap_to(game, state, "1.11.240")


def test_swap_puts_everything_back_when_it_fails(tmp_path, monkeypatch):
    game, dirs, entry, state = _setup(tmp_path)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    after = _snapshot(game)
    real = os.replace

    def boom(src, dst):
        if str(dst).endswith("Fallout4.esm"):
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(dv.VersionSwitchError, match="disk full"):
        dv.swap_to(game, state, "1.11.240")
    monkeypatch.setattr(os, "replace", real)
    assert _snapshot(game) == after
    assert not list(game.rglob("*.mosaic-version"))
    # The store is intact, so the switch can simply be tried again.
    assert dv.swap_to(game, state, "1.11.240") == "1.11.240"


def test_a_switch_made_before_the_store_existed_still_swaps(tmp_path):
    """An install that switched with an older Mosaic has no index, only
    version_state.json and the throw-away version_backup folder it names.
    That state describes one complete slot, so it migrates as it is read."""
    game, dirs, entry, state = _setup(tmp_path)
    newer = _snapshot(game)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    older = _snapshot(game)
    legacy = pathlib.Path(state) / dv.BACKUP_DIRNAME / "1.11.240"
    shutil.move(str(dv.read_index(state)["slots"]["1.11.240"]["dir"]), str(legacy))
    (pathlib.Path(state) / dv.INDEX_FILENAME).unlink()
    st = dv.load_state(state)
    st["backup_dir"] = str(legacy)
    dv._write_state(state, st)

    index = dv.read_index(state)
    assert index["current"] == "1.10.163" and index["base"] == "1.11.240"
    assert dv.swap_to(game, state, "1.11.240") == "1.11.240"
    assert _snapshot(game) == newer
    # ...and the build it came from is now a slot of its own, as it would be
    # had the store been there all along.
    assert dv.swap_to(game, state, "1.10.163") == "1.10.163"
    assert _snapshot(game) == older


def _library(tmp_path):
    game = tmp_path / "steamapps" / "common" / "Fallout 4"
    (game / "Data").mkdir(parents=True)
    (game / "Fallout4.exe").write_bytes(b"exe 1.11")
    (game / "Data" / "Fallout4.esm").write_bytes(b"esm 1.11")
    (tmp_path / "steamapps" / "appmanifest_377160.acf").write_text(APPMANIFEST)
    return game


def test_switch_options_says_how_each_build_would_be_reached(tmp_path):
    game = _library(tmp_path)
    state = tmp_path / "state"
    table = dv.VersionTable(app_id="377160", exe_file="Fallout4.exe", versions=[
        dv.VersionEntry(label="cached", depots={"377162": "5847529232406005096"}),
        dv.VersionEntry(label="far away", depots={"377162": "9" * 19})])
    folder = tmp_path / "steamrt64" / "steamapps" / "content" / "app_377160" / "depot_377162"
    folder.mkdir(parents=True)
    log = CONTENT_LOG.format(root=tmp_path)
    dv.mark_clean_start(state, folder, when="2026-10-08 00:00:00")

    opts = {o.label: o for o in dv.switch_options(game, "377160", state, table, log_text=log,
                                                  roots=[tmp_path])}
    assert opts["Steam build 24564252"].state == dv.SwitchOption.CURRENT
    assert opts["cached"].state == dv.SwitchOption.CACHED
    assert opts["cached"].depot_dirs == {"377162": folder}
    assert opts["far away"].state == dv.SwitchOption.NEEDS_DOWNLOAD
    assert opts["far away"].download == [("377162", "9" * 19)]
    assert [o.instant for o in (opts["cached"], opts["far away"])] == [True, False]


def test_switch_options_reads_the_installed_build_from_mosaic_not_steam(tmp_path):
    """Mosaic never edits Steam's appmanifest, so Steam goes on reporting the
    build it installed while a switched one is on disk. The list has to say
    what Mosaic put there, or it would offer to "switch" to what is running."""
    game = _library(tmp_path)
    state = tmp_path / "state"
    depot = _depot(tmp_path, "d", **{"Fallout4.exe": b"exe 1.10"})
    entry = dv.VersionEntry(label="1.10.163", depots={"377162": "1" * 19})
    table = dv.VersionTable(app_id="377160", exe_file="Fallout4.exe", versions=[entry])
    dv.apply_version(game, entry, {"377162": depot}, state, from_label="Steam build 24564252")

    opts = {o.label: o for o in dv.switch_options(game, "377160", state, table, log_text="",
                                                  roots=[tmp_path])}
    assert opts["1.10.163"].state == dv.SwitchOption.CURRENT
    assert opts["Steam build 24564252"].state == dv.SwitchOption.STORED
    # Current first, whatever order the table is in.
    assert dv.switch_options(game, "377160", state, table, log_text="",
                             roots=[tmp_path])[0].label == "1.10.163"


def test_switch_to_puts_the_installed_build_back_before_applying(tmp_path):
    """Applying a download runs on the build Steam installed, so switching
    from one downgrade straight to another has to pass through it -- leaving
    the first downgrade in the store, ready to swap back to."""
    game = _library(tmp_path)
    state = tmp_path / "state"
    base = _snapshot(game)
    d10 = _depot(tmp_path, "d10", **{"Fallout4.exe": b"exe 1.10"})
    d09 = _depot(tmp_path, "d09", **{"Fallout4.exe": b"exe 1.09"})
    e10 = dv.VersionEntry(label="1.10.163", depots={"377162": "1" * 19})
    e09 = dv.VersionEntry(label="1.09.000", depots={"377162": "2" * 19})

    dv.apply_version(game, e10, {"377162": d10}, state, from_label="Steam build 24564252")
    opt = dv.SwitchOption("1.09.000", dv.SwitchOption.CACHED, entry=e09,
                          depot_dirs={"377162": d09})
    assert dv.switch_to(game, state, opt) == "1.09.000"
    assert (game / "Fallout4.exe").read_bytes() == b"exe 1.09"
    assert sorted(dv.stored_labels(state)) == ["1.10.163", "Steam build 24564252"]

    assert dv.swap_to(game, state, "1.10.163") == "1.10.163"
    assert (game / "Fallout4.exe").read_bytes() == b"exe 1.10"
    assert dv.swap_to(game, state, "Steam build 24564252") == "Steam build 24564252"
    assert _snapshot(game) == base


def test_switch_to_refuses_a_build_that_is_not_on_hand(tmp_path):
    game = _library(tmp_path)
    opt = dv.SwitchOption("1.09.000", dv.SwitchOption.NEEDS_DOWNLOAD)
    with pytest.raises(dv.VersionSwitchError, match="isn't downloaded yet"):
        dv.switch_to(game, tmp_path / "state", opt)


def test_builds_are_named_by_the_exe_each_one_has(tmp_path):
    """The appmanifest only knows a build id, which is not what anyone calls a
    version. The exe says 1.7.104, and the exe of a build that isn't installed
    is in its store slot, so a stored build can be named too."""
    from tests.test_pe_version import build_pe
    game, _dirs, _entry, state = _setup(tmp_path)
    (game / "Fallout4.exe").write_bytes(build_pe((1, 11, 240, 0)))
    depot = _depot(tmp_path, "old", **{"Fallout4.exe": build_pe((1, 10, 163, 0))})
    entry = dv.VersionEntry(label="1.10.163", depots={"1": "x"})
    dv.apply_version(game, entry, {"1": depot}, state, from_label="Steam build 24564252")

    names = dv.build_names(game, state, "Fallout4.exe")
    assert names == {"1.10.163": "1.10.163", "Steam build 24564252": "1.11.240"}
    # Labels stay the identity; only the display changes.
    assert dv.stored_labels(state) == ["Steam build 24564252"]
    assert dv.swap_to(game, state, "Steam build 24564252") == "Steam build 24564252"


def test_a_build_with_no_readable_exe_keeps_its_label(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    assert dv.build_names(game, state, "Fallout4.exe") == {
        "1.10.163": "1.10.163", "1.11.240": "1.11.240"}
    assert dv.build_names(game, state, "") == {"1.10.163": "1.10.163", "1.11.240": "1.11.240"}


def test_two_builds_reporting_one_version_fall_back_to_their_labels(tmp_path):
    from tests.test_pe_version import build_pe
    game, _dirs, _entry, state = _setup(tmp_path)
    same = build_pe((1, 11, 240, 0))
    (game / "Fallout4.exe").write_bytes(same)
    depot = _depot(tmp_path, "same", **{"Fallout4.exe": same, "Data|x.ba2": b"differs"})
    dv.apply_version(game, dv.VersionEntry(label="re-release", depots={"1": "x"}),
                     {"1": depot}, state, from_label="Steam build 24564252")
    names = dv.build_names(game, state, "Fallout4.exe")
    assert names == {"re-release": "re-release", "Steam build 24564252": "Steam build 24564252"}


def test_records_survive_being_written_from_many_threads_at_once(tmp_path):
    """depot_status marks a clean start from whatever worker thread it runs on,
    and the build dropdown can have two of those in flight. A shared temp file
    let them land truncated JSON, which reads back as "no record" -- and a
    depot with no record is treated as holding another version."""
    import threading
    state = tmp_path / "state"
    folders = [tmp_path / f"depot_{i}" for i in range(12)]
    errors = []

    def hammer(folder):
        try:
            for _ in range(25):
                dv.mark_clean_start(state, folder)
                json.loads((state / dv.DOWNLOADS_FILENAME).read_text(encoding="utf-8"))
        except Exception as exc:                 # truncated JSON lands here
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(f,)) for f in folders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert not list(state.glob("*.tmp"))
    assert set(dv._load_records(state)) == {str(f) for f in folders}


def test_a_runtime_swapped_exe_does_not_rename_the_build(tmp_path):
    """Skyrim's runtime swap patches the exe back to an older version on
    purpose, on top of the newer build's data files -- what LoreRim wants:
    1.7.104 game files under a 1.6.1170 exe. The build is still 1.7.104, and
    naming it after the patched exe would both misreport it and collide with
    the genuine 1.6.1170 in the store."""
    from tests.test_pe_version import build_pe
    game, _dirs, _entry, state = _setup(tmp_path)
    (game / "SkyrimSE.exe").write_bytes(build_pe((1, 7, 104, 0)))
    depot = _depot(tmp_path, "older", **{"SkyrimSE.exe": build_pe((1, 6, 1170, 0))})
    dv.apply_version(game, dv.VersionEntry(label="1.6.1170", depots={"1": "x"}),
                     {"1": depot}, state, from_label="Steam build 24914197")
    dv.swap_to(game, state, "Steam build 24914197")
    assert dv.build_names(game, state, "SkyrimSE.exe")["Steam build 24914197"] == "1.7.104"

    # Now the runtime swap patches the exe down, leaving the 1.7.104 data.
    (game / "SkyrimSE.exe").write_bytes(build_pe((1, 6, 1170, 0)))
    (pathlib.Path(state) / "runtime_state.json").write_text(json.dumps(
        {"applied": True, "from": "1.7.104.0", "to": "1.6.1170.0"}), encoding="utf-8")
    names = dv.build_names(game, state, "SkyrimSE.exe")
    assert names["Steam build 24914197"] == "1.7.104"
    assert names["1.6.1170"] == "1.6.1170"


def test_a_stale_runtime_record_does_not_rename_anything(tmp_path):
    """A record only counts while the exe still reads as what it produced."""
    from tests.test_pe_version import build_pe
    game, _dirs, _entry, state = _setup(tmp_path)
    (game / "SkyrimSE.exe").write_bytes(build_pe((1, 7, 104, 0)))
    depot = _depot(tmp_path, "older", **{"SkyrimSE.exe": build_pe((1, 6, 1170, 0))})
    dv.apply_version(game, dv.VersionEntry(label="1.6.1170", depots={"1": "x"}),
                     {"1": depot}, state, from_label="Steam build 24914197")
    dv.swap_to(game, state, "Steam build 24914197")
    (pathlib.Path(state) / "runtime_state.json").write_text(json.dumps(
        {"applied": True, "from": "1.6.1170.0", "to": "1.5.97.0"}), encoding="utf-8")
    assert dv.build_names(game, state, "SkyrimSE.exe")["Steam build 24914197"] == "1.7.104"


def _runtime_record(state, game, *, frm, to, patched: "dict[str, bytes]"):
    """A runtime_state.json as skyrim_runtime writes one, with the patched
    hashes of the files it claims are on disk."""
    files = {rel: {"original_sha256": "0" * 64,
                   "patched_sha256": hashlib.sha256(data).hexdigest()}
             for rel, data in patched.items()}
    pathlib.Path(state).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(state) / "runtime_state.json").write_text(
        json.dumps({"applied": True, "from": frm, "to": to, "files": files}), encoding="utf-8")


def _skyrim_on(tmp_path, exe_version):
    """A Skyrim folder on *exe_version* with one build stored, ready to swap."""
    from tests.test_pe_version import build_pe
    game, _dirs, _entry, state = _setup(tmp_path)
    (game / "SkyrimSE.exe").write_bytes(build_pe((1, 7, 104, 0)))
    (game / "SkyrimSELauncher.exe").write_bytes(b"launcher 1.7.104")
    depot = _depot(tmp_path, "older", **{"SkyrimSE.exe": build_pe(exe_version),
                                         "SkyrimSELauncher.exe": b"launcher 1.6.1170"})
    dv.apply_version(game, dv.VersionEntry(label="1.6.1170", depots={"1": "x"}),
                     {"1": depot}, state, from_label="Steam build 24914197")
    dv.swap_to(game, state, "Steam build 24914197")
    return game, state


def test_a_build_switch_refuses_while_the_runtime_swap_is_applied(tmp_path):
    """The runtime swap patches nine of the files the store manages. Taking
    them to be the installed build's own would store a mixture, and the swap's
    own revert would later write its build's files over whatever is installed.
    """
    from tests.test_pe_version import build_pe
    game, state = _skyrim_on(tmp_path, (1, 6, 1170, 0))
    patched_exe = build_pe((1, 6, 1170, 0))
    (game / "SkyrimSE.exe").write_bytes(patched_exe)
    (game / "SkyrimSELauncher.exe").write_bytes(b"patched launcher")
    _runtime_record(state, game, frm="1.7.104.0", to="1.6.1170.0",
                    patched={"SkyrimSE.exe": patched_exe,
                             "SkyrimSELauncher.exe": b"patched launcher"})
    before = _snapshot(game)
    with pytest.raises(dv.VersionSwitchError, match="runtime swap is applied"):
        dv.swap_to(game, state, "1.6.1170")
    assert _snapshot(game) == before
    assert dv.runtime_swap_blocker(game, state).startswith(
        "The Skyrim runtime swap is applied: the exe and the files it patches read 1.6.1170 "
        "while the rest of the game is 1.7.104")


def test_a_stale_runtime_record_does_not_block_a_build_switch(tmp_path):
    """The real case: a record from three weeks ago still says applied, and
    its target version happens to match the exe. Its files are long gone, so
    it must not stand in the way."""
    from tests.test_pe_version import build_pe
    game, state = _skyrim_on(tmp_path, (1, 6, 1170, 0))
    _runtime_record(state, game, frm="1.7.104.0", to="1.6.1170.0",
                    patched={"SkyrimSELauncher.exe": b"a launcher from weeks ago"})
    # Switching puts a real 1.6.1170 exe in place, which matches the record's
    # target version -- the thing that used to be mistaken for "still applied".
    assert dv.swap_to(game, state, "1.6.1170") == "1.6.1170"
    from Utils.wizard_support.pe_version import format_version, read_file_version
    assert format_version(read_file_version(game / "SkyrimSE.exe")) == "1.6.1170.0"
    assert dv.runtime_swap_blocker(game, state) == ""
    assert dv.swap_to(game, state, "Steam build 24914197") == "Steam build 24914197"


def test_other_games_are_never_blocked_by_a_runtime_swap(tmp_path):
    game, dirs, entry, state = _setup(tmp_path)
    assert dv.runtime_swap_blocker(game, state) == ""
    dv.apply_version(game, entry, dirs, state, from_label="1.11.240")
    assert dv.revert_version(game, state) == "1.11.240"

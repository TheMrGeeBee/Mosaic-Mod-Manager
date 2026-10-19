"""Tests for GameFileSourceDownloader support: archives a modlist takes
from the user's own game install. The manifest entry below is verbatim
from A Dragonborn's Fate 8.8.26 (all 7 such entries in that list share
these State keys)."""
from __future__ import annotations

from Utils.wabbajack.downloaders.game_file_source import fetch_game_file, resolve_game_file
from Utils.wabbajack.wabbajack_hash import hash_bytes
from Utils.wabbajack.wabbajack_manifest import GameFileSourceState, parse_archive, parse_modlist
from Utils.wabbajack.wabbajack_preflight import check_game_files, check_sources

REAL_ENTRY = {
    "Hash": "0o3x9+jxQCI=",
    "Meta": "[General]\ngameName=SkyrimSpecialEdition\ngameFile=Data\\ccbgssse025-advdsgs.esm",
    "Name": "Data_ccbgssse025-advdsgs.esm",
    "Size": 812873,
    "State": {
        "$type": "GameFileSourceDownloader, Wabbajack.Lib",
        "Game": "SkyrimSpecialEdition",
        "GameFile": "Data\\ccbgssse025-advdsgs.esm",
        "GameVersion": "1.6.1170.0",
        "Hash": "0o3x9+jxQCI=",
    },
}


def _uses(*archives, under="mods\\M"):
    """A modlist whose files are built from *archives* (into *under*)."""
    return parse_modlist({"Archives": list(archives), "Directives": [
        {"$type": "FromArchive, Wabbajack.Lib", "To": f"{under}\\f{i}", "Hash": "x",
         "Size": 1, "ArchiveHashPath": [a["Hash"]]} for i, a in enumerate(archives)]})


def _state(game_file, data, version="1.6.1170.0"):
    return GameFileSourceState(game="SkyrimSpecialEdition", game_file=game_file,
                               game_version=version, hash=hash_bytes(data))


def test_real_entry_parses_as_game_file_source():
    archive = parse_archive(REAL_ENTRY)
    assert isinstance(archive.state, GameFileSourceState)
    assert archive.state.game_file == "Data\\ccbgssse025-advdsgs.esm"
    assert archive.state.game_version == "1.6.1170.0"
    assert archive.state.hash == archive.hash


def test_real_entry_no_longer_blocks_preflight():
    ml = parse_modlist({"Archives": [REAL_ENTRY]})
    [check] = check_sources(ml, nexus_premium=True, loverslab_logged_in=True)
    assert check.ok


def test_resolve_matches_case_insensitively(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "DAWNGUARD.ESM").write_bytes(b"x")
    assert resolve_game_file(tmp_path, "Data\\Dawnguard.esm") == tmp_path / "data" / "DAWNGUARD.ESM"
    assert resolve_game_file(tmp_path, "Data\\Dragonborn.esm") is None


def test_fetch_uses_the_file_in_place(tmp_path):
    data = b"DLC master bytes"
    (tmp_path / "Data").mkdir()
    game_file = tmp_path / "Data" / "Dawnguard.esm"
    game_file.write_bytes(data)
    state = _state("Data\\Dawnguard.esm", data)
    result = fetch_game_file(state, tmp_path, state.hash)
    assert result.success and result.file_path == game_file


def test_fetch_mismatch_names_expected_version_and_keeps_file(tmp_path):
    (tmp_path / "Data").mkdir()
    game_file = tmp_path / "Data" / "Update.esm"
    game_file.write_bytes(b"a different game version")
    state = _state("Data\\Update.esm", b"curator's copy")
    result = fetch_game_file(state, tmp_path, state.hash)
    assert not result.success
    assert "1.6.1170.0" in result.error
    assert game_file.read_bytes() == b"a different game version"


def test_fetch_missing_file_and_unset_game_root(tmp_path):
    state = _state("Data\\HearthFires.esm", b"x")
    assert "isn't in your game folder" in fetch_game_file(state, tmp_path, state.hash).error
    assert "install folder" in fetch_game_file(state, None, state.hash).error


def test_preflight_blocks_on_missing_game_files_with_version(tmp_path):
    (tmp_path / "Data").mkdir()
    (tmp_path / "Data" / "Dawnguard.esm").write_bytes(b"x" * REAL_ENTRY["Size"])
    present = dict(REAL_ENTRY, Hash="dawnguard",
                   State=dict(REAL_ENTRY["State"], GameFile="Data\\Dawnguard.esm"))
    ml = _uses(present, REAL_ENTRY)
    [check] = check_game_files(ml, tmp_path)
    assert not check.ok and check.blocking
    assert "Data\\ccbgssse025-advdsgs.esm" in check.detail
    assert "Dawnguard" not in check.detail
    assert "1.6.1170.0" in check.detail


def test_preflight_game_files_present_or_not_needed(tmp_path):
    (tmp_path / "Data").mkdir()
    (tmp_path / "Data" / "ccbgssse025-advdsgs.esm").write_bytes(b"x" * REAL_ENTRY["Size"])
    [check] = check_game_files(_uses(REAL_ENTRY), tmp_path)
    assert check.ok
    assert check_game_files(parse_modlist({"Archives": []}), tmp_path) == []
    assert check_game_files(_uses(REAL_ENTRY), None) == []


def test_preflight_blocks_when_game_files_are_a_different_version(tmp_path):
    """Real case (Wasteland Genesis AE): built from 1.11.221 game files, the
    user's differed -- caught by size before any download."""
    (tmp_path / "Data").mkdir()
    (tmp_path / "Fallout4.exe").write_bytes(b"x" * 10)
    (tmp_path / "Data" / "DLCCoast.esm").write_bytes(b"x" * 20)
    (tmp_path / "Data" / "Fallout4.esm").write_bytes(b"x" * 30)
    ml = _uses(_fo4("Fallout4.exe", 11), _fo4("Data\\DLCCoast.esm", 21),
               _fo4("Data\\Fallout4.esm", 30))
    [check] = check_game_files(ml, tmp_path)
    assert check.key == "game-version" and not check.ok and check.blocking
    assert "1.11.221.0" in check.detail and "2 of the 3" in check.detail
    assert "Fallout4.exe" in check.detail and "Fallout4.esm" not in check.detail


def _fo4(game_file, size):
    return {"Hash": game_file, "Name": game_file.replace("\\", "_"), "Size": size,
            "State": {"$type": "GameFileSourceDownloader, Wabbajack.Lib",
                      "Game": "Fallout4", "GameFile": game_file,
                      "GameVersion": "1.11.221.0", "Hash": game_file}}


def test_game_files_that_only_feed_skipped_parts_do_not_block(tmp_path):
    """Real case (Wasteland Genesis AE): every game file that differed only
    fed the list's Game Root\\ folder, which Mosaic doesn't install."""
    (tmp_path / "Data").mkdir()
    (tmp_path / "Data" / "DLCCoast.esm").write_bytes(b"x" * 20)
    ml = _uses(_fo4("Data\\DLCCoast.esm", 21), under="Game Root\\Data")
    assert check_game_files(ml, tmp_path) == []


def test_game_version_warning_does_not_block(tmp_path, monkeypatch):
    from Utils.wabbajack import wabbajack_preflight as pf
    from Utils.wizard_support import pe_version
    (tmp_path / "Fallout4.exe").write_bytes(b"exe")
    ml = _uses(_fo4("Fallout4.exe", 3), under="Game Root")
    monkeypatch.setattr(pe_version, "read_file_version", lambda p: (1, 10, 163, 0))
    [check] = pf.check_game_version(ml, tmp_path)
    assert check.key == "game-runtime" and not check.ok and not check.blocking
    assert "1.11.221.0" in check.detail and "1.10.163.0" in check.detail
    monkeypatch.setattr(pe_version, "read_file_version", lambda p: (1, 11, 221, 0))
    [check] = pf.check_game_version(ml, tmp_path)
    assert check.ok


def test_extender_built_for_an_older_exe_warns_even_when_game_files_match(tmp_path, monkeypatch):
    """Real case (LoreRim): game files from 1.7.104.0, SKSE for 1.6.1170."""
    from Utils.wabbajack import wabbajack_preflight as pf
    from Utils.wizard_support import pe_version
    (tmp_path / "SkyrimSE.exe").write_bytes(b"exe")
    exe = {"Hash": "exe", "Name": "SkyrimSE.exe", "Size": 3,
           "State": {"$type": "GameFileSourceDownloader, Wabbajack.Lib",
                     "Game": "SkyrimSpecialEdition", "GameFile": "SkyrimSE.exe",
                     "GameVersion": "1.7.104.0", "Hash": "exe"}}
    ml = _uses(exe, under="Stock Game")
    ml.directives.append(parse_modlist({"Directives": [
        {"$type": "InlineFile, Wabbajack.Lib", "To": "mods\\SKSE\\skse64_1_6_1170.dll",
         "Hash": "x", "Size": 1, "SourceDataID": "d"}]}).directives[0])
    monkeypatch.setattr(pe_version, "read_file_version", lambda p: (1, 7, 104, 0))
    [check] = pf.check_game_version(ml, tmp_path)
    assert not check.ok and not check.blocking and "1.6.1170" in check.title
    monkeypatch.setattr(pe_version, "read_file_version", lambda p: (1, 6, 1170, 0))
    # exe already switched to the extender's version: that is the right state
    [check] = pf.check_game_version(ml, tmp_path)
    assert check.ok


def test_resolve_prefers_the_parked_original_while_mods_are_deployed(tmp_path):
    """Deployed: Data/ has the mods' (cleaned) file, Data_Core/ the game's own."""
    (tmp_path / "Data").mkdir()
    (tmp_path / "Data" / "Dawnguard.esm").write_bytes(b"cleaned")
    (tmp_path / "Data_Core").mkdir()
    (tmp_path / "Data_Core" / "dawnguard.esm").write_bytes(b"original")
    assert resolve_game_file(tmp_path, "Data\\Dawnguard.esm").read_bytes() == b"original"
    (tmp_path / "Data_Core" / "dawnguard.esm").unlink()
    assert resolve_game_file(tmp_path, "Data\\Dawnguard.esm").read_bytes() == b"cleaned"

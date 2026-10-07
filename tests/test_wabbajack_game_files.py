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
    present = dict(REAL_ENTRY, State=dict(REAL_ENTRY["State"], GameFile="Data\\Dawnguard.esm"))
    ml = parse_modlist({"Archives": [present, REAL_ENTRY]})
    [check] = check_game_files(ml, tmp_path)
    assert not check.ok and check.blocking
    assert "Data\\ccbgssse025-advdsgs.esm" in check.detail
    assert "Dawnguard" not in check.detail
    assert "1.6.1170.0" in check.detail


def test_preflight_game_files_present_or_not_needed(tmp_path):
    (tmp_path / "Data").mkdir()
    (tmp_path / "Data" / "ccbgssse025-advdsgs.esm").write_bytes(b"x" * REAL_ENTRY["Size"])
    [check] = check_game_files(parse_modlist({"Archives": [REAL_ENTRY]}), tmp_path)
    assert check.ok
    assert check_game_files(parse_modlist({"Archives": []}), tmp_path) == []
    assert check_game_files(parse_modlist({"Archives": [REAL_ENTRY]}), None) == []


def test_preflight_blocks_when_game_files_are_a_different_version(tmp_path):
    """Real case (Wasteland Genesis AE): built from 1.11.221 game files, the
    user's differed -- caught by size before any download."""
    def entry(game_file, size):
        return {"Hash": "h", "Name": game_file.replace("\\", "_"), "Size": size,
                "State": {"$type": "GameFileSourceDownloader, Wabbajack.Lib",
                          "Game": "Fallout4", "GameFile": game_file,
                          "GameVersion": "1.11.221.0", "Hash": "h"}}
    (tmp_path / "Data").mkdir()
    (tmp_path / "Fallout4.exe").write_bytes(b"x" * 10)
    (tmp_path / "Data" / "DLCCoast.esm").write_bytes(b"x" * 20)
    (tmp_path / "Data" / "Fallout4.esm").write_bytes(b"x" * 30)
    ml = parse_modlist({"Archives": [entry("Fallout4.exe", 11), entry("Data\\DLCCoast.esm", 21),
                                     entry("Data\\Fallout4.esm", 30)]})
    [check] = check_game_files(ml, tmp_path)
    assert check.key == "game-version" and not check.ok and check.blocking
    assert "1.11.221.0" in check.detail and "2 of the 3" in check.detail
    assert "Fallout4.exe" in check.detail and "Fallout4.esm" not in check.detail

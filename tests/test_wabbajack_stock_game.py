"""A Wabbajack list's own game folder ("Stock Game") built as a game copy.

Real case (LoreRim 5.0.4.8, 2026-11-24): its Stock Game/ holds 33 files built
from the 1.7.104 game files, including a patched SkyrimSE.exe (-> 1.6.1170, the
version its SKSE needs), cleaned masters and rebuilt vanilla BSAs. Mosaic used
to skip the folder and tell the user to downgrade Steam's exe instead."""
from __future__ import annotations

import json

from test_wabbajack_install_pipeline import FakeGame, _build, _fake_http, _inline

from Utils import game_copy as gc
from Utils.profile.profile_state import read_profile_settings
from Utils.wabbajack import wabbajack_install as wi
from Utils.wabbajack import wabbajack_preflight as pf
from Utils.wabbajack import wabbajack_stock_game as sg
from Utils.wabbajack.wabbajack_hash import hash_bytes
from Utils.wabbajack.wabbajack_manifest import parse_modlist

MASTER = b"DAWNGUARD MASTER"


def _stock_extra(_a_hash):
    m_hash = hash_bytes(MASTER)
    return [
        _inline("Stock Game\\SkyrimSE.exe", "exe", size=10),
        {"$type": "FromArchive, Wabbajack.Lib", "To": "Stock Game\\Data\\Dawnguard.esm",
         "Hash": m_hash, "Size": len(MASTER), "ArchiveHashPath": [m_hash]},
        {"$type": "CreateBSA, Wabbajack.Lib", "To": "Stock Game\\Data\\Skyrim - Shaders.bsa",
         "Hash": "curator==", "Size": 1, "TempID": "shaders1",
         "State": {"$type": "BSAState, Compression.BSA", "Magic": "BSA\u0000",
                   "Version": 105, "ArchiveFlags": 0x7, "FileFlags": 0x3},
         "FileStates": [{"$type": "BSAFileState, Compression.BSA", "Path": "shaders\\a.fx",
                         "Index": 0, "FlipCompression": False}]},
        {"$type": "FromArchive, Wabbajack.Lib", "To": "TEMP_BSA_FILES\\shaders1\\shaders\\a.fx",
         "Hash": m_hash, "Size": len(MASTER), "ArchiveHashPath": [m_hash]},
    ]


class SkyrimLike(FakeGame):
    name = "Skyrim Special Edition"
    exe_name = "SkyrimSE.exe"
    supports_game_copy = True

    def __init__(self, staging, steam):
        super().__init__(staging)
        self.steam = steam

    def get_steam_install_path(self):
        return self.steam

    def get_game_path(self):
        return self.steam


def _list_with_stock_game(tmp_path):
    """The pipeline test's list plus a Stock Game/ built from a game file."""
    import zipfile
    wj, modlist, src = _build(tmp_path, extra=_stock_extra)
    with zipfile.ZipFile(wj, "a") as zf:
        zf.writestr("exe", b"PATCHED EX")
    m_hash = hash_bytes(MASTER)
    modlist.archives.append(parse_modlist({"Archives": [{
        "Hash": m_hash, "Name": "Data_Dawnguard.esm", "Size": len(MASTER),
        "State": {"$type": "GameFileSourceDownloader, Wabbajack.Lib",
                  "Game": "SkyrimSpecialEdition", "GameFile": "Data\\Dawnguard.esm",
                  "GameVersion": "1.7.104.0", "Hash": m_hash}}]}).archives[0])
    steam = tmp_path / "lib" / "steamapps" / "common" / "Skyrim Special Edition"
    (steam / "Data").mkdir(parents=True)
    (steam / "Data" / "Dawnguard.esm").write_bytes(MASTER)
    return wj, modlist, src, steam


def _install(tmp_path, monkeypatch, wj, modlist, src, steam, dest, logs=None):
    monkeypatch.setattr(wi, "resolve_downloader", lambda state: _fake_http(src))
    monkeypatch.setattr(wi, "_rebuild_index", lambda *a: None)
    staging = tmp_path / "staging" / "mods"
    profile = tmp_path / "staging" / "profiles" / "LoreRim"
    profile.mkdir(parents=True, exist_ok=True)
    report = wi.run_wabbajack_install(
        wabbajack_path=wj, modlist=modlist, game=SkyrimLike(staging, steam),
        profile_dir=profile, download_dir=tmp_path / "downloads", stock_game_dest=dest,
        callbacks=wi.WabbajackInstallCallbacks(on_log=(logs if logs is not None else []).append))
    return report, profile


def test_finds_the_folder_with_the_game_exe(tmp_path):
    _wj, modlist, _src, _steam = _list_with_stock_game(tmp_path)
    assert sg.stock_game_dir(modlist, "SkyrimSE.exe") == "Stock Game"
    assert sg.stock_game_dir(modlist, "skyrimse.EXE") == "Stock Game"
    assert sg.stock_game_dir(modlist, "Fallout4.exe") is None


def test_found_and_versioned_by_the_build_exe_not_the_launcher(tmp_path, monkeypatch):
    """Real case: Mosaic launches Skyrim via SkyrimSELauncher.exe, which LoreRim
    replaces with SKSE's loader -- its version (0.2.2.6) got recorded as the
    copy's build. The version table's SkyrimSE.exe names the build."""
    wj, modlist, src, steam = _list_with_stock_game(tmp_path)
    monkeypatch.setattr(SkyrimLike, "exe_name", "SkyrimSELauncher.exe")
    monkeypatch.setattr(gc, "version_exe", lambda game: "SkyrimSE.exe")
    read = []
    monkeypatch.setattr(gc, "_exe_version", lambda root, exe: read.append(exe) or "1.6.1170")
    assert sg.game_stock_dir(modlist, SkyrimLike(tmp_path, steam)) == "Stock Game"
    dest = tmp_path / "Modlists" / "LoreRim" / "Stock Game"
    report, _ = _install(tmp_path, monkeypatch, wj, modlist, src, steam, dest)
    assert report.ok and read == ["SkyrimSE.exe"]
    assert gc.read_marker(dest).version == "1.6.1170"


def test_builds_the_stock_game_as_a_copy_and_points_the_profile_at_it(tmp_path, monkeypatch):
    wj, modlist, src, steam = _list_with_stock_game(tmp_path)
    dest = tmp_path / "Modlists" / "LoreRim (Mosaic)" / "Stock Game"
    report, profile = _install(tmp_path, monkeypatch, wj, modlist, src, steam, dest)
    assert report.ok, report.failed_directives
    assert (dest / "SkyrimSE.exe").read_bytes() == b"PATCHED EX"
    assert (dest / "Data" / "Dawnguard.esm").read_bytes() == MASTER
    assert (dest / "Data" / "Skyrim - Shaders.bsa").is_file()
    info = gc.read_marker(dest)
    assert info.build == "Wabbajack: Test List 1.2" and info.method == "wabbajack"
    assert report.stock_game == str(dest)
    assert read_profile_settings(profile)["game_path"] == str(dest)
    assert not any(u.startswith("Stock Game") for u in report.unplaced_files)
    assert not gc.partial_dir(dest).exists()
    assert (steam / "Data" / "Dawnguard.esm").read_bytes() == MASTER     # Steam untouched


def test_without_a_destination_the_folder_is_skipped_as_before(tmp_path, monkeypatch):
    wj, modlist, src, steam = _list_with_stock_game(tmp_path)
    report, profile = _install(tmp_path, monkeypatch, wj, modlist, src, steam, None)
    assert report.ok and not report.stock_game
    assert sum(u.startswith("Stock Game/") for u in report.unplaced_files) == 3
    assert "game_path" not in read_profile_settings(profile)


def test_same_list_version_reuses_the_copy(tmp_path, monkeypatch):
    wj, modlist, src, steam = _list_with_stock_game(tmp_path)
    dest = tmp_path / "Modlists" / "LoreRim" / "Stock Game"
    _install(tmp_path, monkeypatch, wj, modlist, src, steam, dest)
    (dest / "SkyrimSE.exe").write_bytes(b"untouched")
    logs = []
    report, profile = _install(tmp_path, monkeypatch, wj, modlist, src, steam, dest, logs)
    assert report.ok and (dest / "SkyrimSE.exe").read_bytes() == b"untouched"
    assert any("already built" in m for m in logs)
    assert read_profile_settings(profile)["game_path"] == str(dest)


def test_a_new_list_version_replaces_the_old_copy(tmp_path, monkeypatch):
    wj, modlist, src, steam = _list_with_stock_game(tmp_path)
    dest = tmp_path / "Modlists" / "LoreRim" / "Stock Game"
    _install(tmp_path, monkeypatch, wj, modlist, src, steam, dest)
    (dest / "stale.txt").write_text("from the old version")
    modlist.version = "1.3"
    report, _ = _install(tmp_path, monkeypatch, wj, modlist, src, steam, dest)
    assert report.ok and not (dest / "stale.txt").exists()
    assert gc.read_marker(dest).build == "Wabbajack: Test List 1.3"


def test_a_failed_game_folder_file_installs_no_copy(tmp_path, monkeypatch):
    wj, modlist, src, steam = _list_with_stock_game(tmp_path)
    (steam / "Data" / "Dawnguard.esm").write_bytes(b"a different master!")
    dest = tmp_path / "Modlists" / "LoreRim" / "Stock Game"
    report, profile = _install(tmp_path, monkeypatch, wj, modlist, src, steam, dest)
    assert not report.ok and not report.stock_game
    assert not dest.exists() and not gc.partial_dir(dest).exists()
    assert "game_path" not in read_profile_settings(profile)


def test_never_replaces_a_folder_mosaic_did_not_make(tmp_path, monkeypatch):
    wj, modlist, src, steam = _list_with_stock_game(tmp_path)
    dest = tmp_path / "Modlists" / "LoreRim" / "Stock Game"
    dest.mkdir(parents=True)
    (dest / "SkyrimSE.exe").write_bytes(b"MO2's own")
    report, profile = _install(tmp_path, monkeypatch, wj, modlist, src, steam, dest)
    assert not report.stock_game and (dest / "SkyrimSE.exe").read_bytes() == b"MO2's own"
    assert "game_path" not in read_profile_settings(profile)


def test_destination_avoids_the_lists_own_mo2_install(tmp_path, monkeypatch):
    _wj, modlist, _src, steam = _list_with_stock_game(tmp_path)
    monkeypatch.setattr(gc, "_prefs_file", lambda _n: tmp_path / "cfg" / "game_copies.json")
    (tmp_path / "cfg").mkdir()
    game = SkyrimLike(tmp_path / "staging", steam)
    modlists = tmp_path / "lib" / "Modlists"
    assert sg.stock_game_destination(game, modlist) == modlists / "Test List" / "Stock Game"
    mo2 = modlists / "Test List" / "Stock Game"
    mo2.mkdir(parents=True)
    (mo2 / "SkyrimSE.exe").write_bytes(b"MO2")
    assert sg.stock_game_destination(game, modlist) == (
        modlists / "Test List (Mosaic)" / "Stock Game")
    mine = modlists / "Test List (Mosaic)" / "Stock Game"
    mine.mkdir(parents=True)
    (mine / gc.MARKER_NAME).write_text(json.dumps({"build": "old"}))
    assert sg.stock_game_destination(game, modlist) == mine      # Mosaic's own: reused


def test_preflight_no_longer_asks_to_switch_the_exe(tmp_path):
    _wj, modlist, _src, steam = _list_with_stock_game(tmp_path)
    dest = tmp_path / "Modlists" / "LoreRim" / "Stock Game"
    (check,) = pf.check_game_version(modlist, steam, stock_game_dest=dest)
    assert check.ok and str(dest) in check.detail


def test_preflight_checks_the_game_files_the_copy_is_built_from(tmp_path):
    _wj, modlist, _src, steam = _list_with_stock_game(tmp_path)
    (steam / "Data" / "Dawnguard.esm").write_bytes(b"a different master!")
    assert pf.check_game_files(modlist, steam) == []      # not built: not checked
    checks = pf.check_game_files(modlist, steam, stock_dir="Stock Game")
    assert checks and not checks[0].ok

"""SKSE64 only works with the exact game runtime it was built for; a mismatch
launches the game and silently does nothing. The post-install audit finds the SKSE
DLL in the profile (its name encodes the runtime) and says so."""
from __future__ import annotations

from Utils.collections import collection_audit as au


def _mods(tmp_path, layout: dict[str, list[str]]):
    for mod, files in layout.items():
        d = tmp_path / mod
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_text("x")
    return tmp_path


def test_finds_the_skse_dll_in_enabled_mods_only(tmp_path):
    mods = _mods(tmp_path, {
        "SKSE64": ["skse64_1_6_1170.dll", "skse64_loader.exe"],
        "Old SKSE": ["skse64_1_5_97.dll"],
        "Unrelated": ["skse64_loader.exe", "plugin.esp"],
    })
    found = au.find_skse_runtimes(mods, ["SKSE64", "Unrelated", "Missing Mod"])
    assert found == {(1, 6, 1170): ["SKSE64"]}                        # 'Old SKSE' is disabled


def test_only_top_level_files_of_a_mod_count(tmp_path):
    mods = _mods(tmp_path, {"Mod": []})
    (mods / "Mod" / "Data").mkdir()
    (mods / "Mod" / "Data" / "skse64_1_6_1170.dll").write_text("x")
    assert au.find_skse_runtimes(mods, ["Mod"]) == {}


def test_extra_dirs_such_as_root_folder_are_scanned(tmp_path):
    (tmp_path / "Root_Folder").mkdir()
    (tmp_path / "Root_Folder" / "SKSE64_1_6_640.DLL").write_text("x")
    assert au.find_skse_runtimes(tmp_path / "mods", [], [tmp_path / "Root_Folder"]) == \
        {(1, 6, 640): ["Root_Folder"]}


def test_matching_runtime_is_fine():
    assert au.check_skse_runtime((1, 6, 1170, 0), {(1, 6, 1170): ["SKSE64"]}) is None


def test_mismatch_names_both_versions_and_the_consequence():
    text = au.check_skse_runtime((1, 7, 104, 0), {(1, 6, 1170): ["SKSE64"]})
    assert "1.6.1170" in text and "1.7.104.0" in text and "silently" in text


def test_any_matching_dll_among_several_is_enough():
    assert au.check_skse_runtime((1, 6, 1170, 0), {(1, 5, 97): ["a"], (1, 6, 1170): ["b"]}) is None


def test_no_skse_or_an_unreadable_exe_says_nothing():
    assert au.check_skse_runtime((1, 7, 104, 0), {}) is None
    assert au.check_skse_runtime(None, {(1, 6, 1170): ["SKSE64"]}) is None


# ---- wiring: the collection install's end-of-run audit ---------------------------

from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

from pe_builder import build_pe  # noqa: E402
from Utils.collections.collection_install import _audit_profile  # noqa: E402


@dataclass
class _Game:
    root: Path
    steam_id: str = "489830"

    def get_game_path(self):
        return self.root


def _profile(tmp_path, exe_version, dll="skse64_1_6_1170.dll", enabled=True):
    game_root = tmp_path / "game"
    game_root.mkdir()
    (game_root / "SkyrimSE.exe").write_bytes(build_pe(exe_version))
    profile = tmp_path / "profile"
    mods = profile / "mods"
    (mods / "SKSE64").mkdir(parents=True)
    (mods / "SKSE64" / dll).write_text("x")
    (profile / "modlist.txt").write_text(("+" if enabled else "-") + "SKSE64\n+Separator_separator\n")
    return _Game(game_root), profile, mods


def test_audit_warns_when_skse_does_not_match_the_installed_exe(tmp_path):
    game, profile, mods = _profile(tmp_path, (1, 7, 104, 0))
    (warning,) = _audit_profile(game, profile, profile / "modlist.txt", mods)
    assert "1.6.1170" in warning and "1.7.104.0" in warning


def test_audit_is_quiet_when_they_match(tmp_path):
    game, profile, mods = _profile(tmp_path, (1, 6, 1170, 0))
    assert _audit_profile(game, profile, profile / "modlist.txt", mods) == []


def test_audit_ignores_a_disabled_skse_mod(tmp_path):
    game, profile, mods = _profile(tmp_path, (1, 7, 104, 0), enabled=False)
    assert _audit_profile(game, profile, profile / "modlist.txt", mods) == []


def test_audit_only_applies_to_skyrim_se(tmp_path):
    game, profile, mods = _profile(tmp_path, (1, 7, 104, 0))
    game.steam_id = "377160"
    assert _audit_profile(game, profile, profile / "modlist.txt", mods) == []


def test_audit_never_breaks_an_install(tmp_path):
    game, profile, mods = _profile(tmp_path, (1, 7, 104, 0))
    logs = []
    assert _audit_profile(game, profile, tmp_path / "no" / "modlist.txt", mods, logs.append) == []
    assert _audit_profile(object(), profile, profile / "modlist.txt", None, logs.append) == []

"""Adding a custom exe must never fail silently.

Real-world motivation (2026-09-19): "+ Add custom EXE…" was used twice on
Fallout4.exe and the Run dropdown just stayed on the default entry. Nothing was
written anywhere and nothing was reported. The list lives in the active
profile's ``profile_state.json``, and ``add_custom_exe`` quietly did nothing
when the game object had no active profile dir.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from Utils.exe_launch.exe_launch import add_custom_exe, load_custom_exes, save_custom_exes


def _game(profile_dir):
    return SimpleNamespace(_active_profile_dir=profile_dir)


def _exe(tmp_path, name="Fallout4.exe"):
    p = tmp_path / name
    p.write_bytes(b"MZ")
    return p


def _profile(tmp_path):
    pdir = tmp_path / "profiles" / "Test"
    pdir.mkdir(parents=True)
    return pdir


def test_add_persists_and_reloads(tmp_path):
    pdir, exe = _profile(tmp_path), _exe(tmp_path)
    assert add_custom_exe(_game(pdir), exe) is True
    assert load_custom_exes(_game(pdir)) == [exe]
    assert json.loads((pdir / "profile_state.json").read_text())["custom_exes"] == [str(exe)]


def test_adding_the_same_exe_twice_keeps_one_entry(tmp_path):
    pdir, exe = _profile(tmp_path), _exe(tmp_path)
    assert add_custom_exe(_game(pdir), exe) is True
    assert add_custom_exe(_game(pdir), exe) is True
    assert load_custom_exes(_game(pdir)) == [exe]


def test_no_active_profile_reports_failure_and_writes_nothing(tmp_path):
    exe = _exe(tmp_path)
    assert add_custom_exe(_game(None), exe) is False
    assert save_custom_exes(_game(None), [exe]) is False
    assert not list(tmp_path.rglob("profile_state.json"))


def test_add_leaves_the_rest_of_the_profile_state_alone(tmp_path):
    pdir, exe = _profile(tmp_path), _exe(tmp_path)
    (pdir / "profile_state.json").write_text(json.dumps(
        {"selected_exe": "f4se_loader.exe", "profile_settings": {"deploy_mode": "symlink"}}))
    assert add_custom_exe(_game(pdir), exe) is True
    state = json.loads((pdir / "profile_state.json").read_text())
    assert state["selected_exe"] == "f4se_loader.exe"
    assert state["profile_settings"] == {"deploy_mode": "symlink"}
    assert state["custom_exes"] == [str(exe)]

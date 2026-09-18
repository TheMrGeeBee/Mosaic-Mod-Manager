"""Routing coverage for The Blood of Dawnwalker, added when the game was
promoted from a custom-game JSON definition to a real Games/ module.

The new rules close gaps found by diffing the previous JSON's routing table
against the actual Vortex extension for this game (dwmapi.dll/xinput proxy
UE4SS loader, ReShade, generic loose .ini/.dll fallbacks, and bare .lua
mods with no Scripts/ wrapper) — none of these were things a currently
installed mod had hit yet, but the real extension's table confirms they're
real packaging shapes Nexus mods for this game use.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "blood_of_dawnwalker",
    Path(__file__).resolve().parent.parent / "src" / "Games" /
    "The Blood of Dawnwalker" / "blood_of_dawnwalker.py",
)
_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_module)
BloodOfDawnwalker = _module.BloodOfDawnwalker


def _resolve(entries):
    game = BloodOfDawnwalker()
    resolved = game._resolve_filemap_entries(entries)
    return {staged: (dest, final) for staged, _mod, dest, final in resolved}


def test_ue4ss_injector_proxy_dlls_land_in_binaries():
    mod = "Some UE4SS Loader Package"
    entries = [("dwmapi.dll", mod), ("xinput1_4.dll", mod)]
    resolved = _resolve(entries)
    assert resolved["dwmapi.dll"] == ("Binaries/Win64", "dwmapi.dll")
    assert resolved["xinput1_4.dll"] == ("Binaries/Win64", "xinput1_4.dll")


def test_bare_lua_with_no_scripts_folder_still_reaches_ue4ss_mods():
    mod = "Bare Lua Mod"
    entries = [("main.lua", mod)]
    resolved = _resolve(entries)
    dest, final = resolved["main.lua"]
    assert dest == "Binaries/Win64/ue4ss/Mods"
    assert final == "Bare Lua Mod/main.lua"


def test_reshade_ini_and_loader_dll_land_flat_in_binaries():
    mod = "Some ReShade Preset"
    entries = [
        ("SomePresetFolder/ReShade.ini", mod),
        ("SomePresetFolder/dxgi.dll", mod),
    ]
    resolved = _resolve(entries)
    assert resolved["SomePresetFolder/ReShade.ini"] == ("Binaries/Win64", "ReShade.ini")
    assert resolved["SomePresetFolder/dxgi.dll"] == ("Binaries/Win64", "dxgi.dll")


def test_reshade_shaders_folder_keeps_its_name():
    mod = "Some ReShade Preset"
    entries = [("reshade-shaders/Shaders/Foo.fx", mod)]
    resolved = _resolve(entries)
    dest, final = resolved["reshade-shaders/Shaders/Foo.fx"]
    assert dest == "Binaries/Win64"
    assert final == "reshade-shaders/Shaders/Foo.fx"


def test_loose_ini_fallback_does_not_steal_scripts_sibling_ini():
    """A .ini that rides along with a Scripts/ mod (e.g. mod_settings.ini)
    must still be claimed by the earlier scripts/dlls sibling-drag rule,
    not the later loose_only .ini fallback."""
    mod = "Combat Camera - Configurable"
    entries = [
        ("Data/CombatCamera/Scripts/main.lua", mod),
        ("Data/CombatCamera/mod_settings.ini", mod),
    ]
    resolved = _resolve(entries)
    dest, final = resolved["Data/CombatCamera/mod_settings.ini"]
    assert dest == "Binaries/Win64/ue4ss/Mods"
    assert final == "CombatCamera/mod_settings.ini"


def test_loose_top_level_ini_fallback_reaches_binaries():
    mod = "Standalone Preset Ini"
    entries = [("presetsettings.ini", mod)]
    resolved = _resolve(entries)
    assert resolved["presetsettings.ini"] == ("Binaries/Win64", "presetsettings.ini")


def test_generic_leftover_dll_fallback_reaches_binaries():
    mod = "Some Native Plugin"
    entries = [("SomePlugin.dll", mod)]
    resolved = _resolve(entries)
    assert resolved["SomePlugin.dll"] == ("Binaries/Win64", "SomePlugin.dll")


def test_combat_camera_real_layout_lands_unwrapped():
    """End-to-end regression for the reported bug, through the real shipped
    module (not just the isolated UE5Game stub test)."""
    mod = "Combat Camera - Configurable"
    entries = [
        ("Data/CombatCamera/Scripts/main.lua", mod),
        ("Data/CombatCamera/dlls/main.dll", mod),
        ("Data/CombatCamera/mod_settings.ini", mod),
        ("Data/CombatCamera/enabled.txt", mod),
    ]
    resolved = _resolve(entries)
    for staged, (dest, final) in resolved.items():
        assert dest == "Binaries/Win64/ue4ss/Mods", staged
        assert final.startswith("CombatCamera/"), (staged, final)

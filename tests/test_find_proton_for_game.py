"""find_proton_for_game() must find the Proton that actually built a prefix.

Real-world motivation: a Fallout 4 prefix created by Steam with the system
``proton-cachyos-slr`` (no per-game CompatToolMapping entry — Steam used its
default tool) records its tool in ``compatdata/<id>/config_info`` as
``/run/host/usr/share/steam/compatibilitytools.d/proton-cachyos-slr/...``.
``/run/host`` only exists inside SteamLinuxRuntime's sandbox, so the lookup
returned None and Run EXE silently fell back to an arbitrary Proton
(GE-Proton11-7) — a different one than the prefix was built with.

Lookup order: per-game mapping -> config_info (the tool that really built the
prefix) -> Steam's default-tool mapping (key "0").
"""
from __future__ import annotations

from pathlib import Path

import pytest

from Utils.wine_proton import steam_finder

APPID = "377160"


@pytest.fixture
def steam(tmp_path, monkeypatch):
    root = tmp_path / "Steam"
    (root / "config").mkdir(parents=True)
    (root / "compatibilitytools.d").mkdir()
    (root / "steamapps" / "compatdata" / APPID).mkdir(parents=True)
    monkeypatch.setattr(steam_finder, "_STEAM_CANDIDATES", [root])
    return root


def _add_tool(parent: Path, name: str) -> Path:
    tool = parent / name
    tool.mkdir(parents=True)
    script = tool / "proton"
    script.write_text("#!/usr/bin/env python3\n")
    return script


def _write_mapping(root: Path, entries: dict[str, str], trailer: str = "") -> None:
    body = "".join(
        f'\t\t\t\t\t"{k}"\n\t\t\t\t\t{{\n\t\t\t\t\t\t"name"\t\t"{v}"\n'
        f'\t\t\t\t\t\t"config"\t\t""\n\t\t\t\t\t\t"priority"\t\t"250"\n\t\t\t\t\t}}\n'
        for k, v in entries.items())
    (root / "config" / "config.vdf").write_text(
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
        '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n'
        + body + "\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n" + trailer + "}\n")


def _write_config_info(root: Path, *lines: str) -> None:
    (root / "steamapps" / "compatdata" / APPID / "config_info").write_text(
        "\n".join(lines) + "\n")


# ---- _strip_run_host -------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("/run/host/usr/share/steam/compatibilitytools.d",
     "/usr/share/steam/compatibilitytools.d"),
    ("/run/host", "/"),
    ("/usr/share/steam/compatibilitytools.d", "/usr/share/steam/compatibilitytools.d"),
    ("/run/hostile/x", "/run/hostile/x"),
])
def test_strip_run_host(raw, expected):
    assert steam_finder._strip_run_host(raw) == expected


# ---- config_info with sandbox-only paths -----------------------------------

def test_run_host_config_info_resolves_to_the_host_path(steam, tmp_path):
    system = tmp_path / "usr" / "share" / "steam" / "compatibilitytools.d"
    expected = _add_tool(system, "proton-cachyos-slr")
    _write_config_info(
        steam,
        f"/run/host{system}/proton-cachyos-slr/files/share/fonts/",
        f"/run/host{system}/proton-cachyos-slr/files/lib/",
        str(steam),
    )
    assert steam_finder.find_proton_for_game(APPID) == expected


def test_run_host_config_info_falls_back_to_installed_tool_by_name(steam):
    # The recorded system path doesn't exist on this host, but the same tool is
    # installed under the user's Steam compatibilitytools.d.
    installed = _add_tool(steam / "compatibilitytools.d", "proton-cachyos-slr")
    _write_config_info(
        steam,
        "/run/host/nonexistent/compatibilitytools.d/proton-cachyos-slr/files/lib/",
    )
    assert steam_finder.find_proton_for_game(APPID) == installed


def test_unknown_tool_in_config_info_is_none(steam):
    _write_config_info(
        steam, "/run/host/nowhere/compatibilitytools.d/proton-ghost/files/lib/")
    assert steam_finder.find_proton_for_game(APPID) is None


# ---- mapping precedence -----------------------------------------------------

def test_per_game_mapping_is_used(steam):
    tool = _add_tool(steam / "compatibilitytools.d", "GE-Proton-x")
    _write_mapping(steam, {APPID: "GE-Proton-x"})
    assert steam_finder.find_proton_for_game(APPID) == tool


def test_default_tool_mapping_used_when_nothing_more_specific(steam):
    tool = _add_tool(steam / "compatibilitytools.d", "proton-cachyos-slr")
    _write_mapping(steam, {"0": "proton-cachyos-slr", "1466060": "other"})
    assert steam_finder.find_proton_for_game(APPID) == tool


def test_per_game_mapping_beats_default(steam):
    _add_tool(steam / "compatibilitytools.d", "tool-default")
    specific = _add_tool(steam / "compatibilitytools.d", "tool-specific")
    _write_mapping(steam, {"0": "tool-default", APPID: "tool-specific"})
    assert steam_finder.find_proton_for_game(APPID) == specific


def test_config_info_beats_default_mapping(steam):
    """The tool that built the prefix wins over whatever the default is now."""
    # config_info only trusts dirs named proton*/ge-proton* (existing filter).
    built_with = _add_tool(steam / "compatibilitytools.d", "proton-built")
    _add_tool(steam / "compatibilitytools.d", "proton-default")
    _write_mapping(steam, {"0": "proton-default"})
    _write_config_info(
        steam, f"{steam / 'compatibilitytools.d'}/proton-built/files/lib/")
    assert steam_finder.find_proton_for_game(APPID) == built_with


def test_zero_key_outside_the_mapping_block_is_ignored(steam):
    """A stray "0" { "name" ... } elsewhere in config.vdf is not the default tool."""
    _add_tool(steam / "compatibilitytools.d", "junk")
    _write_mapping(
        steam, {"1466060": "other"},
        trailer='\t"Unrelated"\n\t{\n\t\t"0"\n\t\t{\n\t\t\t"name"\t\t"junk"\n\t\t}\n\t}\n')
    assert steam_finder.find_proton_for_game(APPID) is None


def test_nothing_known_returns_none(steam):
    assert steam_finder.find_proton_for_game(APPID) is None
    assert steam_finder.find_proton_for_game("") is None

"""ensure_prefix_deps() must reinstall a game's Proton-prefix dependencies
(vcredist / d3dcompiler_47) whenever they're actually missing from the
CURRENT prefix, and skip cheaply when already present — the same
"reapply, no-op if already correct" contract already used for Wine DLL
overrides (deploy_game_wine_dll_overrides).

Real-world motivation: this function previously only ran once, at
configure-game-save time. A Proton prefix recreated afterward (a fresh
Steam Proton prefix, "Clear local Proton data", a manually deleted
compatdata folder) silently lost vcredist with no prompt to reinstall it —
surfaced as RED4ext/Cyber Engine Tweaks on Cyberpunk 2077 both crashing
identically deep inside a ~6-year-old msvcp140.dll that Proton's own
fresh-prefix defaults ship, with no obvious link back to "reinstall
vcredist". Now called on every deploy (see deploy_pipeline.py) instead.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from Utils.wine_proton.protontricks import (
    VCREDIST_DEP_KEY,
    D3D_DEP_KEY,
    ensure_prefix_deps,
    mark_dep_installed,
)


class _Game:
    def __init__(self, name="TestGame", auto_install_deps=None,
                winetricks_components=None):
        self.name = name
        self.auto_install_deps = auto_install_deps or []
        self.winetricks_components = winetricks_components or []


@pytest.fixture
def prefix(tmp_path):
    """A fake prefix dir nested under tmp_path. is_dep_installed/
    mark_dep_installed key their marker file off prefix.parent (not
    prefix itself) — pytest's tmp_path fixtures for different tests in the
    same session share a common parent directory, so passing tmp_path
    directly as the "prefix" would leak the marker file across tests.
    Nesting one level down makes prefix.parent (== tmp_path) unique per
    test again."""
    p = tmp_path / "pfx"
    p.mkdir()
    return p


def test_no_declared_deps_is_a_no_op(prefix):
    game = _Game()
    result = ensure_prefix_deps(game, prefix)
    assert result == {"installed": [], "skipped": [], "failed": []}


def test_skips_vcredist_already_installed(prefix):
    mark_dep_installed(prefix, VCREDIST_DEP_KEY)
    game = _Game(auto_install_deps=["vcredist"])
    with patch("Utils.wine_proton.protontricks.install_vcredist") as mock_install:
        result = ensure_prefix_deps(game, prefix)
    mock_install.assert_not_called()
    assert result == {"installed": [], "skipped": ["vcredist"], "failed": []}


def test_installs_missing_vcredist_on_a_fresh_prefix(prefix):
    """The exact real-world case: a prefix with no dep marker at all (as if
    just recreated) must trigger a real reinstall, not a silent skip."""
    game = _Game(auto_install_deps=["vcredist"])
    with patch("Utils.wine_proton.protontricks.build_proton_env_for_game",
               return_value=(prefix / "proton", {"FAKE": "1"})), \
         patch("Utils.wine_proton.protontricks.install_vcredist",
               return_value=True) as mock_install:
        result = ensure_prefix_deps(game, prefix)
    mock_install.assert_called_once()
    assert result == {"installed": ["vcredist"], "skipped": [], "failed": []}


def test_skips_vcredist_when_no_proton_available(prefix):
    game = _Game(auto_install_deps=["vcredist"])
    with patch("Utils.wine_proton.protontricks.build_proton_env_for_game",
               return_value=(None, None)), \
         patch("Utils.wine_proton.protontricks.install_vcredist") as mock_install:
        result = ensure_prefix_deps(game, prefix)
    mock_install.assert_not_called()
    assert result == {"installed": [], "skipped": ["vcredist"], "failed": []}


def test_records_failed_install(prefix):
    game = _Game(auto_install_deps=["vcredist"])
    with patch("Utils.wine_proton.protontricks.build_proton_env_for_game",
               return_value=(prefix / "proton", {})), \
         patch("Utils.wine_proton.protontricks.install_vcredist",
               return_value=False):
        result = ensure_prefix_deps(game, prefix)
    assert result == {"installed": [], "skipped": [], "failed": ["vcredist"]}


def test_d3dcompiler_and_vcredist_are_independent(prefix):
    """Already having ONE dep installed must not skip the other."""
    mark_dep_installed(prefix, VCREDIST_DEP_KEY)
    game = _Game(auto_install_deps=["vcredist", "d3dcompiler_47"])
    with patch("Utils.wine_proton.protontricks.install_d3dcompiler_47",
               return_value=True) as mock_d3d, \
         patch("Utils.wine_proton.protontricks.install_vcredist") as mock_vc:
        result = ensure_prefix_deps(game, prefix)
    mock_vc.assert_not_called()
    mock_d3d.assert_called_once()
    assert result == {"installed": ["d3dcompiler_47"], "skipped": ["vcredist"], "failed": []}


def test_unknown_dep_is_skipped_not_crashed(prefix):
    game = _Game(auto_install_deps=["something_unheard_of"])
    result = ensure_prefix_deps(game, prefix)
    assert result == {"installed": [], "skipped": ["something_unheard_of"], "failed": []}

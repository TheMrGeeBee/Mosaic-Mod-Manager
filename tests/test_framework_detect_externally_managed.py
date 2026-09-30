"""Test for Utils.wine_proton.framework_detect.detect_frameworks' handling
of game.externally_managed_frameworks.

"Yet Another BG3 Native Mod Loader" is a standalone injector that, by
design, never places any file under the game install directory -- the
framework banner's file-existence check can never confirm it no matter what
path is checked. externally_managed_frameworks lets the user assert directly
that a given framework label is handled by such a tool, bypassing file
detection for it entirely (reported as STATE_INSTALLED so the banner shows
green rather than nagging)."""
from __future__ import annotations

from Utils.wine_proton.framework_detect import (
    STATE_INSTALLED, STATE_MISSING, detect_frameworks,
)


class _FakeGame:
    def __init__(self, game_root, frameworks, externally_managed_frameworks=None):
        self._game_root = game_root
        self.frameworks = frameworks
        self.externally_managed_frameworks = externally_managed_frameworks or []
        self.root_folder_deploy_enabled = True
        self.mods_dir = ""

    def get_game_path(self):
        return self._game_root

    def get_effective_root_folder_path(self):
        return None


def test_externally_managed_framework_reports_installed_without_a_file(tmp_path):
    game_root = tmp_path / "game"
    game_root.mkdir()  # genuinely empty -- no bin/bink2w64_original.dll
    game = _FakeGame(
        game_root,
        frameworks={"Native Mod Loader": "bin/bink2w64_original.dll"},
        externally_managed_frameworks=["Native Mod Loader"])

    statuses = detect_frameworks(game, filemap_path=None, modlist_path=None)

    assert len(statuses) == 1
    assert statuses[0].label == "Native Mod Loader"
    assert statuses[0].state == STATE_INSTALLED
    assert "externally" in statuses[0].message.lower()


def test_a_different_framework_is_unaffected_by_the_override(tmp_path):
    """Only the labels actually listed get the override -- Script Extender
    still goes through real file detection and correctly reports missing."""
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = _FakeGame(
        game_root,
        frameworks={"Script Extender": "bin/DWrite.dll",
                    "Native Mod Loader": "bin/bink2w64_original.dll"},
        externally_managed_frameworks=["Native Mod Loader"])

    statuses = detect_frameworks(game, filemap_path=None, modlist_path=None)
    by_label = {s.label: s for s in statuses}

    assert by_label["Native Mod Loader"].state == STATE_INSTALLED
    assert by_label["Script Extender"].state == STATE_MISSING


def test_no_override_falls_back_to_real_file_detection(tmp_path):
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = _FakeGame(
        game_root, frameworks={"Native Mod Loader": "bin/bink2w64_original.dll"})

    statuses = detect_frameworks(game, filemap_path=None, modlist_path=None)

    assert statuses[0].state == STATE_MISSING

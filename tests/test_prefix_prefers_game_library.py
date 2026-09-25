"""A game's Proton prefix lives in the SAME Steam library as the game. The finder
used to check the default ~/.local/share/Steam first and only then other
libraries, so a stale prefix left there by an earlier install location shadowed
the real one - "Gate To Sovngarde" launched with the wrong prefix (no vcredist, no
real d3dcompiler_47) and Mosaic kept silently reverting the user's fix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from Utils.wine_proton import steam_finder as sf

APP = "489830"


def _prefix(steam_root: Path) -> Path:
    pfx = steam_root / "steamapps" / "compatdata" / APP / "pfx"
    pfx.mkdir(parents=True)
    return pfx


@pytest.fixture
def libs(tmp_path, monkeypatch):
    default = tmp_path / "default_steam"
    custom = tmp_path / "games"            # a second library, like ~/games/steamapps
    (default / "steamapps").mkdir(parents=True)
    (custom / "steamapps" / "common" / "Skyrim Special Edition").mkdir(parents=True)
    monkeypatch.setattr(sf, "_STEAM_CANDIDATES", [default])
    return default, custom


def test_library_steamapps_for():
    assert sf.library_steamapps_for("/mnt/g/steamapps/common/Game") == Path("/mnt/g/steamapps")
    assert sf.library_steamapps_for(Path("/mnt/g/SteamLibrary/steamapps/common/Game/Sub")) == \
        Path("/mnt/g/SteamLibrary/steamapps")
    assert sf.library_steamapps_for("/games/GOG/Skyrim") is None
    assert sf.library_steamapps_for("/x/common/Game") is None       # 'common' not under steamapps
    assert sf.library_steamapps_for(None) is None and sf.library_steamapps_for("") is None


def test_the_games_own_library_wins_over_a_stale_default_prefix(libs):
    default, custom = libs
    stale, real = _prefix(default), _prefix(custom)
    game = custom / "steamapps" / "common" / "Skyrim Special Edition"
    assert sf.find_prefix(APP, game) == real != stale


def test_without_a_game_path_the_old_order_is_unchanged(libs):
    default, custom = libs
    stale, _real = _prefix(default), _prefix(custom)
    assert sf.find_prefix(APP) == stale


def test_falls_back_when_the_games_library_has_no_prefix_yet(libs):
    default, custom = libs
    only_default = _prefix(default)
    game = custom / "steamapps" / "common" / "Skyrim Special Edition"
    assert sf.find_prefix(APP, game) == only_default


def test_a_game_outside_any_steam_library_uses_the_old_search(libs, tmp_path):
    default, _custom = libs
    stale = _prefix(default)
    assert sf.find_prefix(APP, tmp_path / "GOG" / "Skyrim") == stale


def test_a_symlinked_library_path_still_matches(libs, tmp_path):
    default, custom = libs
    _prefix(default)
    real = _prefix(custom)
    link = tmp_path / "Games"                # like ~/Games -> games/_Games
    link.symlink_to(custom)
    found = sf.find_prefix(APP, link / "steamapps" / "common" / "Skyrim Special Edition")
    assert found is not None and found.resolve() == real.resolve()      # same directory, spelled via the link


def test_no_prefix_anywhere(libs):
    assert sf.find_prefix(APP, libs[1] / "steamapps" / "common" / "Skyrim Special Edition") is None
    assert sf.find_prefix("") is None

"""Fallout 3 is a concrete game on top of BethesdaGame, not the base itself."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Games.Bethesda.bethesda_game import BethesdaGame  # noqa: E402
from Games.Bethesda.fallout_3 import Fallout_3  # noqa: E402
from Games.Bethesda.fallout_3_goty import Fallout3_GOTY  # noqa: E402
from Games.Bethesda.skyrim_se import SkyrimSE  # noqa: E402


def test_hierarchy():
    assert issubclass(Fallout_3, BethesdaGame)
    assert issubclass(Fallout3_GOTY, Fallout_3)
    assert issubclass(SkyrimSE, BethesdaGame) and not issubclass(SkyrimSE, Fallout_3)


def test_base_carries_no_fallout3_identity():
    for attr in ("name", "game_id", "steam_id", "vanilla_plugins", "loot_game_type"):
        assert attr not in vars(BethesdaGame), attr


def test_fallout3_identity():
    g = Fallout_3()
    assert (g.name, g.game_id, g.steam_id) == ("Fallout 3", "Fallout3", "22300")
    assert g.loot_game_type == "Fallout3"

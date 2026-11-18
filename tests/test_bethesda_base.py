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


def test_reshade_hints_match_each_games_engine():
    """The base default (d3d9 / 32-bit) is for the classic DX9 titles only; a 64-bit
    DX11 game must set its own (Enderal SE once inherited the 32-bit default)."""
    from Games.Bethesda.enderal import Enderal
    from Games.Bethesda.enderal_se import EnderalSE
    from Games.Bethesda.fallout_4 import Fallout_4
    from Games.Bethesda.fallout_nv import Fallout_NV
    from Games.Bethesda.oblivion import Oblivion
    from Games.Bethesda.skyrim import Skyrim

    expected = {
        Fallout_3: ("d3d9.dll", 32), Fallout_NV: ("d3d9.dll", 32),
        Oblivion: ("d3d9.dll", 32), Skyrim: ("d3d9.dll", 32), Enderal: ("d3d9.dll", 32),
        SkyrimSE: ("dxgi.dll", 64), EnderalSE: ("dxgi.dll", 64),
        Fallout_4: ("dxgi.dll", 64),
    }
    for cls, hint in expected.items():
        g = cls()
        assert (g.reshade_dll, g.reshade_arch) == hint, cls.__name__

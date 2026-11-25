"""A Wabbajack Fallout 3 list installs into Fallout 3 GOTY too.

Wabbajack has one Fallout3 game, covering both Steam editions (SteamIDs 22300
and 22370 in its GameRegistry); Mosaic has a handler for each. Real case
(2026-11-25): with a Fallout 3 GOTY profile the gallery showed no lists and
an imported list was refused as "for a different game"."""
from __future__ import annotations

from gui_qt.wabbajack.wabbajack_browser_view import filter_entries
from Utils.wabbajack.wabbajack_gallery import GalleryModlist
from Utils.wabbajack.wabbajack_manifest import ModList, mosaic_games_for
from Utils.wabbajack.wabbajack_preflight import check_game


def test_fallout3_covers_both_editions_main_one_first():
    assert mosaic_games_for("Fallout3") == ("Fallout 3", "Fallout 3 GOTY")
    assert mosaic_games_for("SkyrimSpecialEdition") == ("Skyrim Special Edition",)
    assert mosaic_games_for("NotAGame") == ()


def test_a_fallout3_list_passes_the_game_check_on_goty_and_base():
    for active in ("Fallout 3 GOTY", "Fallout 3"):
        (c,) = check_game(ModList(game_type="Fallout3"), active)
        assert c.ok, c
        assert active in c.title


def test_a_fallout3_list_is_still_refused_on_another_game():
    (c,) = check_game(ModList(game_type="Fallout3"), "Fallout New Vegas")
    assert not c.ok
    assert "Fallout 3 or Fallout 3 GOTY" in c.detail


def test_the_gallery_shows_fallout3_lists_on_a_goty_profile():
    lists = [GalleryModlist(title="Rebirth", game="fallout3"),
             GalleryModlist(title="Some NV List", game="falloutnewvegas")]
    assert [e.title for e in filter_entries(lists, game_name="Fallout 3 GOTY")] == ["Rebirth"]
    assert [e.title for e in filter_entries(lists, game_name="Fallout 3")] == ["Rebirth"]
    assert lists[0].mosaic_game == "Fallout 3"          # shown name unchanged

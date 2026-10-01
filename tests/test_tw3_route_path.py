"""Utils.mods.tw3_routing.route_path -- decides whether a staged file
deploys under mods/, dlc/, or the game root.

Real bug, found live: a real mod archive ("Hide Quest in Quest Menu for
Remaster ONLY") ships as `ModHideQuest 5.00 - Je1992/Mods/modHideQuests/
content/...` -- an author-chosen wrapper folder whose own name happens to
start with "mod", wrapping a properly-structured `Mods/modHideQuests/`
TW3 mod underneath it. The original _route_path stopped at the OUTER
wrapper (since it also starts with "mod"), so the mod deployed doubly
nested: mods/ModHideQuest 5.00 - Je1992/Mods/modHideQuests/content/...
instead of mods/modHideQuests/content/.... Not just cosmetic: TW3's own
mod loader only scans one level deep under mods/, so the mod's actual
content was never read by the game at all. Confirmed via Script Merger
failing to find/hash the file at the path it correctly expected
(mods/modHideQuests/content/...), which doesn't exist -- only the
wrongly-doubled path does.

This logic originally lived directly in Games/The Witcher 3/witcher_3.py
(hence the dynamic spec-loading this test used to need -- that folder
name has a space in it, unimportable as a normal module) but was
extracted to Utils.mods.tw3_routing so Utils.mods.tw3_load_index could
reuse it too; witcher_3.py now imports it from there as well.
"""
from __future__ import annotations

from Utils.mods.tw3_routing import route_path as _route_path


# ---- documented baseline behavior (previously untested) -------------------

def test_mod_folder_at_root():
    assert _route_path("modFoo/content/x.xml") == ("mods", "modFoo/content/x.xml")


def test_mod_folder_under_version_wrapper():
    assert _route_path("TrueFires_v1.01/modFoo/content/x.xml") == \
        ("mods", "modFoo/content/x.xml")


def test_mod_folder_under_mods_container():
    assert _route_path("mods/modFoo/content/x.xml") == \
        ("mods", "modFoo/content/x.xml")


def test_mod_folder_under_wrapper_and_mods_container():
    assert _route_path("Full/mods/modFoo/content/x.xml") == \
        ("mods", "modFoo/content/x.xml")


def test_dlc_folder_at_root():
    assert _route_path("dlcFoo/content/x.xml") == ("dlc", "dlcFoo/content/x.xml")


def test_dlc_folder_under_wrapper_and_dlc_container():
    assert _route_path("Full/DLC/dlcFoo/content/x.xml") == \
        ("dlc", "dlcFoo/content/x.xml")


def test_bin_at_root():
    assert _route_path("bin/x64/d3d11.dll") == ("", "bin/x64/d3d11.dll")


def test_bin_under_wrapper():
    assert _route_path("Full/bin/config/r4game/user_config.xml") == \
        ("", "bin/config/r4game/user_config.xml")


# ---- the real bug: a wrapper folder whose name starts with mod/dlc --------

def test_mod_named_wrapper_around_a_real_mods_container_is_not_mistaken_for_it():
    path = ("ModHideQuest 5.00 - Je1992/Mods/modHideQuests/content/scripts/"
            "game/player/playerWitcher.ws")
    assert _route_path(path) == (
        "mods", "modHideQuests/content/scripts/game/player/playerWitcher.ws")


def test_mod_named_wrapper_around_a_real_bin_sibling_is_not_mistaken_for_it():
    path = "ModHideQuest 5.00 - Je1992/bin/config/r4game/user.ini"
    assert _route_path(path) == ("", "bin/config/r4game/user.ini")


def test_dlc_named_wrapper_around_a_real_dlc_container_is_not_mistaken_for_it():
    path = "DLCFooBundle_v2/dlc/dlcFoo/content/x.xml"
    assert _route_path(path) == ("dlc", "dlcFoo/content/x.xml")


def test_genuine_mod_folder_still_wins_when_nothing_disambiguates_it():
    # A real top-level "modFoo" directly containing "content" -- the common
    # case -- must still be accepted immediately, not second-guessed.
    assert _route_path("modFoo/content/scripts/x.ws") == \
        ("mods", "modFoo/content/scripts/x.ws")

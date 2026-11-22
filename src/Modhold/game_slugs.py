"""
game_slugs.py
Map a Mosaic ``game_id`` to the slug Modhold uses for that game.

A game with no entry has no Modhold tab. Only slugs confirmed against the live
API (``GET /api/v1/mods?game=<slug>`` and ``/loadouts?game=<slug>`` answering
200, and the slug present in ``GET /api/v1/games``) belong here; add the rest
as they are checked. Modhold's own list is at https://modholdmods.com/api/v1/games.

Only the *same* game is mapped. A different game that merely shares a name or
engine is left out on purpose, because its mods would not fit: Fallout 4 VR,
Skyrim VR, Subnautica: Below Zero, Kingdom Come: Deliverance (the first one),
Enderal and the Resident Evil games Modhold does not list.
"""

from __future__ import annotations

# game_id -> Modhold slug.
MODHOLD_GAME_SLUGS: dict[str, str] = {
    # Bethesda
    "skyrim_se": "skyrim-se",
    "skyrim": "skyrim",                       # Skyrim Legendary Edition
    "oblivion": "oblivion",
    "oblivion_remastered": "oblivion-remastered",
    "Fallout3": "fallout-3",
    "Fallout3GOTY": "fallout-3",              # same game, same mods
    "Fallout4": "fallout-4",
    "FalloutNV": "fallout-new-vegas",
    "Starfield": "starfield",
    "morrowind": "morrowind",
    "morrowind_openmw": "morrowind",          # OpenMW plays the Morrowind mods
    # Others
    "baldurs_gate_3": "baldurs-gate-3",
    "cyberpunk_2077": "cyberpunk-2077",
    "witcher_3": "witcher-3",
    "dragon_age_origins": "dragon-age-origins",
    "kingdom_come_deliverance_2": "kcd2",
    "Mount___Blade_II__Bannerlord": "bannerlord",
    "red_dead_redemption_2": "red-dead-redemption-2",
    "resident_evil_4": "resident-evil-4",
    "monster_hunter_wilds": "monster-hunter-wilds",
    "marvel_rivals": "marvel-rivals",
    "Stardew_Valley": "stardew-valley",
    "Subnautica": "subnautica",
    "7_Days_to_Die": "7-days-to-die",
    "TCG_Card_Shop_Simulator": "tcg-card-shop",
}


def modhold_slug_for(game_id: str) -> str:
    """Return the Modhold slug for a Mosaic game id, or "" when unmapped."""
    return MODHOLD_GAME_SLUGS.get((game_id or "").strip(), "")

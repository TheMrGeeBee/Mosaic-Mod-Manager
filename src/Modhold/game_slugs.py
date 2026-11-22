"""
game_slugs.py
Map a Mosaic ``game_id`` to the slug Modhold uses for that game.

A game with no entry has no Modhold tab. Only slugs confirmed against the live
API (``GET /api/v1/mods?game=<slug>`` answering 200) belong here; add the rest
as they are checked.
"""

from __future__ import annotations

# game_id -> Modhold slug.
MODHOLD_GAME_SLUGS: dict[str, str] = {
    # Verified against the live API (mods and loadouts both answer).
    "skyrim_se": "skyrim-se",
}


def modhold_slug_for(game_id: str) -> str:
    """Return the Modhold slug for a Mosaic game id, or "" when unmapped."""
    return MODHOLD_GAME_SLUGS.get((game_id or "").strip(), "")

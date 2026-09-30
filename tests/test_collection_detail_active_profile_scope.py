"""Tests for CollectionDetailView._installed_revision — it must only offer
"Update Collection" when the collection is installed in the ACTIVE profile,
not just installed somewhere.

Caught live: a profile ("BG3_DnD_2024_Ruleset") originally created from the
"BG3 Essentials" collection had been customized from 59 to 473 mods since.
find_profile_with_collection_slug() still finds it (collection.json/
profile_settings carry the slug forever, regardless of how much the profile
has diverged) and permanently offered "Update Collection" for that slug --
even while a completely different, unrelated profile ("default") was active
and the user just wanted a plain fresh install there. Clicking Update in
that state doesn't touch the wrong profile (app.py's _run_collection_update
already refuses unless the active profile matches), but it does permanently
block ever getting a plain "Install" for that collection from any profile
other than the one that first ever had it -- the real, reported bug."""
from __future__ import annotations

from types import SimpleNamespace

from Utils.exe_launch import game_helpers
from Utils.profile.profile_state import write_collection_revision
from gui_qt.collections.collection_detail_view import CollectionDetailView


class _FakeGame:
    def __init__(self, name, profile_root):
        self.name = name
        self._profile_root = profile_root
        self._active_profile_dir = None

    def get_profile_root(self):
        return self._profile_root


def _setup(tmp_path, game_name="TestGameForCollectionScope"):
    profile_root = tmp_path
    profiles_dir = profile_root / "profiles"
    old_profile = profiles_dir / "BG3_DnD_2024_Ruleset"
    other_profile = profiles_dir / "default"
    old_profile.mkdir(parents=True)
    other_profile.mkdir(parents=True)

    game_helpers.save_collection_url_to_profile(
        old_profile, "https://www.nexusmods.com/baldursgate3/collections/abc123")
    write_collection_revision(old_profile, 5)

    game = _FakeGame(game_name, profile_root)
    game_helpers._GAMES[game_name] = game
    return game, old_profile, other_profile


def _detail_view(game, slug="abc123"):
    return SimpleNamespace(_collection=SimpleNamespace(slug=slug), _game=game)


def test_offers_update_when_active_profile_is_the_installed_one(tmp_path):
    game, old_profile, _other = _setup(tmp_path)
    try:
        game._active_profile_dir = str(old_profile)
        view = _detail_view(game)

        revision = CollectionDetailView._installed_revision(view)

        assert revision == 5
    finally:
        game_helpers._GAMES.pop(game.name, None)


def test_does_not_offer_update_when_a_different_profile_is_active(tmp_path):
    """The exact bug: an unrelated/diverged profile elsewhere still carries
    the slug, but the user is on a different profile entirely."""
    game, _old_profile, other_profile = _setup(tmp_path)
    try:
        game._active_profile_dir = str(other_profile)
        view = _detail_view(game)

        revision = CollectionDetailView._installed_revision(view)

        assert revision is None
    finally:
        game_helpers._GAMES.pop(game.name, None)


def test_no_active_profile_at_all_returns_none(tmp_path):
    game, _old_profile, _other = _setup(tmp_path)
    try:
        game._active_profile_dir = None
        view = _detail_view(game)

        assert CollectionDetailView._installed_revision(view) is None
    finally:
        game_helpers._GAMES.pop(game.name, None)

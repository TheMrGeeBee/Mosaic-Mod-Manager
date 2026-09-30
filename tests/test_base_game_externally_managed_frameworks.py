"""Test for Games.base_game.BaseGame.externally_managed_frameworks.

Exercised via the property descriptors directly against a lightweight fake
(rather than instantiating the real BaseGame ABC or a full concrete game
subclass) -- this property is a thin wrapper around the same
_load_settings()/_save_settings() pattern auto_deploy already uses, so what
actually needs verifying is its own read/write logic: the settings key name,
list coercion, and that the setter writes back through _save_settings with
every other existing setting preserved."""
from __future__ import annotations

from Games.base_game import BaseGame

_GETTER = BaseGame.externally_managed_frameworks.fget
_SETTER = BaseGame.externally_managed_frameworks.fset


class _FakeGame:
    def __init__(self, initial: dict):
        self._settings = dict(initial)
        self.saved_with: "dict | None" = None

    def _load_settings(self) -> dict:
        return dict(self._settings)

    def _save_settings(self, data: dict) -> None:
        self.saved_with = dict(data)
        self._settings = dict(data)


def test_defaults_to_empty_list_when_unset():
    game = _FakeGame({})
    assert _GETTER(game) == []


def test_reads_back_whatever_was_stored():
    game = _FakeGame({"externally_managed_frameworks": ["Native Mod Loader"]})
    assert _GETTER(game) == ["Native Mod Loader"]


def test_non_list_stored_value_is_ignored_not_raised():
    game = _FakeGame({"externally_managed_frameworks": "not-a-list"})
    assert _GETTER(game) == []


def test_setter_writes_the_key_and_preserves_other_settings():
    game = _FakeGame({"auto_deploy": True})

    _SETTER(game, ["Native Mod Loader"])

    assert game.saved_with == {
        "auto_deploy": True,
        "externally_managed_frameworks": ["Native Mod Loader"],
    }
    assert _GETTER(game) == ["Native Mod Loader"]


def test_setter_accepts_none_as_empty():
    game = _FakeGame({})
    _SETTER(game, None)
    assert _GETTER(game) == []

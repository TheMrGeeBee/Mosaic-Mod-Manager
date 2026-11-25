"""Archive invalidation must never leave a game without its vanilla archives.

Real case (2026-11-25): a fresh Fallout 3 GOTY (never launched, so no
Fallout.ini yet) rendered solid magenta. Mosaic wrote SArchiveList into
FALLOUT.INI and FalloutPrefs.ini starting from an empty list -- the dummy BSA
plus the mods' BSAs only -- and writing the key replaces the game's built-in
list, so Fallout - Textures.bsa, Meshes, Misc ... were never loaded. FALLOUT.INI
also sat next to a Fallout.ini the collection had written: two files Wine
picks between."""
from __future__ import annotations

from pathlib import Path

import pytest

from Games.Bethesda.bethesda_ini import _read_ini_key
from Games.Bethesda.fallout_3_goty import Fallout3_GOTY

VANILLA = ("Fallout - Textures.bsa, Fallout - Meshes.bsa, Fallout - Voices.bsa, "
           "Fallout - Sound.bsa, Fallout - MenuVoices.bsa, Fallout - Misc.bsa")
MOD_BSAS = ["Character Overhaul.bsa", "Unofficial Fallout 3 Patch - Textures.bsa"]


@pytest.fixture
def fo3(tmp_path, monkeypatch):
    game = Fallout3_GOTY()
    root = tmp_path / "Fallout 3 goty"
    (root / "Data").mkdir(parents=True)
    (root / "Fallout_default.ini").write_text(f"[Archive]\nSArchiveList={VANILLA}\n")
    mygames = tmp_path / "My Games" / "Fallout3"
    mygames.mkdir(parents=True)
    game._game_path = root
    monkeypatch.setattr(Fallout3_GOTY, "archive_invalidation", property(lambda s: True))
    monkeypatch.setattr(game, "_mygames_path", lambda: mygames)
    monkeypatch.setattr(game, "_deployed_mod_bsas", lambda: list(MOD_BSAS))
    monkeypatch.setattr(game, "_tracked_mod_bsas", lambda: [])
    monkeypatch.setattr(game, "_save_tracked_mod_bsas", lambda names: None)
    monkeypatch.setattr(game, "_sync_archive_list_custom_ini", lambda *a: None)
    monkeypatch.setattr(game, "_write_dummy_bsa_file", lambda log: None)
    return game, mygames


def _list(path: Path) -> "list[str]":
    return [x.strip() for x in (_read_ini_key(path, "Archive", "SArchiveList") or "").split(",")]


def test_a_fresh_game_keeps_its_vanilla_archives_in_both_inis(fo3):
    game, mygames = fo3
    game.apply_archive_invalidation(lambda m: None)
    for name in ("Fallout.ini", "FalloutPrefs.ini"):
        archives = _list(mygames / name)
        assert archives[0] == "Fallout - Invalidation.bsa", name
        for vanilla in ("Fallout - Textures.bsa", "Fallout - Meshes.bsa", "Fallout - Misc.bsa"):
            assert vanilla in archives, (name, archives)
        assert archives[-2:] == MOD_BSAS, name
    assert sorted(p.name for p in mygames.iterdir()) == ["Fallout.ini", "FalloutPrefs.ini"]


def test_an_existing_ini_of_another_case_is_used_not_duplicated(fo3):
    game, mygames = fo3
    (mygames / "FALLOUT.INI").write_text(f"[Archive]\nSArchiveList={VANILLA}\n")
    game.apply_archive_invalidation(lambda m: None)
    assert sorted(p.name for p in mygames.iterdir()) == ["FALLOUT.INI", "FalloutPrefs.ini"]
    assert "Fallout - Textures.bsa" in _list(mygames / "FALLOUT.INI")


def test_an_ini_with_its_own_list_keeps_it(fo3):
    game, mygames = fo3
    (mygames / "Fallout.ini").write_text(
        "[Archive]\nSArchiveList=Fallout - Textures.bsa, Custom Thing.bsa\n")
    game.apply_archive_invalidation(lambda m: None)
    archives = _list(mygames / "Fallout.ini")
    assert "Custom Thing.bsa" in archives
    assert "Fallout - Meshes.bsa" not in archives      # not re-added over the user's list
    # Prefs starts from the main INI's list, not from nothing
    assert "Custom Thing.bsa" in _list(mygames / "FalloutPrefs.ini")


def test_without_a_default_ini_nothing_is_invented(fo3):
    game, mygames = fo3
    (Path(game._game_path) / "Fallout_default.ini").unlink()
    game.apply_archive_invalidation(lambda m: None)
    assert _list(mygames / "Fallout.ini")[0] == "Fallout - Invalidation.bsa"


def test_every_bethesda_handler_has_its_game_inis_listed():
    """Collection INI tweaks and a Wabbajack list's INIs are looked up by the
    handler's name; a missing name silently skips them (Fallout 3 GOTY did)."""
    import importlib
    import pkgutil

    import Games.Bethesda as pkg
    from Games.Bethesda.bethesda_game import BethesdaGame
    from Utils.collections.collection_ini_tweaks import GAME_INI_TARGETS

    missing = []
    for mod in pkgutil.iter_modules(pkg.__path__):
        m = importlib.import_module(f"Games.Bethesda.{mod.name}")
        for obj in vars(m).values():
            if (isinstance(obj, type) and issubclass(obj, BethesdaGame)
                    and obj is not BethesdaGame and obj.__module__ == m.__name__):
                name = obj().name
                if name not in GAME_INI_TARGETS:
                    missing.append(name)
    assert not missing

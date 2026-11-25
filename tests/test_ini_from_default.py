"""A game's main INI that doesn't exist yet starts from the game's own default.

Real case (2026-11-25): Fallout 3 had never been started, so there was no
Fallout.ini. Mosaic and a collection created one holding only their own keys
(the archive list, a few tweaks) -- no [Fonts], so the main menu was drawn
without any text, and none of the engine's other settings were there."""
from __future__ import annotations

from pathlib import Path

import pytest

from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key, default_ini
from Games.Bethesda.fallout_3_goty import Fallout3_GOTY
from Utils.collections.collection_ini_tweaks import apply_collection_ini_tweaks

DEFAULT = ("[General]\nsLanguage=ENGLISH\n[Fonts]\n"
           "sFontFile_1=Textures\\Fonts\\Glow_Monofonto_Large.fnt\n"
           "[Archive]\nSArchiveList=Fallout - Textures.bsa, Fallout - Meshes.bsa\n")


@pytest.fixture
def fo3(tmp_path, monkeypatch):
    game = Fallout3_GOTY()
    root = tmp_path / "Fallout 3 goty"
    (root / "Data").mkdir(parents=True)
    (root / "Fallout_default.ini").write_text(DEFAULT)
    mygames = tmp_path / "My Games" / "Fallout3"
    mygames.mkdir(parents=True)
    game._game_path = root
    monkeypatch.setattr(Fallout3_GOTY, "archive_invalidation", property(lambda s: True))
    monkeypatch.setattr(game, "_mygames_path", lambda: mygames)
    monkeypatch.setattr(game, "_mygames_paths", lambda: [mygames])
    monkeypatch.setattr(game, "_deployed_mod_bsas", lambda: ["Mod.bsa"])
    monkeypatch.setattr(game, "_tracked_mod_bsas", lambda: [])
    monkeypatch.setattr(game, "_save_tracked_mod_bsas", lambda names: None)
    monkeypatch.setattr(game, "_sync_archive_list_custom_ini", lambda *a: None)
    monkeypatch.setattr(game, "_write_dummy_bsa_file", lambda log: None)
    return game, root, mygames


def _font(path: Path):
    return _read_ini_key(path, "Fonts", "sFontFile_1")


def test_archive_invalidation_creates_the_main_ini_from_the_default(fo3):
    game, _, mygames = fo3
    game.apply_archive_invalidation(lambda m: None)
    main = mygames / "Fallout.ini"
    assert _font(main) == "Textures\\Fonts\\Glow_Monofonto_Large.fnt"
    assert _read_ini_key(main, "General", "sLanguage") == "ENGLISH"
    archives = _read_ini_key(main, "Archive", "SArchiveList")
    assert archives.startswith("Fallout - Invalidation.bsa, Fallout - Textures.bsa")
    assert archives.endswith("Mod.bsa")
    # the Prefs INI has no default; it isn't invented
    assert _font(mygames / "FalloutPrefs.ini") is None


def test_an_existing_main_ini_is_never_replaced(fo3):
    game, _, mygames = fo3
    (mygames / "FALLOUT.INI").write_text("[General]\nsLanguage=GERMAN\n")
    game.apply_archive_invalidation(lambda m: None)
    assert _read_ini_key(mygames / "FALLOUT.INI", "General", "sLanguage") == "GERMAN"
    assert sorted(p.name for p in mygames.iterdir()) == ["FALLOUT.INI", "FalloutPrefs.ini"]


def test_profile_inis_start_from_the_default_when_nothing_is_live(fo3, tmp_path):
    game, _, mygames = fo3
    game._profile_ini_files = True
    ini_dir = tmp_path / "profiles" / "Rebirth" / "ini files"
    game._profile_ini_dir = lambda profile: ini_dir
    game._symlink_profile_ini_files("Rebirth", lambda m: None)
    assert _font(ini_dir / "Fallout.ini") == "Textures\\Fonts\\Glow_Monofonto_Large.fnt"
    assert (mygames / "Fallout.ini").is_symlink()


def test_collection_tweaks_start_from_the_default_not_an_empty_file(fo3, tmp_path):
    _, root, mygames = fo3
    archive = tmp_path / "collection"
    (archive / "INI Tweaks").mkdir(parents=True)
    (archive / "INI Tweaks" / "FOV [Fallout].ini").write_text("[Display]\nfDefaultFOV=80\n")
    apply_collection_ini_tweaks(
        archive_root=archive, profile_dir=mygames, prefix_ini_dir=mygames,
        set_ini_key=_set_ini_key, read_ini_key=_read_ini_key,
        allowed_targets=["Fallout.ini", "FalloutPrefs.ini", "FalloutCustom.ini"],
        game_root=root)
    main = mygames / "Fallout.ini"
    assert _read_ini_key(main, "Display", "fDefaultFOV") == "80"
    assert _font(main) == "Textures\\Fonts\\Glow_Monofonto_Large.fnt"


def test_a_game_without_a_default_ini_gets_none(tmp_path):
    assert default_ini(tmp_path, "Fallout.ini") is None
    assert default_ini(None, "Fallout.ini") is None

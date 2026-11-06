"""Duplicating a profile: everything that defines the setup comes across, the
mods are shared rather than copied, and the source's deploy state does not."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from Utils.exe_launch import game_helpers
from Utils.profile.profile_state import read_profile_settings


def _profiles_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "Profiles" / "AGame"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setattr(game_helpers, "_GAMES", {})
    monkeypatch.setattr(game_helpers, "get_profiles_dir", lambda: tmp_path / "Profiles")
    return root


def _source(root: Path, name: str = "Fashionista", *, shared_pool: Path | None = None):
    d = root / "profiles" / name
    d.mkdir(parents=True)
    (d / "modlist.txt").write_text("+ModA\n-ModB\n")
    (d / "plugins.txt").write_text("*ModA.esp\n")
    (d / "profile_state.json").write_text(
        '{"profile_settings": {"profile_specific_mods": true, "profile_locked": true}}')
    (d / "filemap.txt").write_text("a/b.txt\tModA\n")
    # deploy state and history that must NOT be inherited
    (d / "backups" / "20260101_000000").mkdir(parents=True)
    (d / "overwrite" / "save").mkdir(parents=True)
    (d / "overwrite" / "save" / "autosave.sav").write_text("a savegame")
    (d / "Root_Backup").mkdir()
    (d / "root_folder_deployed.txt").write_text("vampire.exe\n")
    (d / "deploy_stats.txt").write_text("x")
    if shared_pool is not None:
        shared_pool.mkdir(parents=True, exist_ok=True)
        (shared_pool / "ModA").mkdir(exist_ok=True)
        os.symlink(str(shared_pool), d / "mods")
    else:
        (d / "mods" / "ModA").mkdir(parents=True)
        (d / "mods" / "ModA" / "big.bin").write_text("x" * 1000)
    return d


def test_duplicate_carries_the_setup_but_not_the_deploy_state(tmp_path, monkeypatch):
    root = _profiles_root(tmp_path, monkeypatch)
    _source(root)

    new = game_helpers.duplicate_profile("AGame", "Fashionista", "Copy of Fashionista")

    assert new.name == "Copy of Fashionista"
    assert (new / "modlist.txt").read_text() == "+ModA\n-ModB\n"
    assert (new / "plugins.txt").read_text() == "*ModA.esp\n"
    assert (new / "filemap.txt").is_file()
    # the source's own history and deploy state stay behind
    for gone in ("backups", "Root_Backup", "root_folder_deployed.txt",
                 "deploy_stats.txt"):
        assert not (new / gone).exists(), f"{gone} should not be duplicated"
    # ...and the copy starts without the original's savegames
    assert (new / "overwrite").is_dir()
    assert not (new / "overwrite" / "save" / "autosave.sav").exists()


def test_duplicate_joins_the_same_shared_pool_instead_of_copying_it(tmp_path, monkeypatch):
    """A Wabbajack profile's mods are a symlink into one shared pool. Copying
    it would mean another full copy of the modlist (71 GB for Fashionista)."""
    root = _profiles_root(tmp_path, monkeypatch)
    pool = tmp_path / "shared_mods" / "Fashionista_-_Steam"
    _source(root, shared_pool=pool)

    new = game_helpers.duplicate_profile("AGame", "Fashionista", "Copy of Fashionista")

    mods = new / "mods"
    assert mods.is_symlink(), "mods must be shared, not copied"
    assert Path(os.path.realpath(mods)) == pool.resolve()


def test_duplicate_of_a_profile_owning_its_mods_links_to_them(tmp_path, monkeypatch):
    root = _profiles_root(tmp_path, monkeypatch)
    src = _source(root)

    new = game_helpers.duplicate_profile("AGame", "Fashionista", "Copy")

    mods = new / "mods"
    assert mods.is_symlink()
    assert Path(os.path.realpath(mods)) == (src / "mods").resolve()
    assert (mods / "ModA" / "big.bin").read_text() == "x" * 1000
    assert read_profile_settings(new, None).get("mods_shared_from") == "Fashionista"


def test_duplicate_keeps_profile_specific_mods_but_drops_the_lock(tmp_path, monkeypatch):
    root = _profiles_root(tmp_path, monkeypatch)
    _source(root)

    new = game_helpers.duplicate_profile("AGame", "Fashionista", "Copy")

    settings = read_profile_settings(new, None)
    assert settings.get("profile_specific_mods") is True
    assert "profile_locked" not in settings
    assert "original_default" not in settings


def test_duplicate_refuses_an_existing_name_and_a_missing_source(tmp_path, monkeypatch):
    root = _profiles_root(tmp_path, monkeypatch)
    _source(root)
    (root / "profiles" / "Taken").mkdir()

    with pytest.raises(FileExistsError):
        game_helpers.duplicate_profile("AGame", "Fashionista", "Taken")
    with pytest.raises(FileNotFoundError):
        game_helpers.duplicate_profile("AGame", "Nope", "Copy")


# --- the as-installed baseline a Wabbajack install leaves behind -------------

def test_as_installed_backup_is_kept_labelled_and_restorable(tmp_path):
    """A Wabbajack modlist is a setup its author tested. Enabling or removing
    a mod afterwards must be undoable without reinstalling 70 GB."""
    from Utils.profile.profile_backup import (
        create_backup, set_backup_kept, set_backup_label,
        is_backup_user_made, get_backup_label, list_backups, restore_backup)

    prof = tmp_path / "Fashionista"
    prof.mkdir()
    (prof / "modlist.txt").write_text("+Unofficial Patch Plus\n-BAEO\n")
    (prof / "plugins.txt").write_text("")

    b = create_backup(prof, manual=True)
    set_backup_kept(b, True)
    set_backup_label(b, "As installed — Fashionista - Steam")

    # the user then enables a mod the modlist ships disabled
    (prof / "modlist.txt").write_text("+Unofficial Patch Plus\n+BAEO\n")

    [(_dt, found)] = list_backups(prof)
    assert is_backup_user_made(found), "the baseline must never be pruned"
    assert get_backup_label(found) == "As installed — Fashionista - Steam"

    restore_backup(prof, found)
    assert (prof / "modlist.txt").read_text() == "+Unofficial Patch Plus\n-BAEO\n"


def test_removing_the_source_hands_its_mods_to_the_duplicate(tmp_path, monkeypatch):
    """A duplicate of a profile that owns its mods borrows that folder, so
    deleting the source would strand it. The mods move to the copy instead --
    which is what the profile-removal path does before it deletes anything."""
    import shutil

    from Utils.profile.profile_state import (
        hand_over_shared_mods,
        shared_mods_dependents,
    )

    root = _profiles_root(tmp_path, monkeypatch)
    src = _source(root)
    new = game_helpers.duplicate_profile("AGame", "Fashionista", "Copy")
    profiles = root / "profiles"

    assert [d.name for d in shared_mods_dependents(profiles, src)] == ["Copy"]
    assert hand_over_shared_mods(profiles, src) == new
    shutil.rmtree(src)

    mods = new / "mods"
    assert mods.is_dir() and not mods.is_symlink(), "the copy now owns the mods"
    assert (mods / "ModA" / "big.bin").read_text() == "x" * 1000
    assert "mods_shared_from" not in read_profile_settings(new, None)


# --------------------------------------------------------------------------
# a copy of a collection profile is not a second home for that collection
# --------------------------------------------------------------------------
def _collection_source(root: Path, name: str = "Compatibility_25_MAX_Rev13"):
    """A profile installed from a Nexus collection, mid-pause."""
    d = _source(root, name)
    (d / "profile_state.json").write_text(json.dumps({
        "profile_settings": {
            "profile_specific_mods": True,
            "collection_url": "https://www.nexusmods.com/games/cyberpunk2077"
                              "/collections/us59v8/revisions/13",
        },
        "collection_revision_number": 13,
        "collection_install_paused": True,
    }))
    return d


def test_duplicate_does_not_inherit_the_collection_claim(tmp_path, monkeypatch):
    """Two profiles answering to one collection URL made "Install collection"
    a coin toss: the installer continues into the first claimant it finds, so
    a reinstall could land in the copy somebody had been working in."""
    root = _profiles_root(tmp_path, monkeypatch)
    src = _collection_source(root)

    new = game_helpers.duplicate_profile(
        "AGame", src.name, "Modded_Compatibility_25_MAX_Rev13")

    assert "collection_url" not in read_profile_settings(new, None)
    state = json.loads((new / "profile_state.json").read_text())
    assert "collection_revision_number" not in state
    assert "collection_install_paused" not in state, \
        "the source's paused install is not the copy's to resume"
    # the source keeps its own claim
    assert read_profile_settings(src, None).get("collection_url")
    assert json.loads((src / "profile_state.json").read_text())[
        "collection_revision_number"] == 13


def test_a_collection_install_no_longer_finds_the_duplicate(tmp_path, monkeypatch):
    """The end of it: find_profile_with_collection_url is what the installer
    asks, and only the profile the collection was installed into answers."""
    root = _profiles_root(tmp_path, monkeypatch)
    src = _collection_source(root)
    url = read_profile_settings(src, None)["collection_url"]
    game_helpers.duplicate_profile("AGame", src.name, "Copy")

    assert game_helpers.find_profile_with_collection_url("AGame", url) == src.name
    assert game_helpers.find_profile_with_collection_slug("AGame", "us59v8") == src.name

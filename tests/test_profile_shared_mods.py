"""Several Mosaic profiles installed from one Wabbajack modlist share a
single copy of its mods: each profile keeps profile_specific_mods and its own
overwrite/filemap/index, but its ``mods`` entry is a symlink to one folder
outside every profile. These cover that the link behaves -- above all that
removing a profile never takes the shared mods with it."""
from __future__ import annotations

import shutil

from Games.base_game import BaseGame
from Utils.profile.profile_state import (
    link_shared_mods,
    read_shared_mods,
    shared_mods_users,
    write_profile_settings,
)


def _profile(root, name):
    d = root / "profiles" / name
    (d / "mods").mkdir(parents=True)
    (d / "overwrite").mkdir()
    write_profile_settings(d, {"profile_specific_mods": True})
    return d


def test_linked_profiles_share_one_mods_folder(tmp_path):
    shared = tmp_path / "shared_mods" / "fashionista"
    a, b = _profile(tmp_path, "Fashionista"), _profile(tmp_path, "War_Games")
    link_shared_mods(a, shared, "fashionista")
    link_shared_mods(b, shared, "fashionista")

    (shared / "Unofficial Patch Plus").mkdir(parents=True)
    assert (a / "mods" / "Unofficial Patch Plus").is_dir()
    assert (b / "mods" / "Unofficial Patch Plus").is_dir()
    assert read_shared_mods(a) == read_shared_mods(b) == "fashionista"
    assert sorted(p.name for p in shared_mods_users(tmp_path / "profiles", shared)) == [
        "Fashionista", "War_Games"]


def test_the_link_keeps_everything_else_per_profile(tmp_path):
    """The point of linking mods/ rather than redirecting the staging path:
    filemap.txt, modindex.bin and overwrite/ hang off <profile>/mods' parent,
    which is still the profile itself."""
    class _Game(BaseGame):
        @property
        def name(self):
            return "Test"

        @property
        def game_id(self):
            return "test"

        @property
        def exe_name(self):
            return "test.exe"

        def get_mod_staging_path(self):
            return tmp_path / "mods"

        def get_mod_data_path(self):
            return tmp_path / "game" / "Data"

    shared = tmp_path / "shared_mods" / "g"
    a, b = _profile(tmp_path, "A"), _profile(tmp_path, "B")
    link_shared_mods(a, shared, "g")
    link_shared_mods(b, shared, "g")

    game = _Game()
    game.set_active_profile_dir(a)
    assert game.get_effective_mod_staging_path() == a / "mods"
    assert game.get_effective_filemap_path().parent == a
    assert game.get_effective_overwrite_path() == a / "overwrite"
    game.set_active_profile_dir(b)
    assert game.get_effective_filemap_path().parent == b


def test_removing_a_profile_leaves_the_shared_mods_alone(tmp_path):
    shared = tmp_path / "shared_mods" / "g"
    a, b = _profile(tmp_path, "A"), _profile(tmp_path, "B")
    link_shared_mods(a, shared, "g")
    link_shared_mods(b, shared, "g")
    (shared / "ModA").mkdir(parents=True)

    shutil.rmtree(a)
    assert (shared / "ModA").is_dir()
    assert (b / "mods" / "ModA").is_dir()
    assert [p.name for p in shared_mods_users(tmp_path / "profiles", shared)] == ["B"]
    shutil.rmtree(b)
    assert shared_mods_users(tmp_path / "profiles", shared) == []


def test_linking_refuses_a_profile_that_already_holds_mods(tmp_path):
    """Guard against pointing a profile that already installed something at
    the shared pool, which would orphan its mods."""
    import pytest

    a = _profile(tmp_path, "A")
    (a / "mods" / "Existing").mkdir()
    with pytest.raises(OSError):
        link_shared_mods(a, tmp_path / "shared_mods" / "g", "g")
    assert (a / "mods" / "Existing").is_dir() and not (a / "mods").is_symlink()


# -- a duplicate borrowing the source profile's own mods --------------------
#
# duplicate_profile() links the copy at the source's mods folder when the
# source owns it outright (rather than sharing a pool), which makes the source
# a profile other profiles depend on. Removing either end has to leave the
# mods where the survivors can still reach them.

def test_a_duplicate_borrowing_a_profiles_mods_counts_as_a_dependent(tmp_path):
    from Utils.profile.profile_state import shared_mods_dependents

    a, b = _profile(tmp_path, "A"), _profile(tmp_path, "B")
    (a / "mods" / "ModA").mkdir()
    (b / "mods").rmdir()
    (b / "mods").symlink_to((a / "mods").resolve(), target_is_directory=True)

    profiles = tmp_path / "profiles"
    assert [d.name for d in shared_mods_dependents(profiles, a)] == ["B"]
    # The borrower has nothing of its own for anyone to depend on.
    assert shared_mods_dependents(profiles, b) == []


def test_pool_siblings_are_not_dependents(tmp_path):
    """Profiles on a shared Wabbajack pool don't depend on each other: the
    mods live outside both, so removing one is already safe."""
    from Utils.profile.profile_state import shared_mods_dependents

    shared = tmp_path / "shared_mods" / "g"
    a, b = _profile(tmp_path, "A"), _profile(tmp_path, "B")
    link_shared_mods(a, shared, "g")
    link_shared_mods(b, shared, "g")
    assert shared_mods_dependents(tmp_path / "profiles", a) == []


def test_removing_the_borrower_leaves_the_owners_mods_alone(tmp_path):
    """The orphan sweep must see the owning profile as a user. Otherwise
    removing the duplicate would delete the mods out from under the original."""
    from Utils.profile.profile_state import shared_mods_users

    a, b = _profile(tmp_path, "A"), _profile(tmp_path, "B")
    (a / "mods" / "ModA").mkdir()
    (b / "mods").rmdir()
    (b / "mods").symlink_to((a / "mods").resolve(), target_is_directory=True)

    shutil.rmtree(b)
    users = shared_mods_users(tmp_path / "profiles", a / "mods")
    assert [d.name for d in users] == ["A"]


def test_the_sweep_never_deletes_mods_that_live_in_a_profile(tmp_path):
    """Belt and braces on the above, through the sweep itself. The owner being
    counted as a user stops the first case; the second -- a link that pointed
    at a subfolder, so no profile's mods resolves to it -- is caught by the
    sweep's own rule that a folder inside the profiles tree is not a pool."""
    from gui_qt.views.profile_settings_view import ProfileSettingsView

    a = _profile(tmp_path, "A")
    (a / "mods" / "ModA").mkdir()

    class _Stub:
        def __init__(self):
            self.logged = []

        def _log(self, msg):
            self.logged.append(msg)

    profiles = tmp_path / "profiles"
    for target in (a / "mods", a / "mods" / "ModA"):
        ProfileSettingsView._sweep_shared_mods(_Stub(), profiles, target)
        assert target.is_dir(), f"the sweep deleted {target}"


def test_handing_over_moves_the_mods_to_the_first_borrower(tmp_path):
    from Utils.profile.profile_state import (
        hand_over_shared_mods,
        read_profile_settings,
    )

    a, b, c = (_profile(tmp_path, n) for n in ("A", "B", "C"))
    (a / "mods" / "ModA").mkdir()
    for d in (b, c):
        (d / "mods").rmdir()
        (d / "mods").symlink_to((a / "mods").resolve(), target_is_directory=True)
        write_profile_settings(d, {"profile_specific_mods": True,
                                   "mods_shared_from": "A"})

    heir = hand_over_shared_mods(tmp_path / "profiles", a)
    assert heir == b
    shutil.rmtree(a)

    # B owns them now; C follows B.
    assert (b / "mods" / "ModA").is_dir() and not (b / "mods").is_symlink()
    assert "mods_shared_from" not in read_profile_settings(b, None)
    assert (c / "mods").is_symlink()
    assert (c / "mods" / "ModA").is_dir()
    assert read_profile_settings(c, None).get("mods_shared_from") == "B"


def test_handing_over_does_nothing_without_borrowers(tmp_path):
    from Utils.profile.profile_state import hand_over_shared_mods

    a = _profile(tmp_path, "A")
    (a / "mods" / "ModA").mkdir()
    assert hand_over_shared_mods(tmp_path / "profiles", a) is None
    assert (a / "mods" / "ModA").is_dir()


def test_handing_over_does_nothing_for_a_pooled_profile(tmp_path):
    """Removing one profile off a Wabbajack pool must not move the pool."""
    from Utils.profile.profile_state import hand_over_shared_mods

    shared = tmp_path / "shared_mods" / "g"
    a, b = _profile(tmp_path, "A"), _profile(tmp_path, "B")
    link_shared_mods(a, shared, "g")
    link_shared_mods(b, shared, "g")
    (shared / "ModA").mkdir(parents=True)

    assert hand_over_shared_mods(tmp_path / "profiles", a) is None
    assert (shared / "ModA").is_dir() and (b / "mods").is_symlink()

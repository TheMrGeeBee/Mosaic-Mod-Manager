"""A My Games INI that links into another profile is not ours to read or write.

The link lives in the Proton prefix, which outlives the profile it was made
for: it survives deleting that profile, moving the staging root, and even
reinstalling the game, because Steam keeps compatdata. Hit for real on
2026-11-06 — Skyrim SE booted on an INI belonging to a profile from three
months and one staging root earlier, and the deploy's archive-invalidation
step wrote its flag into that other profile's file.
"""
from __future__ import annotations

from pathlib import Path

from Games.Bethesda.fallout_3 import Fallout_3


class _Prefix:
    """Just enough of a game to exercise the sweep: where My Games is, and
    where the active profile keeps its INIs."""

    _drop_foreign_profile_ini_links = Fallout_3._drop_foreign_profile_ini_links

    def __init__(self, root: Path):
        self.mygames = root / "pfx" / "My Games" / "Skyrim Special Edition"
        self.mygames.mkdir(parents=True)
        self.profiles = root / "staging" / "profiles"
        self.logged: list[str] = []

    def _mygames_paths(self):
        return [self.mygames]

    def _profile_ini_dir(self, profile: str) -> Path:
        return self.profiles / profile / "ini files"

    def _ini_in(self, profile: str, name: str, text: str) -> Path:
        d = self._profile_ini_dir(profile)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(text)
        return p

    def sweep(self, profile: str = "Current"):
        self._drop_foreign_profile_ini_links(profile, self.logged.append)


def test_a_link_into_another_profile_becomes_a_real_file(tmp_path):
    pfx = _Prefix(tmp_path)
    old = pfx._ini_in("Gate_To_Sovngarde_Rev116", "Skyrim.ini", "[Display]\niSize W=1920\n")
    link = pfx.mygames / "Skyrim.ini"
    link.symlink_to(old)

    pfx.sweep("SKSE_and_Behaviours_Essentials_Rev68")

    assert not link.is_symlink(), "the other profile's copy is no longer in play"
    assert link.read_text() == "[Display]\niSize W=1920\n", "its settings are kept"
    old.write_text("changed by someone else\n")
    assert link.read_text() != old.read_text(), "and the two are now separate files"
    assert any("another profile" in m for m in pfx.logged), pfx.logged


def test_the_active_profiles_own_link_is_left_alone(tmp_path):
    pfx = _Prefix(tmp_path)
    mine = pfx._ini_in("Current", "Skyrim.ini", "[Archive]\n")
    link = pfx.mygames / "Skyrim.ini"
    link.symlink_to(mine)

    pfx.sweep("Current")

    assert link.is_symlink() and link.resolve() == mine.resolve()
    assert pfx.logged == []


def test_a_dead_link_is_removed_rather_than_left_to_fail(tmp_path):
    """Nothing can be salvaged, but a dangling link is worse than no file:
    the game cannot create its INI through it either."""
    pfx = _Prefix(tmp_path)
    link = pfx.mygames / "SkyrimPrefs.ini"
    link.symlink_to(tmp_path / "staging" / "profiles" / "Gone" / "ini files" / "SkyrimPrefs.ini")

    pfx.sweep()

    assert not link.is_symlink() and not link.exists()
    assert any("dead link" in m for m in pfx.logged), pfx.logged


def test_a_real_ini_is_never_touched(tmp_path):
    pfx = _Prefix(tmp_path)
    real = pfx.mygames / "Skyrim.ini"
    real.write_text("[General]\nsLanguage=ENGLISH\n")

    pfx.sweep()

    assert not real.is_symlink()
    assert real.read_text() == "[General]\nsLanguage=ENGLISH\n"
    assert pfx.logged == []


def test_deploy_sweeps_even_with_the_per_profile_ini_feature_off(tmp_path):
    """The case that actually bit: profile_ini_files was off for Skyrim SE, so
    both the link and the unlink path returned at once and the deploy's Step 5
    printed nothing while a foreign link sat there. The sweep has to run before
    that early return, because Step 7's archive invalidation writes to whatever
    My Games/<game>.ini resolves to moments later."""
    pfx = _Prefix(tmp_path)
    pfx._symlink_profile_ini_files = (
        lambda profile, log: Fallout_3._symlink_profile_ini_files(pfx, profile, log))
    pfx._profile_ini_files = False
    old = pfx._ini_in("Gate_To_Sovngarde_Rev116", "Skyrim.ini", "[Archive]\n")
    link = pfx.mygames / "Skyrim.ini"
    link.symlink_to(old)

    pfx._symlink_profile_ini_files("SKSE_and_Behaviours_Essentials_Rev68", pfx.logged.append)

    assert not link.is_symlink(), "the stale link must not survive a deploy"
    assert link.read_text() == "[Archive]\n"

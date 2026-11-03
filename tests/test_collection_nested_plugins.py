"""A plugin in a mod's subfolder must not be registered as a loadable plugin.

A Bethesda engine only loads plugins sitting directly in Data/. Mods regularly
ship a spare set in a subfolder ("patches for mods you might also have"), which
deploys to Data/<sub>/ and is inert. The collection installer's preliminary
plugins.txt writer walked the whole mod folder, so those became enabled entries:
phantom rows, phantom "missing master" warnings for mods never installed, and
phantom names for LOOT to sort. Enough of them also trip the plugin panel's
mass-miss guard (_PRUNE_MAX = 10), which then refuses to prune ANY stale entry,
so they survive for the life of the profile.

Observed on Starfield Revisited Rev67: 18 such plugins from three mods — 16 in
'Better Ship Part Flips/Optional/', plus 'Simple Hydration Improvements and
Tweaks/Optional/SHI_tweaks.esp' and 'The Final Patch/Optional/Final Patch.esp'.
12 of the 16 named a master the collection never included, and the count stayed
at exactly 18 across reinstalls.
"""
from __future__ import annotations

import inspect

import pytest

from Utils.collections.collection_install import deploys_at_data_root

# Bethesda: the filemap builder strips a leading 'Data' segment, so a mod that
# wraps its files in Data/ still deploys to the root (Starfield/Fallout 3:
# mod_folder_strip_prefixes == {"Data", "oblivion"}).
BETHESDA = {"Data", "oblivion"}


@pytest.mark.parametrize("rel", [
    "BetterShipPartFlips.esm",
    "BSPF-ShipColorize-Patch.esm",          # the pick the collection recorded
    "Data/Item Sorter.esm",                 # Data/-wrapped mod, stripped on deploy
    "data/Item Sorter.esm",                 # strip match is case-insensitive
    "oblivion/Foo.esp",                     # the other declared prefix
])
def test_a_plugin_that_lands_in_the_data_root_is_registered(rel):
    assert deploys_at_data_root(rel, BETHESDA) is True


@pytest.mark.parametrize("rel", [
    "Optional/BSPF-USU-Patch.esm",          # the real Starfield case
    "optional/SHI_tweaks.esp",
    "Optional/Final Patch.esp",
    "Data/Optional/BSPF-USU-Patch.esm",     # one strip still leaves a subfolder
    "Data/textures/stray.esm",
    "deep/deeper/plugin.esp",
])
def test_a_plugin_below_the_data_root_is_not_registered(rel):
    assert deploys_at_data_root(rel, BETHESDA) is False


def test_backslashes_are_treated_as_separators():
    """Archives written on Windows yield backslash members."""
    assert deploys_at_data_root("Optional\\BSPF-USU-Patch.esm", BETHESDA) is False
    assert deploys_at_data_root("Data\\Item Sorter.esm", BETHESDA) is True


def test_only_the_first_segment_is_stripped():
    """Parity with the filemap builder, which strips the leading segment only."""
    assert deploys_at_data_root("Optional/Data/x.esm", BETHESDA) is False


def test_a_game_with_no_strip_prefixes_keeps_only_true_top_level():
    assert deploys_at_data_root("x.esp", set()) is True
    assert deploys_at_data_root("Data/x.esp", set()) is False
    assert deploys_at_data_root("Data/x.esp", None) is False


def test_an_empty_path_is_not_a_plugin():
    assert deploys_at_data_root("", BETHESDA) is False
    assert deploys_at_data_root("/", BETHESDA) is False


def test_a_bare_strip_prefix_segment_is_left_alone():
    """Nothing to strip down to — don't turn 'Data' into an empty path."""
    assert deploys_at_data_root("Data", BETHESDA) is True


def test_the_writer_uses_the_games_own_plugin_extensions():
    """It hardcoded ('.esm', '.esl', '.esp'), which is wrong for BG3."""
    src = inspect.getsource(
        __import__("Utils.collections.collection_install",
                   fromlist=["run_collection_install"]).run_collection_install)
    assert "_write_preliminary_plugins_txt" in src
    assert 'getattr(game, "plugin_extensions"' in src


def test_the_writer_filters_through_the_helper():
    src = inspect.getsource(
        __import__("Utils.collections.collection_install",
                   fromlist=["run_collection_install"]).run_collection_install)
    assert "deploys_at_data_root(" in src, (
        "the preliminary plugins.txt writer must filter nested plugins out")


def test_the_panels_recovery_path_applies_the_same_rule():
    """The two must not drift: plugin_state already refuses nested entries, and
    this test is the reminder that the writer mirrors it."""
    import gui_qt.plugin.plugin_state as ps
    src = inspect.getsource(ps._filemap_deployed_plugins)
    assert 'if "/" in rel_path' in src


# --------------------------------------------------------------------------
# post-install passes must use the staging path captured for THIS install
# --------------------------------------------------------------------------
def test_post_install_passes_never_re_resolve_the_staging_path():
    """``run_collection_install`` restores the active profile before its
    post-install passes run, so any pass that calls
    ``game.get_effective_mod_staging_path()`` again resolves the PREVIOUS
    profile's mods dir instead of where these mods landed.

    This bit twice: the 2026-09-13 verification bug (283 installed mods all
    reported missing), and the Step 5 category/endorsement backfill, which on
    2026-10-28 wrote one collection's metadata into 112 of another profile's
    mods — every folder name the two collections shared — while reconciling
    none of the mods actually installed. Both now reuse the captured
    ``staging_path``; this guards against a third.
    """
    import Utils.collections.collection_install as ci
    src = inspect.getsource(ci.run_collection_install)
    # Everything from the verification block onward runs AFTER the active
    # profile has been restored. Calls before it (e.g. the preliminary
    # plugins.txt writer) run mid-install while the active profile is still the
    # install target, so resolving live is correct there.
    anchor = "_final_staging = staging_path"
    assert anchor in src, "verification block moved; update this guard"
    post_install = "\n".join(
        line for line in src[src.index(anchor):].splitlines()
        if not line.lstrip().startswith("#"))   # comments may name the call
    assert "get_effective_mod_staging_path()" not in post_install, (
        "a post-install pass re-resolves the staging path; reuse the captured "
        "staging_path so it cannot land in the previously-active profile")

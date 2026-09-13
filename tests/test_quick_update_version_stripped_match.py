"""resolve_latest_name_match must still find an update for a mod author who
embeds the version number directly in each file's own Nexus display label
(e.g. "Modname 1.3" -> "Modname 1.4") — an exact-string match can never
succeed across a version bump for these, so Quick Update silently skipped
every one of them ("no name-matched update — use Change Version") even
though a real update existed.

Fixture data below is the REAL current Nexus file listing (mod name, id,
category) for two mods a user specifically flagged as risky to get wrong,
fetched live while designing this fix:
  - Inventory Adjustments Hub (cyberpunk2077 mod 19632) has a "for E3UI"
    variant that must never be picked when the plain variant is installed.
  - Say Something Damn It (cyberpunk2077 mod 22228) has an OPTIONAL "Instant
    Activator" file that must never be picked over the MAIN file.
"""
from __future__ import annotations

from dataclasses import dataclass

from Utils.mods.mod_files_versions import resolve_latest_name_match


@dataclass
class _F:
    file_id: int
    name: str
    category_name: str = "MAIN"
    uploaded_timestamp: int = 0
    file_name: str = ""


# Real data, cyberpunk2077 mod 19632 (trimmed to the mods relevant to the assertions).
_INVENTORY_ADJUSTMENTS_HUB = [
    _F(100875, "Inventory Adjustments Hub", "OLD_VERSION"),
    _F(104650, "Inventory Adjustments Hub 1.0", "OLD_VERSION"),
    _F(105604, "Inventory Adjustments Hub 1.0 for E3UI", "OLD_VERSION"),
    _F(106473, "Inventory Adjustments Hub 1.1", "ARCHIVED"),
    _F(106471, "Inventory Adjustments Hub 1.1 for E3UI", "OLD_VERSION"),
    _F(112580, "Inventory Adjustments Hub 1.2", "OLD_VERSION"),
    _F(112581, "Inventory Adjustments Hub 1.2 for E3UI", "ARCHIVED"),
    _F(117002, "Inventory Adjustments Hub 1.3", "OLD_VERSION"),  # installed
    _F(117003, "Inventory Adjustments Hub 1.3 for E3UI", "OLD_VERSION"),
    _F(142818, "Inventory Adjustments Hub 1.4 - bug found", "OLD_VERSION"),
    _F(142819, "Inventory Adjustments Hub 1.4 for E3UI - bug found", "OLD_VERSION"),
    _F(142831, "Inventory Adjustments Hub 1.4 for E3UI", "MAIN"),
    _F(142832, "Inventory Adjustments Hub 1.4", "MAIN"),
]

# Real data, cyberpunk2077 mod 22228 (trimmed).
_SAY_SOMETHING_DAMN_IT = [
    _F(111963, "Say Something Damn It v1.0.0", "ARCHIVED"),
    _F(111966, "Say Something Damn It Instant Activator v1.0.0", "ARCHIVED"),
    _F(115145, "Say Something Damn It v1.5.0", "ARCHIVED"),
    _F(115146, "Say Something Damn It Instant Activator v1.5.0", "OPTIONAL"),
    _F(131430, "Say Something Damn It v1.14.0", "ARCHIVED"),  # installed
    _F(140675, "Say Something Damn It", "OLD_VERSION"),
    _F(150046, "Say Something Damn It", "OLD_VERSION"),
    _F(150954, "Say Something Damn It", "MAIN"),
]


def test_picks_plain_variant_not_the_e3ui_variant():
    match_id, _ = resolve_latest_name_match(
        _INVENTORY_ADJUSTMENTS_HUB, 117002, "Inventory Adjustments Hub 1.3")
    assert match_id == 142832, "must pick the plain MAIN file, not 142831 (for E3UI)"


def test_picks_main_file_not_the_optional_instant_activator():
    match_id, _ = resolve_latest_name_match(
        _SAY_SOMETHING_DAMN_IT, 131430, "Say Something Damn It v1.14.0")
    assert match_id == 150954, "must pick the MAIN file, not 115146 (OPTIONAL)"


def test_no_main_or_update_candidate_reports_no_match():
    """A mod whose author renamed the file entirely (not just a version bump)
    has nothing in the stripped-name pool at all — must not guess from
    whatever old/archived files happen to share the OLD name."""
    files = [
        _F(1, "Some Tool 1.0", "OLD_VERSION"),
        _F(2, "Some Tool 1.1", "OLD_VERSION"),
        _F(3, "Some Tool Renamed Completely", "MAIN"),
    ]
    match_id, _ = resolve_latest_name_match(files, 2, "Some Tool 1.1")
    # -1 (no match at all) or the installed file's own id (matched itself,
    # nothing newer) both mean "no real update" to the caller
    # (resolve_quick_update_target treats `fid == meta.file_id` as skip too).
    assert match_id in (-1, 2)


def test_exact_match_still_wins_when_available_unchanged_behaviour():
    """A mod whose file label does NOT change on update (constant name each
    release) keeps using plain exact matching — this fix must not change
    that existing, already-working path."""
    files = [
        _F(1, "Some Mod", "OLD_VERSION", uploaded_timestamp=100),
        _F(2, "Some Mod", "MAIN", uploaded_timestamp=200),
    ]
    match_id, old_ids = resolve_latest_name_match(files, 1, "Some Mod")
    assert match_id == 2
    assert old_ids == {1}


def test_already_on_the_newest_version_reports_no_update():
    match_id, _ = resolve_latest_name_match(
        _INVENTORY_ADJUSTMENTS_HUB, 142832, "Inventory Adjustments Hub 1.4")
    assert match_id in (-1, 142832)

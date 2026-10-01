"""Utils.mods.tw3_mods_settings -- writes The Witcher 3's native
mods.settings INI, which the game engine reads to resolve a same-path
conflict between mods (only one mod's copy of a shared file is ever used;
mods.settings' Priority decides which -- LOWEST Priority number wins).

Before this, Mosaic had no TW3 load-order mechanism at all, so a Nexus
Collection's own curator-tested loadOrder was silently ignored -- confirmed
live against a real Collection (witcher3/collections/vpjdhv) where 5 mods
all override game/player/playerWitcher.ws.

The win direction (lowest wins, not highest) was confirmed the hard way:
a first implementation assumed "highest Priority wins" (a natural but wrong
guess, inherited from how most mod managers' own UI position-to-priority
conventions read). It was written and verified byte-correct on disk
(mod0000_MergedFiles -- a Script Merger output meant to supersede all 5
source mods -- at the highest Priority, 41, of 42 entries), yet two
separate live game launches both showed modGearLevelScaling (Priority 11,
the LOWEST of the six-way conflict group: 11/12/18/22/33/41) as the actual
winner instead. "Lowest wins" is the only theory consistent with all six
values, not just a coincidence between two.
"""
from __future__ import annotations

import configparser

from Utils.mods.modlist import ModEntry, write_modlist
from Utils.mods.tw3_mods_settings import write_mods_settings


def _manifest_entry(mod_id: str, prefix: int, file_id: int = 0) -> dict:
    entry = {"id": mod_id, "data": {"prefix": prefix}}
    if file_id:
        entry["fileId"] = file_id
    return entry


def _read(settings_path):
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(settings_path, encoding="utf-8")
    return cp


def test_collection_entries_ranked_by_manifest_prefix_ascending(tmp_path):
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"  # no fallback needed here
    deployed = {
        "modBloodAndSteel": "modBloodAndSteel (Remastered)",
        "modGearLevelScaling": "Gear Level Scaling Remastered",
        "modHideQuests": "Hide Quest in Quest Menu for Remaster ONLY",
    }
    manifest = [
        _manifest_entry("modbloodandsteel", 33),
        _manifest_entry("modgearlevelscaling", 11),
        _manifest_entry("modhidequests", 12),
    ]
    n = write_mods_settings(settings_path, modlist_path, deployed,
                            manifest_load_order=manifest)
    assert n == 3
    cp = _read(settings_path)
    priorities = {s: cp.getint(s, "Priority") for s in cp.sections()}
    # Lower manifest prefix = curator's intended winner = lowest written
    # Priority (confirmed against the real game -- see module docstring).
    assert priorities["modGearLevelScaling"] < priorities["modHideQuests"]
    assert priorities["modHideQuests"] < priorities["modBloodAndSteel"]
    assert min(priorities, key=priorities.get) == "modGearLevelScaling"


def test_manifest_id_match_is_case_insensitive(tmp_path):
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    deployed = {"modSSS_v5": "Tab List More Slots V5"}
    manifest = [_manifest_entry("modSSS_V5", 22)]
    n = write_mods_settings(settings_path, modlist_path, deployed,
                            manifest_load_order=manifest)
    assert n == 1
    cp = _read(settings_path)
    assert cp.has_section("modSSS_v5")


def test_mod_not_in_manifest_falls_back_to_modlist_and_outranks_manifest(tmp_path):
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, [
        ModEntry(name="Extra Mod", enabled=True, locked=False),
    ])
    deployed = {
        "modBloodAndSteel": "modBloodAndSteel (Remastered)",
        "modExtra": "Extra Mod",
    }
    manifest = [_manifest_entry("modbloodandsteel", 33)]
    write_mods_settings(settings_path, modlist_path, deployed,
                        manifest_load_order=manifest)
    cp = _read(settings_path)
    # The manually-added mod (not in the Collection) must win over every
    # Collection-sourced mod by default -- lower Priority = wins.
    assert cp.getint("modExtra", "Priority") < cp.getint("modBloodAndSteel", "Priority")


def test_modlist_top_entry_gets_lowest_fallback_priority(tmp_path):
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, [
        ModEntry(name="Top Mod", enabled=True, locked=False),
        ModEntry(name="Middle Mod", enabled=True, locked=False),
        ModEntry(name="Bottom Mod", enabled=True, locked=False),
    ])
    deployed = {
        "modTop": "Top Mod",
        "modMiddle": "Middle Mod",
        "modBottom": "Bottom Mod",
    }
    write_mods_settings(settings_path, modlist_path, deployed)
    cp = _read(settings_path)
    # Top of modlist.txt = highest Mosaic priority = wins = lowest written
    # Priority integer.
    assert cp.getint("modTop", "Priority") < cp.getint("modMiddle", "Priority")
    assert cp.getint("modMiddle", "Priority") < cp.getint("modBottom", "Priority")


def test_disabled_modlist_entries_are_ignored_for_ranking(tmp_path):
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, [
        ModEntry(name="Enabled Top", enabled=True, locked=False),
        ModEntry(name="Disabled Mid", enabled=False, locked=False),
        ModEntry(name="Enabled Bottom", enabled=True, locked=False),
    ])
    # Only the two enabled mods were actually deployed.
    deployed = {"modA": "Enabled Top", "modB": "Enabled Bottom"}
    write_mods_settings(settings_path, modlist_path, deployed)
    cp = _read(settings_path)
    assert cp.sections() == ["modA", "modB"]
    assert cp.getint("modA", "Priority") < cp.getint("modB", "Priority")


def test_no_manifest_and_no_modlist_file_still_ranks_everything(tmp_path):
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "does_not_exist.txt"
    deployed = {"modA": "Mod A", "modB": "Mod B"}
    n = write_mods_settings(settings_path, modlist_path, deployed)
    assert n == 2
    cp = _read(settings_path)
    assert set(cp.sections()) == {"modA", "modB"}


def test_empty_deployed_mods_writes_nothing(tmp_path):
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    n = write_mods_settings(settings_path, modlist_path, {})
    assert n == 0
    assert not settings_path.exists()


def test_every_written_entry_is_enabled_with_matching_vk(tmp_path):
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    deployed = {"modFoo": "Foo Mod"}
    write_mods_settings(settings_path, modlist_path, deployed)
    cp = _read(settings_path)
    assert cp.get("modFoo", "Enabled") == "1"
    assert cp.get("modFoo", "VK") == "modFoo"


def test_mismatched_folder_name_still_matches_correctly_via_file_id(tmp_path):
    """Real data: the collection's manifest lists id "modHideQuests" with
    fileId 74242, but the actual archive's top-level folder is "ModHideQuest
    5.00 - Je1992" -- not even a prefix match (singular vs plural). File-id
    matching (the mod's own tracked Nexus file_id, independent of archive
    folder naming) must still get this right rather than falling back and
    letting it win priority the curator never intended."""
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    deployed = {
        "modBloodAndSteel": "modBloodAndSteel (Remastered)",
        "ModHideQuest 5.00 - Je1992": "Hide Quest in Quest Menu for Remaster ONLY",
    }
    file_ids = {
        "modBloodAndSteel": 74531,
        "ModHideQuest 5.00 - Je1992": 74242,
    }
    manifest = [
        _manifest_entry("modbloodandsteel", 33, file_id=74531),
        _manifest_entry("modhidequests", 12, file_id=74242),
    ]
    write_mods_settings(settings_path, modlist_path, deployed,
                        manifest_load_order=manifest, file_ids=file_ids)
    cp = _read(settings_path)
    # Lower manifest prefix (modHideQuests, 12) = wins = lower Priority.
    assert (cp.getint("ModHideQuest 5.00 - Je1992", "Priority")
            < cp.getint("modBloodAndSteel", "Priority"))


def test_mismatched_folder_name_without_a_file_id_falls_back_and_warns(tmp_path):
    """Same folder-naming mismatch as above, but this time the mod has no
    tracked Nexus file_id at all (e.g. a non-Nexus/manually-installed
    source) -- there's genuinely no safe way to identify it, so it must
    fall back to modlist.txt ranking rather than silently guess, and say
    so in the log."""
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, [
        ModEntry(name="Hide Quest in Quest Menu for Remaster ONLY",
                 enabled=True, locked=False),
    ])
    deployed = {
        "modBloodAndSteel": "modBloodAndSteel (Remastered)",
        "ModHideQuest 5.00 - Je1992": "Hide Quest in Quest Menu for Remaster ONLY",
    }
    manifest = [
        _manifest_entry("modbloodandsteel", 33, file_id=74531),
        _manifest_entry("modhidequests", 12, file_id=74242),
    ]
    logged = []
    write_mods_settings(settings_path, modlist_path, deployed,
                        log_fn=logged.append,
                        manifest_load_order=manifest)  # no file_ids given
    cp = _read(settings_path)
    # Unmatched falls back to modlist.txt and wins over every Collection
    # entry -- "wins" now means the lowest written Priority.
    assert (cp.getint("ModHideQuest 5.00 - Je1992", "Priority")
            < cp.getint("modBloodAndSteel", "Priority"))
    assert any("ModHideQuest 5.00 - Je1992" in m for m in logged)


def test_written_format_has_no_spaces_around_equals_and_uses_crlf(tmp_path):
    """Real bug, found by live-testing against the actual Script Merger tool:
    it rejected a `Key = Value`-with-spaces file as invalid ("Unrecognized
    setting ... Enabled = 1") -- its parser splits on the first "=" without
    trimming, so a space before "=" makes the key literally "Enabled "
    (trailing space), which never matches what it's looking for. Must write
    strict "Key=Value", no spaces, real Windows CRLF line endings."""
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    deployed = {"modFoo": "Foo Mod"}
    write_mods_settings(settings_path, modlist_path, deployed)
    raw = settings_path.read_bytes()
    assert b"Enabled=1" in raw
    assert b"Enabled = 1" not in raw
    assert b"\r\n" in raw
    # No bare \n without a preceding \r anywhere in the file.
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_one_mod_producing_two_folders_never_gets_duplicate_priority(tmp_path):
    """Real bug, found live: after the deploy-routing fix for a mod whose
    own wrapper folder name starts with "mod", "Hide Quest in Quest Menu"
    legitimately produced TWO top-level TW3 folders -- its real
    "modHideQuests" content folder, and a leftover wrapper
    "ModHideQuest 5.00 - Je1992" holding one ambiguous loose file. Both
    trace to the same Nexus file_id, so both matched the same manifest
    entry -- Script Merger refused to proceed: "Duplicate mod priorities
    detected". Only ONE folder_id may ever claim a given manifest entry;
    the other must fall back. Final Priority values are always a fresh
    unique 0..N-1 sequence now, so duplicates are structurally impossible
    regardless -- this test just confirms the RIGHT folder keeps the claim."""
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, [
        ModEntry(name="Hide Quest in Quest Menu for Remaster ONLY",
                 enabled=True, locked=False),
    ])
    deployed = {
        "modHideQuests": "Hide Quest in Quest Menu for Remaster ONLY",
        "ModHideQuest 5.00 - Je1992": "Hide Quest in Quest Menu for Remaster ONLY",
    }
    file_ids = {
        "modHideQuests": 74242,
        "ModHideQuest 5.00 - Je1992": 74242,
    }
    manifest = [_manifest_entry("modhidequests", 12, file_id=74242)]
    write_mods_settings(settings_path, modlist_path, deployed,
                        manifest_load_order=manifest, file_ids=file_ids)
    cp = _read(settings_path)
    priorities = [cp.getint(s, "Priority") for s in cp.sections()]
    assert len(priorities) == len(set(priorities)), \
        f"duplicate Priority values written: {priorities}"
    # The real "modHideQuests" folder keeps the manifest's actual claim;
    # the leftover wrapper falls back to modlist.txt instead of sharing it.
    assert (cp.getint("modHideQuests", "Priority")
            != cp.getint("ModHideQuest 5.00 - Je1992", "Priority"))


def test_real_folder_wins_the_manifest_match_regardless_of_dict_order(tmp_path):
    """Real bug found live, second round: the first fix for the duplicate-
    priority case still let the WRONG folder win when the incidental
    wrapper folder happened to be processed before the real content
    folder -- the wrapper matched by file_id before the real folder's
    (more precise) id-string match was even tried. The real folder
    ("modHideQuests", matches the manifest's id string exactly) must
    always win over the wrapper ("ModHideQuest 5.00 - Je1992", only
    matches by the shared file_id), no matter which one Python's dict
    happens to iterate first."""
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, [
        ModEntry(name="Hide Quest in Quest Menu for Remaster ONLY",
                 enabled=True, locked=False),
    ])
    # Wrapper folder inserted FIRST -- this ordering is what exposed the bug.
    deployed = {
        "ModHideQuest 5.00 - Je1992": "Hide Quest in Quest Menu for Remaster ONLY",
        "modHideQuests": "Hide Quest in Quest Menu for Remaster ONLY",
    }
    file_ids = {
        "ModHideQuest 5.00 - Je1992": 74242,
        "modHideQuests": 74242,
    }
    manifest = [_manifest_entry("modhidequests", 12, file_id=74242)]
    write_mods_settings(settings_path, modlist_path, deployed,
                        manifest_load_order=manifest, file_ids=file_ids)
    cp = _read(settings_path)
    # The real content folder keeps the manifest's actual claim...
    # ...not the incidental wrapper, which must fall back instead.
    assert (cp.getint("modHideQuests", "Priority")
            != cp.getint("ModHideQuest 5.00 - Je1992", "Priority"))


def test_never_raises_on_bad_manifest_shape(tmp_path):
    settings_path = tmp_path / "mods.settings"
    modlist_path = tmp_path / "modlist.txt"
    deployed = {"modFoo": "Foo Mod"}
    bad_manifest = [{"id": None, "data": "not-a-dict"}, "garbage", 42]
    n = write_mods_settings(settings_path, modlist_path, deployed,
                            manifest_load_order=bad_manifest)
    assert n == 1  # fell through to the modlist/default-rank fallback cleanly

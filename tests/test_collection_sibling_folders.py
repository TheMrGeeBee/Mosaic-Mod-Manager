"""Several files of one Nexus mod page that a collection lists under the same name
(main + HD textures, variants, patches) must each get their own folder.

They used to share one, so each install replaced the one before it: in the
Morrowind Remastered collection 41 mod names ended up with only their last file
(OAAB_Data's HD textures replaced its .esm, which then showed as a missing master).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Utils.collections.collection_install import (  # noqa: E402
    _build_collision_suffix_map,
    _name_fallback_folder,
)


def _entry(name, mod_id, file_id):
    return {"name": name, "source": {"modId": mod_id, "fileId": file_id}}


def _suffixes(*entries):
    mods = list(entries)
    pos = {e["source"]["fileId"]: i for i, e in enumerate(mods)}
    names = {i: e["name"] for i, e in enumerate(mods)}
    return _build_collision_suffix_map(mods, {}, names, pos)


def test_files_of_one_page_with_the_same_name_are_numbered_in_manifest_order():
    got = _suffixes(_entry("OAAB_Data", 49042, 1), _entry("Other", 7, 5), _entry("OAAB_Data", 49042, 2),
                    _entry("OAAB_Data", 49042, 3))
    assert got == {1: "", 5: "", 2: " (2)", 3: " (3)"}


def test_the_same_file_listed_twice_is_one_install_not_a_collision():
    """A FOMOD listed twice with different choice sets (the Cyberpunk case)."""
    got = _suffixes(_entry("Mazda parts", 25274, 170488), _entry("Mazda parts", 25274, 170488))
    assert got == {170488: ""}


def test_the_same_name_on_different_pages_still_gets_the_mod_id():
    got = _suffixes(_entry("Textures", 100, 1), _entry("Textures", 200, 2))
    assert got == {1: " (100)", 2: " (200)"}


def test_unique_names_get_no_suffix():
    assert _suffixes(_entry("A", 1, 10), _entry("B", 2, 20)) == {10: "", 20: ""}


# -- the "already installed, matched by name" fallback --------------------------------------------------
def _idx(*fids):
    return SimpleNamespace(file_id_to_logical={f: "OAAB_Data" for f in fids},
                           pos_to_name={}, file_id_to_pos={f: i for i, f in enumerate(fids)})


def _mod(fid, name="OAAB_Data"):
    return SimpleNamespace(file_id=fid, mod_name=name)


def test_the_plain_name_is_not_claimed_for_a_different_file_of_the_collection():
    idx = _idx(1, 2)
    folders = {"oaab_data": "OAAB_Data"}
    owner = {"oaab_data": 2}                  # that folder holds file 2 (the HD textures)
    assert _name_fallback_folder(idx, _mod(1), folders, owner) == ""     # file 1 is NOT installed
    assert _name_fallback_folder(idx, _mod(2), folders, owner) == "OAAB_Data"


def test_a_folder_with_no_recorded_file_id_still_matches_by_name():
    assert _name_fallback_folder(_idx(1), _mod(1), {"oaab_data": "OAAB_Data"}, {}) == "OAAB_Data"


def test_a_folder_from_an_older_revision_still_matches_by_name():
    """Its file id is not in this manifest (the mod was updated), so it is the same mod."""
    idx = _idx(1)
    assert _name_fallback_folder(idx, _mod(1), {"oaab_data": "OAAB_Data"}, {"oaab_data": 999}) == "OAAB_Data"


def test_no_folder_means_no_match():
    assert _name_fallback_folder(_idx(1), _mod(1), {}, {}) == ""


# -- resuming a collection installed under the old shared-folder scheme ---------------------------------
def _idx2():
    """Main (file 1) and HD (file 2) of one page, both named OAAB_Data in the manifest."""
    return SimpleNamespace(
        file_id_to_logical={1: "OAAB_Data", 2: "OAAB_Data"}, pos_to_name={},
        file_id_to_pos={1: 0, 2: 1}, file_id_to_suffix={1: "", 2: " (2)"},
        file_id_to_mod_id={1: 49042, 2: 49042})


def _resolve(mod, by_ids, folders, owners):
    from Utils.collections.collection_install import _resolve_existing_folder
    return _resolve_existing_folder(_idx2(), mod, by_ids, {}, folders, owners)


def test_resume_repairs_a_page_whose_files_shared_one_folder():
    """Old install: the HD file (2) was installed last into 'OAAB_Data', wiping the main
    file (1). Both must be installed again: 1 into 'OAAB_Data', 2 into 'OAAB_Data (2)'."""
    by_ids = {(49042, 2): "OAAB_Data"}                     # the folder's meta.ini says file 2
    folders, owners = {"oaab_data": "OAAB_Data"}, {"oaab_data": 2}
    assert _resolve(_mod(1), by_ids, folders, owners) == ""     # main: its content is gone
    assert _resolve(_mod(2), by_ids, folders, owners) == ""     # HD: belongs in 'OAAB_Data (2)'


def test_resume_keeps_a_page_whose_first_file_won_and_installs_the_rest():
    by_ids = {(49042, 1): "OAAB_Data"}
    folders, owners = {"oaab_data": "OAAB_Data"}, {"oaab_data": 1}
    assert _resolve(_mod(1), by_ids, folders, owners) == "OAAB_Data"
    assert _resolve(_mod(2), by_ids, folders, owners) == ""


def test_resume_after_the_fix_recognises_numbered_folders():
    by_ids = {(49042, 1): "OAAB_Data", (49042, 2): "OAAB_Data (2)"}
    folders = {"oaab_data": "OAAB_Data", "oaab_data (2)": "OAAB_Data (2)"}
    owners = {"oaab_data": 1, "oaab_data (2)": 2}
    assert _resolve(_mod(1), by_ids, folders, owners) == "OAAB_Data"
    assert _resolve(_mod(2), by_ids, folders, owners) == "OAAB_Data (2)"

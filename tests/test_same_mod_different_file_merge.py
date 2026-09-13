"""A MAIN/UPDATE file for a Nexus mod that's already installed (under a
different folder name, since each file has its own per-file Nexus label)
must be recognized as updating the EXISTING install, not treated as a
brand-new mod with its own freshly-derived folder name.

Real-world trigger: an already-installed "Cyber Engine Tweaks" (mod 107),
then downloading its "1.37.1 - Scripting fixes" hotfix file via "Mod Manager
Download" on the Nexus website. Without this fix, the hotfix's own per-file
label ("CET 1.37.1 - Scripting fixes") named a SECOND, separate mod folder
for the same Nexus mod — finish_install's collision check only ever sees the
new name, so it never prompts Replace and the original entry's modlist
position is silently left untouched while a duplicate appears elsewhere in
the list (reads to the user as "the mod jumped").

OPTIONAL files (deliberately meant to coexist as separate installs, e.g. an
"HD Textures" file alongside the "Main File") must NOT be merged this way.
"""
from __future__ import annotations

from Nexus.nexus_meta import NexusModMeta, write_meta
from Utils.mods.mod_install import _existing_install_for_same_mod


def _install(staging_root, folder_name: str, mod_id: int) -> None:
    mod_dir = staging_root / folder_name
    mod_dir.mkdir(parents=True)
    write_meta(mod_dir / "meta.ini", NexusModMeta(
        mod_name=folder_name, game_domain="cyberpunk2077", mod_id=mod_id,
        nexus_name="Cyber Engine Tweaks"))


def test_main_file_for_already_installed_mod_merges_into_existing_folder(tmp_path):
    _install(tmp_path, "Cyber Engine Tweaks", mod_id=107)
    new_file_meta = NexusModMeta(mod_id=107, file_category="UPDATE",
                                 nexus_file_name="CET 1.37.1 - Scripting fixes")
    assert _existing_install_for_same_mod(tmp_path, new_file_meta) == "Cyber Engine Tweaks"


def test_main_category_also_merges(tmp_path):
    _install(tmp_path, "Cyber Engine Tweaks", mod_id=107)
    new_file_meta = NexusModMeta(mod_id=107, file_category="MAIN")
    assert _existing_install_for_same_mod(tmp_path, new_file_meta) == "Cyber Engine Tweaks"


def test_optional_file_does_not_merge(tmp_path):
    """An OPTIONAL file is meant to coexist as its own separate mod entry."""
    _install(tmp_path, "Cyber Engine Tweaks", mod_id=107)
    optional_meta = NexusModMeta(mod_id=107, file_category="OPTIONAL",
                                 nexus_file_name="HD Textures")
    assert _existing_install_for_same_mod(tmp_path, optional_meta) is None


def test_old_version_file_does_not_merge(tmp_path):
    _install(tmp_path, "Cyber Engine Tweaks", mod_id=107)
    old_meta = NexusModMeta(mod_id=107, file_category="OLD_VERSION")
    assert _existing_install_for_same_mod(tmp_path, old_meta) is None


def test_no_existing_install_returns_none(tmp_path):
    new_file_meta = NexusModMeta(mod_id=107, file_category="MAIN")
    assert _existing_install_for_same_mod(tmp_path, new_file_meta) is None


def test_different_mod_id_does_not_merge(tmp_path):
    _install(tmp_path, "Cyber Engine Tweaks", mod_id=107)
    other_mod_meta = NexusModMeta(mod_id=999, file_category="MAIN")
    assert _existing_install_for_same_mod(tmp_path, other_mod_meta) is None


def test_none_meta_returns_none(tmp_path):
    assert _existing_install_for_same_mod(tmp_path, None) is None


def test_zero_mod_id_returns_none(tmp_path):
    _install(tmp_path, "Cyber Engine Tweaks", mod_id=107)
    unresolved_meta = NexusModMeta(mod_id=0, file_category="MAIN")
    assert _existing_install_for_same_mod(tmp_path, unresolved_meta) is None

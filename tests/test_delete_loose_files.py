"""bsa_pack_ops.delete_loose_files: after packing, the loose originals are
removed. The keys are lowercase forward-slash paths, but on a case-sensitive
filesystem the files on disk can have any casing."""
from __future__ import annotations

from pathlib import Path

from Utils.archives.bsa_pack_ops import delete_loose_files


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_deletes_files_whatever_their_case_on_disk(tmp_path):
    _touch(tmp_path / "Textures" / "Armor" / "Iron.DDS")
    _touch(tmp_path / "meshes" / "Sword.NIF")

    deleted = delete_loose_files(tmp_path, ["textures/armor/iron.dds", "meshes/sword.nif"])

    assert deleted == 2
    assert not any(tmp_path.rglob("*.*"))


def test_several_files_in_one_directory_are_all_deleted(tmp_path):
    """Directory listings are cached and shared across lookups; resolving and
    deleting several files from one folder must still find and delete each."""
    for name in ("A.dds", "B.dds", "C.dds"):
        _touch(tmp_path / "Textures" / name)

    deleted = delete_loose_files(
        tmp_path, ["textures/a.dds", "textures/b.dds", "textures/c.dds"])

    assert deleted == 3
    assert not (tmp_path / "Textures").exists()          # emptied folder removed


def test_exact_case_keys_are_deleted_directly(tmp_path):
    _touch(tmp_path / "textures" / "iron.dds")

    assert delete_loose_files(tmp_path, ["textures/iron.dds"]) == 1


def test_missing_and_unpacked_files_are_ignored(tmp_path):
    keep = _touch(tmp_path / "Textures" / "Keep.dds")

    deleted = delete_loose_files(tmp_path, ["textures/gone.dds", "nowhere/at/all.dds"])

    assert deleted == 0
    assert keep.exists()


def test_folders_that_still_hold_files_are_kept_and_root_survives(tmp_path):
    _touch(tmp_path / "Textures" / "Packed.dds")
    other = _touch(tmp_path / "Textures" / "Other.dds")
    _touch(tmp_path / "Only" / "Packed.dds")

    deleted = delete_loose_files(tmp_path, ["textures/packed.dds", "only/packed.dds"])

    assert deleted == 2
    assert other.exists()
    assert not (tmp_path / "Only").exists()
    assert tmp_path.is_dir()


def test_directory_in_the_way_is_not_treated_as_a_file(tmp_path):
    (tmp_path / "Textures" / "Armor.dds").mkdir(parents=True)

    assert delete_loose_files(tmp_path, ["textures/armor.dds"]) == 0
    assert (tmp_path / "Textures" / "Armor.dds").is_dir()


def test_empty_key_list_is_a_no_op(tmp_path):
    assert delete_loose_files(tmp_path, []) == 0

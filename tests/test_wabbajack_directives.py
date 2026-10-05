"""Tests for Utils.wabbajack.wabbajack_directives.apply_directive -- each
directive handler against tiny synthetic fixtures, including a
PatchedFromArchive round-trip through bsdiff4 and a .wabbajack container."""
from __future__ import annotations

import json
import zipfile

import bsdiff4

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.wabbajack_directives import apply_directive, path_substitutions
from Utils.wabbajack.wabbajack_hash import hash_bytes
from Utils.wabbajack.wabbajack_vfs import ArchiveIndex


def _make_wabbajack_file(path, inline_files=None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("modlist", json.dumps({"Name": "Test"}))
        for data_id, payload in (inline_files or {}).items():
            zf.writestr(data_id, payload)


def _index_with_zip_archive(tmp_path, members: dict, archive_hash="arc-hash"):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    idx = ArchiveIndex(tmp_path / "scratch")
    idx.add_archive(archive_hash, archive)
    return idx


# ---------------------------------------------------------------------------
# FromArchive
# ---------------------------------------------------------------------------

def test_from_archive_copies_and_verifies_hash(tmp_path):
    payload = b"plugin file bytes"
    idx = _index_with_zip_archive(tmp_path, {"mod/foo.esp": payload})
    directive = wm.FromArchiveDirective(
        to="mods/Foo/foo.esp", hash=hash_bytes(payload), size=len(payload),
        archive_hash_path=["arc-hash", "mod/foo.esp"])

    dest_root = tmp_path / "dest"
    result = apply_directive(
        directive, dest_root=dest_root, wabbajack_path=tmp_path / "unused.wabbajack",
        archive_index=idx)

    assert result.success
    assert (dest_root / "mods/Foo/foo.esp").read_bytes() == payload


def test_from_archive_hash_mismatch_fails(tmp_path):
    idx = _index_with_zip_archive(tmp_path, {"mod/foo.esp": b"actual bytes"})
    directive = wm.FromArchiveDirective(
        to="mods/Foo/foo.esp", hash="wrong-hash==", size=1,
        archive_hash_path=["arc-hash", "mod/foo.esp"])
    result = apply_directive(
        directive, dest_root=tmp_path / "dest", wabbajack_path=tmp_path / "unused.wabbajack",
        archive_index=idx)
    assert not result.success
    assert "hash mismatch" in result.error


def test_from_archive_unresolvable_path_reports_error(tmp_path):
    idx = ArchiveIndex(tmp_path / "scratch")
    directive = wm.FromArchiveDirective(
        to="mods/Foo/foo.esp", hash="x==", size=1, archive_hash_path=["missing-hash", "foo.esp"])
    result = apply_directive(
        directive, dest_root=tmp_path / "dest", wabbajack_path=tmp_path / "unused.wabbajack",
        archive_index=idx)
    assert not result.success
    assert not result.unsupported


# ---------------------------------------------------------------------------
# PatchedFromArchive
# ---------------------------------------------------------------------------

def test_patched_from_archive_round_trips_through_bsdiff(tmp_path):
    source_bytes = b"original mod file content, version 1" * 10
    target_bytes = b"patched mod file content, version 2!!" * 10
    patch_bytes = bsdiff4.diff(source_bytes, target_bytes)

    idx = _index_with_zip_archive(tmp_path, {"mod/foo.esp": source_bytes})
    wj_path = tmp_path / "list.wabbajack"
    _make_wabbajack_file(wj_path, inline_files={"patch-1": patch_bytes})

    directive = wm.PatchedFromArchiveDirective(
        to="mods/Foo/foo.esp", hash=hash_bytes(target_bytes), size=len(target_bytes),
        archive_hash_path=["arc-hash", "mod/foo.esp"], patch_id="patch-1")

    dest_root = tmp_path / "dest"
    result = apply_directive(
        directive, dest_root=dest_root, wabbajack_path=wj_path, archive_index=idx)

    assert result.success
    assert (dest_root / "mods/Foo/foo.esp").read_bytes() == target_bytes


def test_patched_from_archive_missing_patch_id_reports_error(tmp_path):
    idx = _index_with_zip_archive(tmp_path, {"mod/foo.esp": b"source"})
    wj_path = tmp_path / "list.wabbajack"
    _make_wabbajack_file(wj_path)
    directive = wm.PatchedFromArchiveDirective(
        to="mods/Foo/foo.esp", hash="x==", size=1,
        archive_hash_path=["arc-hash", "mod/foo.esp"], patch_id="no-such-patch")
    result = apply_directive(
        directive, dest_root=tmp_path / "dest", wabbajack_path=wj_path, archive_index=idx)
    assert not result.success
    assert "patch data" in result.error


# ---------------------------------------------------------------------------
# InlineFile / RemappedInlineFile
# ---------------------------------------------------------------------------

def test_inline_file_extracts_and_verifies_hash(tmp_path):
    payload = b"inline config contents"
    wj_path = tmp_path / "list.wabbajack"
    _make_wabbajack_file(wj_path, inline_files={"data-1": payload})
    directive = wm.InlineFileDirective(
        to="config.ini", hash=hash_bytes(payload), size=len(payload), source_data_id="data-1")

    dest_root = tmp_path / "dest"
    result = apply_directive(
        directive, dest_root=dest_root, wabbajack_path=wj_path,
        archive_index=ArchiveIndex(tmp_path / "scratch"))

    assert result.success
    assert (dest_root / "config.ini").read_bytes() == payload


def test_inline_file_missing_data_id_reports_error(tmp_path):
    wj_path = tmp_path / "list.wabbajack"
    _make_wabbajack_file(wj_path)
    directive = wm.InlineFileDirective(to="config.ini", hash="x==", size=1, source_data_id="nope")
    result = apply_directive(
        directive, dest_root=tmp_path / "dest", wabbajack_path=wj_path,
        archive_index=ArchiveIndex(tmp_path / "scratch"))
    assert not result.success
    assert "inline data" in result.error


def test_path_substitutions_spellings():
    table = path_substitutions(game_path="Z:\\games\\Skyrim", install_path="Z:/mosaic/list")
    assert table["{--||GAME_PATH_MAGIC_BACK||--}"] == "Z:\\games\\Skyrim"
    assert table["{--||GAME_PATH_MAGIC_DOUBLE_BACK||--}"] == "Z:\\\\games\\\\Skyrim"
    assert table["{--||GAME_PATH_MAGIC_FORWARD||--}"] == "Z:/games/Skyrim"
    assert table["{--||MO2_PATH_MAGIC_BACK||--}"] == "Z:\\mosaic\\list"
    assert not any("DOWNLOAD" in k for k in table)  # not given -> not substituted


def test_remapped_inline_file_substitutes_paths_without_hash_check(tmp_path):
    template = (b"sResourceDataDirsFinal=\n"
                b"sLocalSavePath={--||MO2_PATH_MAGIC_DOUBLE_BACK||--}\\\\saves\n"
                b"game={--||GAME_PATH_MAGIC_FORWARD||--}/Data\n")
    wj_path = tmp_path / "list.wabbajack"
    _make_wabbajack_file(wj_path, inline_files={"data-1": template})
    # The compiled Hash can't match per-user output; it must not be enforced.
    directive = wm.RemappedInlineFileDirective(
        to="profiles/Default/Skyrim.ini", hash="compile-time-hash==", size=len(template),
        source_data_id="data-1")

    dest_root = tmp_path / "dest"
    result = apply_directive(
        directive, dest_root=dest_root, wabbajack_path=wj_path,
        archive_index=ArchiveIndex(tmp_path / "scratch"),
        substitutions=path_substitutions(game_path="Z:\\g\\Skyrim", install_path="Z:\\m"))

    assert result.success, result.error
    assert (dest_root / "profiles/Default/Skyrim.ini").read_bytes() == (
        b"sResourceDataDirsFinal=\n"
        b"sLocalSavePath=Z:\\\\m\\\\saves\n"
        b"game=Z:/g/Skyrim/Data\n")


# ---------------------------------------------------------------------------
# Unsupported directive types
# ---------------------------------------------------------------------------

def _bsa_directive(temp_id="t1", hash_="", file_paths=("meshes\\a.nif",)):
    return wm.parse_directive({
        "$type": "CreateBSA, Wabbajack.Lib", "To": "Foo/Foo.bsa", "Hash": hash_, "Size": 1,
        "TempID": temp_id,
        "State": {"$type": "BSAState, Compression.BSA", "Magic": "BSA\u0000", "Version": 105,
                  "ArchiveFlags": 0x7, "FileFlags": 0x1},
        "FileStates": [{"$type": "BSAFileState, Compression.BSA", "Path": p, "Index": i,
                        "FlipCompression": False} for i, p in enumerate(file_paths)],
    })


def test_create_bsa_packs_built_inputs_and_notes_non_identical_hash(tmp_path):
    from Utils.archives.bsa_extract import extract_bsa
    temp_root = tmp_path / "bsa"
    (temp_root / "t1" / "meshes").mkdir(parents=True)
    (temp_root / "t1" / "meshes" / "a.nif").write_bytes(b"NIF DATA" * 100)

    result = apply_directive(
        _bsa_directive(hash_="curator-archive-hash=="), dest_root=tmp_path / "dest",
        wabbajack_path=tmp_path / "unused.wabbajack",
        archive_index=ArchiveIndex(tmp_path / "scratch"), bsa_temp_root=temp_root)

    assert result.success, result.error
    assert "isn't byte-identical" in result.note
    extract_bsa(tmp_path / "dest" / "Foo" / "Foo.bsa", tmp_path / "out")
    assert (tmp_path / "out" / "meshes" / "a.nif").read_bytes() == b"NIF DATA" * 100


def test_create_bsa_with_matching_hash_has_no_note(tmp_path):
    temp_root = tmp_path / "bsa"
    (temp_root / "t1" / "meshes").mkdir(parents=True)
    (temp_root / "t1" / "meshes" / "a.nif").write_bytes(b"x")
    # Build once to learn the hash this writer produces, then rebuild against it.
    first = apply_directive(_bsa_directive(), dest_root=tmp_path / "d1",
                            wabbajack_path="unused", archive_index=ArchiveIndex(tmp_path / "s"),
                            bsa_temp_root=temp_root)
    from Utils.wabbajack.wabbajack_hash import hash_file
    again = apply_directive(_bsa_directive(hash_=hash_file(first.path)), dest_root=tmp_path / "d2",
                            wabbajack_path="unused", archive_index=ArchiveIndex(tmp_path / "s"),
                            bsa_temp_root=temp_root)
    assert again.success and again.note == ""


def test_create_bsa_missing_input_fails(tmp_path):
    result = apply_directive(
        _bsa_directive(), dest_root=tmp_path / "dest", wabbajack_path="unused",
        archive_index=ArchiveIndex(tmp_path / "scratch"), bsa_temp_root=tmp_path / "bsa")
    assert not result.success and not result.unsupported
    assert "missing file" in result.error


def test_transformed_texture_is_unsupported(tmp_path):
    directive = wm.TransformedTextureDirective(to="tex.dds", hash="x==", size=1)
    result = apply_directive(
        directive, dest_root=tmp_path / "dest", wabbajack_path=tmp_path / "unused.wabbajack",
        archive_index=ArchiveIndex(tmp_path / "scratch"))
    assert not result.success
    assert result.unsupported


def test_unknown_directive_is_unsupported(tmp_path):
    directive = wm.UnknownDirective(type_name="SomeFutureDirective", to="weird/path")
    result = apply_directive(
        directive, dest_root=tmp_path / "dest", wabbajack_path=tmp_path / "unused.wabbajack",
        archive_index=ArchiveIndex(tmp_path / "scratch"))
    assert not result.success
    assert result.unsupported

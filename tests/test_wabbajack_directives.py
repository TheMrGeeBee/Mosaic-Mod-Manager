"""Tests for Utils.wabbajack.wabbajack_directives.apply_directive -- each
directive handler against tiny synthetic fixtures, including a
PatchedFromArchive round-trip through bsdiff4 and a .wabbajack container."""
from __future__ import annotations

import json
import zipfile

import bsdiff4

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.wabbajack_directives import apply_directive
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


def test_remapped_inline_file_extracts_raw_without_substitution(tmp_path):
    """Documents current (incomplete) behavior: no placeholder substitution
    is performed, so a directive whose Hash reflects the *remapped* output
    will fail its own hash check -- a loud, correct failure for an
    unfinished feature rather than installing the wrong bytes."""
    raw_template = b"%GAME_PATH%/Data/foo.esp"
    wj_path = tmp_path / "list.wabbajack"
    _make_wabbajack_file(wj_path, inline_files={"data-1": raw_template})

    # Hash matches the raw (unsubstituted) bytes -- succeeds today.
    directive_raw_hash = wm.RemappedInlineFileDirective(
        to="profiles/Default/plugins.txt", hash=hash_bytes(raw_template),
        size=len(raw_template), source_data_id="data-1")
    result = apply_directive(
        directive_raw_hash, dest_root=tmp_path / "dest", wabbajack_path=wj_path,
        archive_index=ArchiveIndex(tmp_path / "scratch"))
    assert result.success

    # Hash matching the (hypothetical) substituted output fails loudly.
    directive_remapped_hash = wm.RemappedInlineFileDirective(
        to="profiles/Default/other.txt", hash=hash_bytes(b"/real/game/path/Data/foo.esp"),
        size=1, source_data_id="data-1")
    result2 = apply_directive(
        directive_remapped_hash, dest_root=tmp_path / "dest", wabbajack_path=wj_path,
        archive_index=ArchiveIndex(tmp_path / "scratch"))
    assert not result2.success


# ---------------------------------------------------------------------------
# Unsupported directive types
# ---------------------------------------------------------------------------

def test_create_bsa_is_unsupported(tmp_path):
    directive = wm.CreateBSADirective(to="mods/Foo/Foo.bsa", hash="x==", size=1, temp_id="t")
    result = apply_directive(
        directive, dest_root=tmp_path / "dest", wabbajack_path=tmp_path / "unused.wabbajack",
        archive_index=ArchiveIndex(tmp_path / "scratch"))
    assert not result.success
    assert result.unsupported


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

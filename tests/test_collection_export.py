"""Tests for Utils.collections.collection_export — manifest building
(including FOMOD/BAIN choices embedding via the shared
Utils.profile.profile_export.resolve_installer_choices helper), collection
name validation, upload-size checks, and the .7z pack round-trip."""
from __future__ import annotations

import json

import pytest

from Utils.collections import collection_export


class _FakeGame:
    def __init__(self, staging_root, profile_dir, name="TestGame", domain="testgame"):
        self._staging_root = str(staging_root)
        self._active_profile_dir = str(profile_dir)
        self.name = name
        self.nexus_game_domain = domain

    def get_effective_mod_staging_path(self):
        return self._staging_root


def _rows(**overrides):
    row = {
        "name": "Test Mod",
        "mod_id": 123,
        "file_id": 456,
        "version": "1.0",
        "optional": False,
        "has_fomod": False,
        "has_bain": False,
        "fomod_export": True,
        "size_bytes": 0,
        "root_folder": False,
        "enabled": True,
        "source": "nexus",
        "direct_url": "",
    }
    row.update(overrides)
    return [row]


def _game(tmp_path):
    staging = tmp_path / "staging"
    profile_dir = tmp_path / "profile"
    staging.mkdir()
    profile_dir.mkdir()
    return _FakeGame(staging, profile_dir), profile_dir


# ---------------------------------------------------------------------------
# build_collection_manifest — basic shape
# ---------------------------------------------------------------------------

def test_build_collection_manifest_basic_shape(tmp_path):
    game, _ = _game(tmp_path)

    manifest, bundle_jobs, warnings = collection_export.build_collection_manifest(
        _rows(), game, {"name": "My Collection", "author": "Me", "description": "desc"})

    assert manifest["info"]["name"] == "My Collection"
    assert manifest["info"]["domainName"] == "testgame"
    assert len(manifest["mods"]) == 1
    mod = manifest["mods"][0]
    assert mod["name"] == "Test Mod"
    assert mod["source"]["modId"] == 123
    assert mod["source"]["fileId"] == 456
    assert not bundle_jobs
    assert warnings == []


def test_build_collection_manifest_skips_disabled_mods(tmp_path):
    game, _ = _game(tmp_path)

    manifest, _bundle_jobs, warnings = collection_export.build_collection_manifest(
        _rows(enabled=False), game, {"name": "My Collection"})

    assert manifest["mods"] == []
    assert len(warnings) == 1
    assert "disabled mod" in warnings[0]


# ---------------------------------------------------------------------------
# FOMOD/BAIN choices embedding (the ported gap)
# ---------------------------------------------------------------------------

def test_build_collection_manifest_embeds_fomod_choices(tmp_path):
    game, profile_dir = _game(tmp_path)
    (profile_dir / "fomod").mkdir()
    selections = {"0": {"Group A": ["Option 1"]}}
    (profile_dir / "fomod" / "Test Mod.json").write_text(json.dumps(selections))

    manifest, _bundle_jobs, warnings = collection_export.build_collection_manifest(
        _rows(has_fomod=True), game, {"name": "My Collection"})

    mod = manifest["mods"][0]
    assert mod["choices"] == {"type": "fomod_selections", "selections": selections}
    assert warnings == []


def test_build_collection_manifest_embeds_bain_choices(tmp_path):
    game, profile_dir = _game(tmp_path)
    (profile_dir / "bain").mkdir()
    selections = {"installed": ["Option A"]}
    (profile_dir / "bain" / "Test Mod.json").write_text(json.dumps(selections))

    manifest, _bundle_jobs, warnings = collection_export.build_collection_manifest(
        _rows(has_fomod=True, has_bain=True), game, {"name": "My Collection"})

    mod = manifest["mods"][0]
    assert mod["choices"] == {"type": "bain_selections", "selections": selections}
    assert warnings == []


def test_build_collection_manifest_warns_when_sidecar_missing(tmp_path):
    game, _profile_dir = _game(tmp_path)

    manifest, _bundle_jobs, warnings = collection_export.build_collection_manifest(
        _rows(has_fomod=True), game, {"name": "My Collection"})

    mod = manifest["mods"][0]
    assert "choices" not in mod
    assert len(warnings) == 1
    assert "asked to choose interactively" in warnings[0]


def test_build_collection_manifest_skips_choices_when_fomod_export_off(tmp_path):
    game, profile_dir = _game(tmp_path)
    (profile_dir / "fomod").mkdir()
    (profile_dir / "fomod" / "Test Mod.json").write_text(json.dumps({}))

    manifest, _bundle_jobs, warnings = collection_export.build_collection_manifest(
        _rows(has_fomod=True, fomod_export=False), game, {"name": "My Collection"})

    mod = manifest["mods"][0]
    assert "choices" not in mod
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# validate_collection_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expect_ok", [
    ("ab", False),
    ("abc", True),
    ("x" * 36, True),
    ("x" * 37, False),
    ("bad\x00name", False),
    ("", False),
])
def test_validate_collection_name(name, expect_ok):
    err = collection_export.validate_collection_name(name)
    assert (err == "") == expect_ok


# ---------------------------------------------------------------------------
# check_upload_size
# ---------------------------------------------------------------------------

def test_check_upload_size_small_file_is_fine(tmp_path):
    f = tmp_path / "a.7z"
    f.write_bytes(b"x" * 1024)
    ok, msg = collection_export.check_upload_size(f)
    assert ok
    assert msg == ""


def test_check_upload_size_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(collection_export, "UPLOAD_SIZE_WARN", 10)
    monkeypatch.setattr(collection_export, "UPLOAD_SIZE_LIMIT", 20)
    f = tmp_path / "a.7z"
    f.write_bytes(b"x" * 30)
    ok, msg = collection_export.check_upload_size(f)
    assert not ok
    assert "over the" in msg


def test_check_upload_size_near_limit_warns_but_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(collection_export, "UPLOAD_SIZE_WARN", 10)
    monkeypatch.setattr(collection_export, "UPLOAD_SIZE_LIMIT", 100)
    f = tmp_path / "a.7z"
    f.write_bytes(b"x" * 30)
    ok, msg = collection_export.check_upload_size(f)
    assert ok
    assert "close to the" in msg


# ---------------------------------------------------------------------------
# pack_collection round-trip
# ---------------------------------------------------------------------------

def test_pack_collection_round_trip(tmp_path):
    import py7zr

    manifest = {"info": {"name": "x"}, "mods": [{"name": "Test Mod"}]}
    out = tmp_path / "out.7z"
    final = collection_export.pack_collection(out, manifest, [])
    assert final == out

    extract_dir = tmp_path / "extracted"
    with py7zr.SevenZipFile(str(final), mode="r") as arc:
        arc.extractall(path=str(extract_dir))

    roundtripped = json.loads((extract_dir / "collection.json").read_text(encoding="utf-8"))
    assert roundtripped == manifest


# ---------------------------------------------------------------------------
# Per-mod instructions / update_policy (Phase 2: per-mod authoring controls)
# ---------------------------------------------------------------------------

def test_build_collection_manifest_writes_instructions(tmp_path):
    game, _profile_dir = _game(tmp_path)

    manifest, _bundle_jobs, warnings = collection_export.build_collection_manifest(
        _rows(instructions="Install only if you also have the base mod."),
        game, {"name": "My Collection"})

    mod = manifest["mods"][0]
    assert mod["source"]["instructions"] == "Install only if you also have the base mod."
    assert warnings == []


def test_build_collection_manifest_omits_instructions_when_blank(tmp_path):
    game, _profile_dir = _game(tmp_path)

    manifest, _bundle_jobs, _warnings = collection_export.build_collection_manifest(
        _rows(instructions=""), game, {"name": "My Collection"})

    assert "instructions" not in manifest["mods"][0]["source"]


@pytest.mark.parametrize("policy", ["exact", "prefer", "latest"])
def test_build_collection_manifest_update_policy_values(tmp_path, policy):
    game, _profile_dir = _game(tmp_path)

    manifest, _bundle_jobs, _warnings = collection_export.build_collection_manifest(
        _rows(update_policy=policy), game, {"name": "My Collection"})

    assert manifest["mods"][0]["source"]["updatePolicy"] == policy


def test_build_collection_manifest_invalid_update_policy_falls_back_to_exact(tmp_path):
    game, _profile_dir = _game(tmp_path)

    manifest, _bundle_jobs, _warnings = collection_export.build_collection_manifest(
        _rows(update_policy="not-a-real-policy"), game, {"name": "My Collection"})

    assert manifest["mods"][0]["source"]["updatePolicy"] == "exact"


def test_build_collection_manifest_browse_source_no_manual_alias(tmp_path):
    """collection_install.py's off-site branch only recognizes "browse", not
    the vestigial "manual" string an earlier version of the exporter wrote."""
    game, _profile_dir = _game(tmp_path)

    manifest, _bundle_jobs, _warnings = collection_export.build_collection_manifest(
        _rows(source="browse", direct_url="https://example.com/mod"),
        game, {"name": "My Collection"})

    source = manifest["mods"][0]["source"]
    assert source["type"] == "browse"
    assert source["url"] == "https://example.com/mod"
    # Browse is off-site/manual-only — no updatePolicy (nothing to auto-update).
    assert "updatePolicy" not in source


def test_build_collection_manifest_modio_source_maps_to_browse(tmp_path):
    """"modio" is a Mosaic-only UI label (real Nexus/Vortex collections have
    no concept of mod.io) — it must write as a real "browse" source type,
    same as an off-site webpage, or collection_install.py's off-site branch
    (which only recognizes "nexus"/"bundle"/"browse"/"direct") won't see it."""
    game, _profile_dir = _game(tmp_path)

    manifest, _bundle_jobs, _warnings = collection_export.build_collection_manifest(
        _rows(source="modio", direct_url="https://mod.io/g/baldursgate3/m/my-mod"),
        game, {"name": "My Collection"})

    source = manifest["mods"][0]["source"]
    assert source["type"] == "browse"
    assert source["url"] == "https://mod.io/g/baldursgate3/m/my-mod"
    assert "updatePolicy" not in source


def test_build_collection_manifest_synthetic_variant_row(tmp_path):
    """A row added via the "+ Variant" picker (Utils.collections.collection_export
    consumer: gui_qt.views.create_collection_view._add_variant_row) has no
    meta.ini/staged folder behind it — name/mod_id/file_id/version are set
    directly on the row instead. Confirms build_collection_manifest handles
    that shape without needing a staging lookup to succeed."""
    game, _profile_dir = _game(tmp_path)
    variant_row = {
        "name": "Texture Pack (2K)",
        "mod_id": 123,
        "file_id": 789,
        "version": "2K",
        "optional": True,
        "has_fomod": False,
        "has_bain": False,
        "fomod_export": False,
        "size_bytes": 4096,
        "root_folder": False,
        "enabled": True,
        "source": "nexus",
        "direct_url": "",
        "update_policy": "exact",
        "instructions": "",
        "is_variant": True,
    }

    manifest, _bundle_jobs, warnings = collection_export.build_collection_manifest(
        [variant_row], game, {"name": "My Collection"})

    assert len(manifest["mods"]) == 1
    mod = manifest["mods"][0]
    assert mod["name"] == "Texture Pack (2K)"
    assert mod["optional"] is True
    assert mod["source"]["modId"] == 123
    assert mod["source"]["fileId"] == 789
    assert warnings == []

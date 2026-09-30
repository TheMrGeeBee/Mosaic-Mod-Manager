"""Test for profile_export.read_manifest()'s .7z support.

Real bug: a real Nexus Collection downloads/exports as a .7z archive (this
app's own Export Collection produces the same format), but the Import
Profile picker only listed *.mosaic/*.amethyst/*.zip/*.json, and
read_manifest() only knew how to open a zip -- so importing a real
Collection required manually extracting the .7z first and pointing at the
bare collection.json inside it."""
from __future__ import annotations

import json

import py7zr
import pytest

from Utils.profile.profile_export import read_manifest


def _write_7z(path, member_name, payload: dict):
    text_path = path.parent / "_payload.json"
    text_path.write_text(json.dumps(payload), encoding="utf-8")
    with py7zr.SevenZipFile(path, "w") as zf:
        zf.write(str(text_path), member_name)
    text_path.unlink()


def test_read_manifest_from_7z_collection_json(tmp_path):
    archive = tmp_path / "BG3 Essentials Compilation.7z"
    _write_7z(archive, "collection.json", {"mods": [{"name": "A"}]})
    assert read_manifest(archive) == {"mods": [{"name": "A"}]}


def test_read_manifest_from_7z_manifest_json(tmp_path):
    archive = tmp_path / "export.7z"
    _write_7z(archive, "manifest.json", {"mods": [{"name": "B"}]})
    assert read_manifest(archive) == {"mods": [{"name": "B"}]}


def test_read_manifest_from_7z_raises_without_a_manifest_member(tmp_path):
    archive = tmp_path / "bad.7z"
    _write_7z(archive, "not_a_manifest.json", {"mods": []})
    with pytest.raises(ValueError):
        read_manifest(archive)


def test_read_manifest_still_reads_bare_json(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"mods": [{"name": "C"}]}), encoding="utf-8")
    assert read_manifest(p) == {"mods": [{"name": "C"}]}

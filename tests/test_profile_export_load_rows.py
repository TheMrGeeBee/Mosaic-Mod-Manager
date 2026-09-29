"""Tests for Utils.profile.profile_export.load_rows — in particular mod.io
mod detection and direct_url seeding.

A mod.io-identified mod (Baldur's Gate 3) has no Nexus mod_id, so load_rows
defaults its export source to "bundle". Games/Baldur's Gate 3/modio_meta.py
already stamps the mod's mod.io profile URL into meta.ini (modioProfileUrl,
written whenever known) for the update checker's use -- load_rows should
carry that URL into the row's direct_url so the Source picker's Direct/Browse
URL field isn't blank when Mosaic already knows it."""
from __future__ import annotations

from pathlib import Path

from Utils.mods.modlist import ModEntry
from Utils.profile import profile_export


class _FakeGame:
    def __init__(self, staging_root, profile_dir):
        self._staging_root = str(staging_root)
        self._active_profile_dir = str(profile_dir)

    def get_effective_mod_staging_path(self):
        return self._staging_root


def _entry(name: str) -> ModEntry:
    return ModEntry(name=name, enabled=True, locked=False)


def _write_meta_ini(path: Path, lines: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "[General]\n" + "\n".join(f"{k} = {v}" for k, v in lines.items())
    path.write_text(body, encoding="utf-8")


def test_modio_mod_defaults_to_bundle_source_with_seeded_direct_url(tmp_path):
    staging = tmp_path / "staging"
    profile_dir = tmp_path / "profile"
    _write_meta_ini(staging / "My Modio Mod" / "meta.ini", {
        "modid": "0",
        "fileid": "0",
        "modioModId": "12345",
        "modioProfileUrl": "https://mod.io/g/baldursgate3/m/my-modio-mod",
    })
    game = _FakeGame(staging, profile_dir)

    rows = profile_export.load_rows([_entry("My Modio Mod")], game)

    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "bundle"
    assert row["direct_url"] == "https://mod.io/g/baldursgate3/m/my-modio-mod"


def test_modio_mod_without_profile_url_still_defaults_to_bundle(tmp_path):
    staging = tmp_path / "staging"
    profile_dir = tmp_path / "profile"
    _write_meta_ini(staging / "Old Modio Mod" / "meta.ini", {
        "modid": "0",
        "fileid": "0",
        "modioModId": "999",
    })
    game = _FakeGame(staging, profile_dir)

    rows = profile_export.load_rows([_entry("Old Modio Mod")], game)

    assert rows[0]["source"] == "bundle"
    assert rows[0]["direct_url"] == ""


def test_nexus_mod_gets_nexus_source_and_no_direct_url(tmp_path):
    staging = tmp_path / "staging"
    profile_dir = tmp_path / "profile"
    _write_meta_ini(staging / "Nexus Mod" / "meta.ini", {
        "modid": "111",
        "fileid": "222",
        "version": "1.0",
    })
    game = _FakeGame(staging, profile_dir)

    rows = profile_export.load_rows([_entry("Nexus Mod")], game)

    assert rows[0]["source"] == "nexus"
    assert rows[0]["direct_url"] == ""

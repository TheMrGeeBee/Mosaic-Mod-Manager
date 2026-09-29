"""Tests for gui_qt.modlist.modlist_data.read_meta_for_entries' BG3 mod.io
update flag (FLAG_MODIO_UPDATE).

Caught live: after the modio_update_checker.py / modio_quick_update.py
downgrade-guard fix landed, Check Updates correctly reported "0 mod(s) need
attention" for a mod already on its live release -- but the modlist row's
Flags column still showed the "update available" icon. Root cause: this
function reads meta.ini directly and independently, with its own separate
"!=" comparison that had the exact same bug (an older mod.io live release
than what's installed was still being flagged)."""
from __future__ import annotations

from pathlib import Path

from gui_qt.modlist.modlist_data import read_meta_for_entries, FLAG_MODIO_UPDATE
from Utils.mods.modlist import ModEntry


def _write_meta_ini(path: Path, lines: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "[General]\n" + "\n".join(f"{k} = {v}" for k, v in lines.items())
    path.write_text(body, encoding="utf-8")


def _entry(name: str) -> ModEntry:
    return ModEntry(name=name, enabled=True, locked=False)


def _flags_and_updates(staging, name="Some Mod"):
    result = read_meta_for_entries([_entry(name)], staging, is_bg3=True)
    flags, updates = result[2], result[4]
    return flags.get(name, 0), updates


def test_older_live_release_does_not_set_update_flag(tmp_path):
    staging = tmp_path / "staging"
    _write_meta_ini(staging / "Some Mod" / "meta.ini", {
        "modioModId": "111",
        "modioFileId": "300",
        "modioLatestFileId": "250",  # older than installed
        "modioVersion": "2.0.0.61",
        "modioLatestVersion": "2.0.0.55",
    })

    flags, updates = _flags_and_updates(staging)

    assert not (flags & FLAG_MODIO_UPDATE)
    assert "Some Mod" not in updates


def test_genuinely_newer_live_release_sets_update_flag(tmp_path):
    staging = tmp_path / "staging"
    _write_meta_ini(staging / "Some Mod" / "meta.ini", {
        "modioModId": "111",
        "modioFileId": "300",
        "modioLatestFileId": "400",
        "modioVersion": "2.0.0.61",
        "modioLatestVersion": "2.0.0.70",
    })

    flags, updates = _flags_and_updates(staging)

    assert flags & FLAG_MODIO_UPDATE
    assert "Some Mod" in updates


def test_equal_file_id_does_not_set_update_flag(tmp_path):
    staging = tmp_path / "staging"
    _write_meta_ini(staging / "Some Mod" / "meta.ini", {
        "modioModId": "111",
        "modioFileId": "300",
        "modioLatestFileId": "300",
        "modioVersion": "2.0.0.61",
        "modioLatestVersion": "2.0.0.61",
    })

    flags, updates = _flags_and_updates(staging)

    assert not (flags & FLAG_MODIO_UPDATE)
    assert "Some Mod" not in updates

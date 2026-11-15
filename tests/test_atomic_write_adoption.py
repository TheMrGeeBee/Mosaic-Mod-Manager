"""Regression tests for the call sites that moved onto Utils.atomic_write.

Each of these used to hand-roll "write <name>.tmp, then rename". They now share
``write_atomic`` / ``write_atomic_text``. What matters to callers is unchanged:
the destination holds the complete payload, the parent directory is created on
demand, a failed write leaves the old file intact, and no temp file is left
behind.
"""
from __future__ import annotations

import json

import pytest

from Nexus import nexus_author_notes
from Utils.atomic_write import write_atomic, write_atomic_text
from Utils.modding_tools import fo4_downgrade, skyrim_runtime


def _only(directory, name):
    """The directory holds exactly *name* -- no stray temp files."""
    assert sorted(p.name for p in directory.iterdir()) == [name]


@pytest.mark.parametrize("module", [fo4_downgrade, skyrim_runtime])
def test_write_state_creates_dir_and_leaves_no_temp(module, tmp_path):
    state_dir = tmp_path / "nested" / "state"          # does not exist yet
    state = {"version": "1.2.3", "files": ["a.dll", "b.dll"]}

    module._write_state(state_dir, state)

    _only(state_dir, module.STATE_FILENAME)
    assert json.loads((state_dir / module.STATE_FILENAME).read_text("utf-8")) == state


@pytest.mark.parametrize("module", [fo4_downgrade, skyrim_runtime])
def test_write_state_overwrites_previous_record(module, tmp_path):
    module._write_state(tmp_path, {"n": 1})
    module._write_state(tmp_path, {"n": 2})

    _only(tmp_path, module.STATE_FILENAME)
    assert json.loads((tmp_path / module.STATE_FILENAME).read_text("utf-8")) == {"n": 2}


def test_author_notes_cache_round_trips_and_leaves_no_temp(tmp_path, monkeypatch):
    cache_file = tmp_path / "nexus_author_notes" / "skyrimspecialedition.json"
    monkeypatch.setattr(nexus_author_notes, "_cache_path", lambda _domain: cache_file)
    data = {"12345": {"notes": "naïve café — ünïcode", "t": 1}}

    nexus_author_notes._write_cache("skyrimspecialedition", data)

    _only(cache_file.parent, cache_file.name)
    assert nexus_author_notes.read_cache("skyrimspecialedition") == data


def test_failed_write_keeps_old_file_and_removes_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    write_atomic_text(target, "old")

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(target), "replace", boom)
    with pytest.raises(OSError):
        write_atomic_text(target, "new")
    monkeypatch.undo()

    assert target.read_text("utf-8") == "old"
    _only(tmp_path, "state.json")


def test_write_atomic_bytes_round_trip_and_suffix(tmp_path):
    target = tmp_path / "sub" / "payload.bin"
    payload = bytes(range(256))

    write_atomic(target, payload, suffix=".mm_tmp")

    assert target.read_bytes() == payload
    _only(target.parent, "payload.bin")

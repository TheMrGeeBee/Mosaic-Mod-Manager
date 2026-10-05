"""Tests for Utils.wabbajack.wabbajack_file's .wabbajack container reading.

No real .wabbajack file is available in this repo, so these build a minimal
synthetic zip matching the documented container shape (a ``modlist`` JSON
entry plus inline-file payloads named by id) rather than testing against a
real one -- see the plan's note to cross-check against a real file during
implementation.
"""
from __future__ import annotations

import json
import zipfile

import pytest

from Utils.wabbajack.wabbajack_file import WabbajackFileError, extract_inline_file, read_modlist


def _make_wabbajack_file(path, modlist_dict, inline_files=None, modlist_name="modlist"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(modlist_name, json.dumps(modlist_dict))
        for data_id, payload in (inline_files or {}).items():
            zf.writestr(data_id, payload)


def test_read_modlist_parses_basic_manifest(tmp_path):
    p = tmp_path / "test.wabbajack"
    _make_wabbajack_file(p, {"Name": "Test List", "GameType": "SkyrimSpecialEdition"})
    ml = read_modlist(p)
    assert ml.name == "Test List"
    assert ml.game_type == "SkyrimSpecialEdition"


def test_read_modlist_accepts_modlist_json_name(tmp_path):
    p = tmp_path / "test.wabbajack"
    _make_wabbajack_file(p, {"Name": "X"}, modlist_name="modlist.json")
    assert read_modlist(p).name == "X"


def test_read_modlist_tolerates_leading_slash(tmp_path):
    p = tmp_path / "test.wabbajack"
    _make_wabbajack_file(p, {"Name": "X"}, modlist_name="/modlist")
    assert read_modlist(p).name == "X"


def test_read_modlist_missing_file_raises_wabbajack_file_error(tmp_path):
    with pytest.raises(WabbajackFileError):
        read_modlist(tmp_path / "does_not_exist.wabbajack")


def test_read_modlist_not_a_zip_raises(tmp_path):
    p = tmp_path / "bad.wabbajack"
    p.write_bytes(b"not a zip file at all")
    with pytest.raises(WabbajackFileError):
        read_modlist(p)


def test_read_modlist_no_modlist_entry_raises(tmp_path):
    p = tmp_path / "empty.wabbajack"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("some_other_file.txt", "hello")
    with pytest.raises(WabbajackFileError):
        read_modlist(p)


def test_read_modlist_invalid_json_raises(tmp_path):
    p = tmp_path / "badjson.wabbajack"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("modlist", "{not valid json")
    with pytest.raises(WabbajackFileError):
        read_modlist(p)


def test_extract_inline_file_writes_payload(tmp_path):
    p = tmp_path / "test.wabbajack"
    _make_wabbajack_file(p, {"Name": "X"}, inline_files={"data-1": b"hello inline payload"})
    dest = tmp_path / "out" / "extracted.bin"
    assert extract_inline_file(p, "data-1", dest) is True
    assert dest.read_bytes() == b"hello inline payload"


def test_extract_inline_file_missing_id_returns_false(tmp_path):
    p = tmp_path / "test.wabbajack"
    _make_wabbajack_file(p, {"Name": "X"})
    assert extract_inline_file(p, "no-such-id", tmp_path / "out.bin") is False


def test_extract_inline_file_empty_id_returns_false(tmp_path):
    p = tmp_path / "test.wabbajack"
    _make_wabbajack_file(p, {"Name": "X"})
    assert extract_inline_file(p, "", tmp_path / "out.bin") is False

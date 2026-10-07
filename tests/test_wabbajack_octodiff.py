"""Tests for Utils.wabbajack.wabbajack_octodiff. Patches are built here in
the layout decoded from a real modlist's patch (Horizon 1.4.4, Better
Console): OCTODELTA, version, .NET-string hash name, int32 hash length,
SHA1 of the *output*, ">>>", then 0x60 copy / 0x80 data commands."""
from __future__ import annotations

import hashlib
import struct

import pytest

from Utils.wabbajack.wabbajack_octodiff import OctodiffError, apply_octodiff, is_octodiff


def _patch(commands, output: bytes, *, hash_override: "bytes | None" = None) -> bytes:
    digest = hash_override if hash_override is not None else hashlib.sha1(output).digest()
    out = bytearray(b"OCTODELTA" + b"\x01" + b"\x04SHA1" + struct.pack("<i", len(digest))
                    + digest + b">>>")
    for cmd in commands:
        if cmd[0] == "copy":
            out += b"\x60" + struct.pack("<qq", cmd[1], cmd[2])
        else:
            out += b"\x80" + struct.pack("<q", len(cmd[1])) + cmd[1]
    return bytes(out)


def _apply(tmp_path, source: bytes, patch: bytes):
    src, pat, dst = tmp_path / "src", tmp_path / "p.octodiff", tmp_path / "out"
    src.write_bytes(source)
    pat.write_bytes(patch)
    apply_octodiff(src, pat, dst)
    return dst


def test_data_only_patch_from_empty_source(tmp_path):
    # The real Better Console case: an empty source file, one data command.
    new = b"BetterConsole memory data " * 100
    dst = _apply(tmp_path, b"", _patch([("data", new)], new))
    assert dst.read_bytes() == new


def test_copy_and_data_commands(tmp_path):
    source = b"0123456789abcdefghij"
    new = b"abcde" + b"NEW" + b"0123"
    dst = _apply(tmp_path, source,
                 _patch([("copy", 10, 5), ("data", b"NEW"), ("copy", 0, 4)], new))
    assert dst.read_bytes() == new


def test_wrong_output_hash_raises_and_leaves_no_file(tmp_path):
    new = b"patched"
    with pytest.raises(OctodiffError, match="doesn't match"):
        _apply(tmp_path, b"", _patch([("data", new)], new, hash_override=b"\0" * 20))
    assert not (tmp_path / "out").exists()


def test_copy_past_end_of_source_raises(tmp_path):
    with pytest.raises(OctodiffError, match="past the end"):
        _apply(tmp_path, b"short", _patch([("copy", 0, 100)], b"x"))
    assert not (tmp_path / "out").exists()


def test_truncated_patch_raises(tmp_path):
    new = b"0123456789"
    with pytest.raises(OctodiffError, match="truncated"):
        _apply(tmp_path, b"", _patch([("data", new)], new)[:-4])


def test_unknown_command_raises(tmp_path):
    with pytest.raises(OctodiffError, match="unknown patch command"):
        _apply(tmp_path, b"", _patch([], b"") + b"\x42")


def test_is_octodiff():
    assert is_octodiff(b"OCTODELTA\x01\x04SHA1")
    assert not is_octodiff(b"BSDIFF40........")

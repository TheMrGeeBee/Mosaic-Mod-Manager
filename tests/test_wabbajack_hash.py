"""Tests for Utils.wabbajack.wabbajack_hash's xxHash64 encoding.

The byte-order contract (little-endian ulong -> base64, matching .NET's
``BitConverter.GetBytes``) is the one thing here that must eventually be
checked against a real ``.wabbajack`` file's ``Archives[].Hash`` value -- see
the plan's "key open risks" note. These tests pin the encoding Python-side
(independent of the file format) so a future regression is caught locally.
"""
from __future__ import annotations

import base64

import xxhash

from Utils.wabbajack.wabbajack_hash import hash_bytes, hash_file, hashes_match


def test_hash_bytes_is_little_endian_xxh64():
    data = b"Mosaic Mod Manager"
    expected = base64.b64encode(
        xxhash.xxh64(data).intdigest().to_bytes(8, "little")).decode("ascii")
    assert hash_bytes(data) == expected


def test_hash_bytes_empty_input():
    assert hash_bytes(b"") == base64.b64encode(
        xxhash.xxh64(b"").intdigest().to_bytes(8, "little")).decode("ascii")


def test_hash_file_matches_hash_bytes(tmp_path):
    data = b"some archive payload" * 1000
    p = tmp_path / "archive.bin"
    p.write_bytes(data)
    assert hash_file(p) == hash_bytes(data)


def test_hash_file_streams_in_chunks(tmp_path):
    data = bytes(range(256)) * 10_000  # bigger than a single small chunk
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    assert hash_file(p, chunk_size=37) == hash_bytes(data)


def test_hashes_match_is_whitespace_tolerant():
    assert hashes_match("  abc==  ", "abc==")
    assert not hashes_match("abc==", "def==")
    assert not hashes_match("", "abc==")

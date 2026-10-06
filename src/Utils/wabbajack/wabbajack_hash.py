"""xxHash64 helpers matching Wabbajack's hash encoding.

Wabbajack hashes every archive and every installed file with xxHash64 and
stores the digest as base64 of the raw 8-byte value in **little-endian**
order -- that's `Convert.ToBase64String(BitConverter.GetBytes(ulong))` on the
.NET side, and `BitConverter.GetBytes` is little-endian on every platform
Wabbajack actually runs on (x86/x64/ARM64 are all little-endian). This module
is the single place that encoding lives so the VFS indexer, downloaders and
directive engine never duplicate it or drift from each other.

NOTE: the little-endian assumption should be cross-checked against a real
``.wabbajack`` file's ``Archives[].Hash`` value (hash the referenced download
yourself and compare) before this is trusted for an actual install -- see the
plan's "key open risks" list.
"""
from __future__ import annotations

import base64
from pathlib import Path

import xxhash

_CHUNK_SIZE = 1 << 20  # 1 MiB, matches NexusDownloader._compute_md5's chunking


def _digest_to_b64(digest: int) -> str:
    return base64.b64encode(digest.to_bytes(8, "little")).decode("ascii")


def hash_bytes(data: bytes) -> str:
    """Wabbajack-format hash (base64 xxHash64) of an in-memory buffer."""
    return _digest_to_b64(xxhash.xxh64(data).intdigest())


def hash_file(path: "str | Path", chunk_size: int = _CHUNK_SIZE) -> str:
    """Wabbajack-format hash (base64 xxHash64) of a file on disk, streamed in
    chunks so multi-GB archives don't need to fit in memory."""
    hasher = xxhash.xxh64()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return _digest_to_b64(hasher.intdigest())


class StreamingHash:
    """Wabbajack-format hash built up from chunks (``update``) -- for
    checking data while it streams to disk."""

    def __init__(self):
        self._h = xxhash.xxh64()

    def update(self, data: bytes) -> None:
        self._h.update(data)

    def b64(self) -> str:
        return _digest_to_b64(self._h.intdigest())


def hashes_match(expected: str, actual: str) -> bool:
    """Whitespace-tolerant comparison of two base64 Wabbajack hash strings."""
    return (expected or "").strip() == (actual or "").strip()

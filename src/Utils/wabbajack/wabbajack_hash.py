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
import json
import os
import threading
import time
from pathlib import Path

import xxhash

_CHUNK_SIZE = 1 << 20  # 1 MiB, the same chunk size Utils.file_hash.file_digest reads in


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


# Hashes of downloaded archives, kept next to them so a re-run (a Repair, a
# resumed install, another list sharing downloads) doesn't read every archive
# again: LoreRim's 280 GB were re-hashed in full before each build. Keyed by
# file name; a file whose size or modification time changed is hashed again.
HASH_CACHE_NAME = ".mosaic_hashes.json"
_hash_cache_lock = threading.Lock()
_hash_caches: "dict[str, dict]" = {}


def _load_cache(folder: Path) -> dict:
    key = str(folder)
    cache = _hash_caches.get(key)
    if cache is None:
        try:
            cache = json.loads((folder / HASH_CACHE_NAME).read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, ValueError):
            cache = {}
        _hash_caches[key] = cache
    return cache


def cached_hash_file(path: "str | Path") -> str:
    """:func:`hash_file`, remembered in the file's folder by name, size and
    modification time (see ``HASH_CACHE_NAME``)."""
    path = Path(path)
    st = path.stat()
    stamp = [st.st_size, st.st_mtime_ns]
    folder = path.parent
    with _hash_cache_lock:
        entry = _load_cache(folder).get(path.name)
    if isinstance(entry, list) and len(entry) == 3 and entry[:2] == stamp:
        return entry[2]
    digest = hash_file(path)
    with _hash_cache_lock:
        _load_cache(folder)[path.name] = stamp + [digest]
        _dirty.add(str(folder))
        if time.monotonic() - _last_save.get(str(folder), 0.0) > 5:
            _save(folder)
    return digest


_dirty: "set[str]" = set()
_last_save: "dict[str, float]" = {}


def _save(folder: Path) -> None:
    key = str(folder)
    tmp = folder / (HASH_CACHE_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(_hash_caches.get(key, {})), encoding="utf-8")
        os.replace(tmp, folder / HASH_CACHE_NAME)
    except OSError:
        pass                                    # a read-only folder: just don't remember
    _dirty.discard(key)
    _last_save[key] = time.monotonic()


def flush_hash_cache() -> None:
    """Save every folder's remembered hashes not yet written."""
    with _hash_cache_lock:
        for key in list(_dirty):
            _save(Path(key))

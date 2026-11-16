"""
file_hash.py
Hash a file on disk, and remember md5 results for large archives.

``file_digest`` replaces a dozen copies of the same "open the file, read 1 MiB
chunks, feed a hash object" loop. ``cached_md5`` / ``md5_matches`` wrap it with
a small persistent cache: hashing a multi-GB archive is slow, so results are
kept in one JSON file inside the app's download cache directory.  Entries are
keyed by the archive's absolute path and invalidated when its size or mtime
changes.  We deliberately never write alongside the archive itself -- that
would pollute the user's Downloads folder or any external download locations
they have configured.

The cache file is parsed once and kept in memory; it is only re-read when the
file on disk changes (another process, a manual edit, a different download
cache directory), instead of on every lookup.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_download_cache_dir

_CACHE_FILE = "md5_cache.json"
_lock = threading.Lock()

# In-memory copy of the cache file: which file it came from, that file's
# (mtime_ns, size) when we last read or wrote it, and the parsed entries.
_mem: dict = {"path": None, "sig": None, "data": {}}


def file_digest(path: "str | Path", algo: str = "md5") -> str:
    """Return the lowercase hex digest of the file at *path*.

    *algo* is any ``hashlib`` algorithm name. These digests identify files
    (Nexus publishes md5s, Steam depots sha256) rather than protect secrets, so
    the hash is created with ``usedforsecurity=False`` -- md5 and sha1 then keep
    working on FIPS-restricted systems. Raises ``OSError`` if the file cannot be
    read.
    """
    with open(path, "rb") as fh:
        return hashlib.file_digest(
            fh, lambda: hashlib.new(algo, usedforsecurity=False)).hexdigest()


# -- md5 cache ---------------------------------------------------------------

def _cache_path() -> Path:
    return get_download_cache_dir() / _CACHE_FILE


def _signature(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load_locked() -> dict:
    """Return the cache entries, re-reading the file only if it changed."""
    path = _cache_path()
    sig = _signature(path)
    if _mem["path"] == path and _mem["sig"] == sig:
        return _mem["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _mem.update(path=path, sig=sig, data=data)
    return data


def _save_locked(data: dict) -> None:
    path = _cache_path()
    try:
        write_atomic_text(path, json.dumps(data))
    except Exception:
        return
    _mem.update(path=path, sig=_signature(path), data=data)


def _key(archive: Path) -> str:
    try:
        return str(archive.resolve())
    except Exception:
        return str(archive)


def md5_cache_get(archive: Path) -> str:
    """Return the cached md5 for *archive*, or "" if absent or stale."""
    try:
        st = archive.stat()
    except Exception:
        return ""
    key = _key(archive)
    with _lock:
        entry = _load_locked().get(key)
    if not entry:
        return ""
    if entry.get("size") != st.st_size or entry.get("mtime") != int(st.st_mtime):
        return ""
    return (entry.get("md5") or "").lower()


def md5_cache_put(archive: Path, md5_hex: str) -> None:
    """Remember *md5_hex* for *archive* at its current size and mtime."""
    try:
        st = archive.stat()
    except Exception:
        return
    key = _key(archive)
    with _lock:
        data = dict(_load_locked())
        data[key] = {"size": st.st_size, "mtime": int(st.st_mtime), "md5": md5_hex.lower()}
        _save_locked(data)


def md5_cache_forget(archive: Path) -> None:
    """Drop any cached md5 for *archive* (e.g. after deleting a bad download)."""
    key = _key(archive)
    with _lock:
        data = dict(_load_locked())
        if data.pop(key, None) is not None:
            _save_locked(data)


def cached_md5(archive: Path) -> str:
    """Return the lowercase md5 of *archive*, hashing only if it is not cached.

    Returns "" if the file cannot be read.
    """
    cached = md5_cache_get(archive)
    if cached:
        return cached
    try:
        digest = file_digest(archive, "md5")
    except Exception:
        return ""
    md5_cache_put(archive, digest)
    return digest


def md5_matches(archive: Path, expected_md5: str) -> bool:
    """Return True if *archive*'s md5 equals *expected_md5*.

    An empty *expected_md5* means "no verification requested" and returns True.
    """
    if not expected_md5:
        return True
    # An unreadable file hashes to "", which never equals a non-empty digest.
    return cached_md5(archive) == expected_md5.strip().lower()

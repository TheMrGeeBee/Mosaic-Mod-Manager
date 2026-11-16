"""Utils.file_hash: one chunked file digest plus the persistent md5 cache that
used to live (privately) in Nexus.nexus_download."""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from Utils import file_hash


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Point the md5 cache at a temp dir and reset the in-memory copy."""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(file_hash, "_cache_path", lambda: cache_dir / "md5_cache.json")
    monkeypatch.setattr(file_hash, "_mem", {"path": None, "sig": None, "data": {}})
    return cache_dir / "md5_cache.json"


@pytest.fixture
def archive(tmp_path):
    p = tmp_path / "mod.7z"
    p.write_bytes(b"not really an archive")
    return p


# -- file_digest -------------------------------------------------------------

@pytest.mark.parametrize("algo", ["md5", "sha1", "sha256"])
def test_file_digest_matches_hashlib(algo, tmp_path):
    data = os.urandom(3 * 1024 * 1024 + 17)            # several chunks + a ragged tail
    p = tmp_path / "blob.bin"
    p.write_bytes(data)

    assert file_hash.file_digest(p, algo) == hashlib.new(algo, data).hexdigest()


def test_file_digest_empty_file(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert file_hash.file_digest(p) == hashlib.md5(b"").hexdigest()


def test_file_digest_is_lowercase_hex(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"abc")
    digest = file_hash.file_digest(p, "sha256")
    assert digest == digest.lower() and len(digest) == 64


def test_file_digest_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        file_hash.file_digest(tmp_path / "nope.bin")


def test_file_digest_accepts_str_path(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"abc")
    assert file_hash.file_digest(str(p)) == hashlib.md5(b"abc").hexdigest()


# -- md5 cache ---------------------------------------------------------------

def test_cached_md5_computes_once_then_serves_from_cache(cache, archive, monkeypatch):
    calls = []
    real = file_hash.file_digest
    monkeypatch.setattr(file_hash, "file_digest", lambda *a, **k: calls.append(a) or real(*a, **k))

    first = file_hash.cached_md5(archive)
    second = file_hash.cached_md5(archive)

    assert first == second == hashlib.md5(archive.read_bytes()).hexdigest()
    assert len(calls) == 1


def test_cache_entry_invalidated_when_file_changes(cache, archive):
    old = file_hash.cached_md5(archive)
    archive.write_bytes(b"different and longer content")      # size changes

    assert file_hash.md5_cache_get(archive) == ""
    assert file_hash.cached_md5(archive) != old


def test_cache_entry_invalidated_when_mtime_changes(cache, archive):
    file_hash.cached_md5(archive)
    st = archive.stat()
    os.utime(archive, (st.st_atime, st.st_mtime + 100))      # same size, new mtime

    assert file_hash.md5_cache_get(archive) == ""


def test_cache_forget_removes_entry(cache, archive):
    file_hash.cached_md5(archive)
    assert file_hash.md5_cache_get(archive) != ""

    file_hash.md5_cache_forget(archive)

    assert file_hash.md5_cache_get(archive) == ""


def test_cache_survives_process_restart(cache, archive, monkeypatch):
    digest = file_hash.cached_md5(archive)
    monkeypatch.setattr(file_hash, "_mem", {"path": None, "sig": None, "data": {}})  # "restart"

    assert file_hash.md5_cache_get(archive) == digest


def test_cache_file_is_parsed_once_not_per_lookup(cache, archive, tmp_path, monkeypatch):
    other = tmp_path / "other.7z"
    other.write_bytes(b"other")
    file_hash.cached_md5(archive)
    file_hash.cached_md5(other)
    monkeypatch.setattr(file_hash, "_mem", {"path": None, "sig": None, "data": {}})

    reads = []
    real_loads = json.loads
    monkeypatch.setattr(file_hash.json, "loads", lambda *a, **k: reads.append(1) or real_loads(*a, **k))
    for _ in range(25):
        file_hash.md5_cache_get(archive)
        file_hash.md5_cache_get(other)

    assert len(reads) == 1


def test_external_edit_of_cache_file_is_picked_up(cache, archive):
    file_hash.cached_md5(archive)
    assert file_hash.md5_cache_get(archive) != ""

    cache.write_text("{}", encoding="utf-8")                  # someone clears the file
    st = cache.stat()
    os.utime(cache, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    assert file_hash.md5_cache_get(archive) == ""


def test_corrupt_cache_file_is_treated_as_empty(cache, archive):
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json", encoding="utf-8")

    assert file_hash.md5_cache_get(archive) == ""
    assert file_hash.cached_md5(archive) == hashlib.md5(archive.read_bytes()).hexdigest()


def test_cached_md5_of_unreadable_file_is_empty(cache, tmp_path):
    assert file_hash.cached_md5(tmp_path / "missing.7z") == ""


# -- md5_matches -------------------------------------------------------------

def test_md5_matches_empty_expected_means_no_check(cache, tmp_path):
    assert file_hash.md5_matches(tmp_path / "does-not-exist", "") is True


def test_md5_matches_true_false_and_case_insensitive(cache, archive):
    good = hashlib.md5(archive.read_bytes()).hexdigest()

    assert file_hash.md5_matches(archive, good) is True
    assert file_hash.md5_matches(archive, "  " + good.upper() + "\n") is True
    assert file_hash.md5_matches(archive, "0" * 32) is False


def test_md5_matches_false_when_file_unreadable(cache, tmp_path):
    assert file_hash.md5_matches(tmp_path / "missing.7z", "0" * 32) is False

"""A freshly downloaded archive must match the collection's md5 before it is
installed. Cache hits were already matched by sidecar/md5; without this check a
truncated or corrupt download installs silently and shows up later as a missing
file or a broken plugin."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from Nexus.nexus_download import DownloadResult
from Utils.collections.collection_install import verify_download_md5


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _dl(tmp_path: Path, data: bytes, name="a.7z", from_cache=False) -> DownloadResult:
    f = tmp_path / name
    f.write_bytes(data)
    Path(str(f) + ".fileid").write_text("5")
    return DownloadResult(success=True, file_path=f, file_name=name, mod_id=1, file_id=5,
                          from_cache=from_cache)


def test_a_matching_archive_is_returned_untouched(tmp_path):
    r = _dl(tmp_path, b"good")
    assert verify_download_md5(r, _md5(b"good")) is r
    assert r.file_path.exists()


@pytest.mark.parametrize("expected", ["", None, "   "])
def test_no_md5_in_the_manifest_means_no_check(tmp_path, expected):
    r = _dl(tmp_path, b"whatever")
    assert verify_download_md5(r, expected) is r and r.file_path.exists()


def test_cache_hits_and_failed_results_are_not_rechecked(tmp_path):
    cached = _dl(tmp_path, b"whatever", from_cache=True)
    assert verify_download_md5(cached, _md5(b"other")) is cached
    failed = DownloadResult(success=False, error="x")
    assert verify_download_md5(failed, _md5(b"other")) is failed
    assert verify_download_md5(None, _md5(b"other")) is None


def test_the_expected_md5_is_case_and_space_insensitive(tmp_path):
    r = _dl(tmp_path, b"good")
    assert verify_download_md5(r, f"  {_md5(b'good').upper()} ") is r


def test_a_corrupt_download_is_deleted_and_redownloaded_once(tmp_path):
    bad = _dl(tmp_path, b"trunc")
    calls = []

    def redownload():
        calls.append(1)
        return _dl(tmp_path, b"good")                    # same path, fresh content

    logs = []
    out = verify_download_md5(bad, _md5(b"good"), redownload, logs.append, "Some Mod")
    assert out.success and out.file_path.read_bytes() == b"good" and len(calls) == 1
    assert any("failed its md5 check" in m for m in logs)
    assert any("re-downloaded and verified" in m for m in logs)


def test_two_bad_downloads_fail_cleanly_and_leave_nothing_behind(tmp_path):
    bad = _dl(tmp_path, b"trunc")

    def redownload():
        return _dl(tmp_path, b"still-bad")

    out = verify_download_md5(bad, _md5(b"good"), redownload)
    assert not out.success and "failed its checksum twice" in out.error
    assert out.status_code == 429                        # so the browser fallback never fires
    assert not (tmp_path / "a.7z").exists() and not (tmp_path / "a.7z.fileid").exists()


def test_a_failed_redownload_reports_the_checksum_failure(tmp_path):
    bad = _dl(tmp_path, b"trunc")
    out = verify_download_md5(bad, _md5(b"good"),
                              lambda: DownloadResult(success=False, error="offline"))
    assert not out.success and "checksum" in out.error
    assert not (tmp_path / "a.7z").exists()


def test_without_a_redownloader_a_mismatch_still_fails_and_deletes(tmp_path):
    bad = _dl(tmp_path, b"trunc")
    out = verify_download_md5(bad, _md5(b"good"))
    assert not out.success and not (tmp_path / "a.7z").exists()

"""ModholdDownloader: cache reuse, SHA-256 verification, retry, cancel, entitlement."""
from __future__ import annotations

import hashlib
import threading

import pytest
import requests

from Modhold.modhold_api import (
    DownloadLink,
    ModholdAuthError,
    ModholdError,
    ModholdGone,
    ModholdNotEntitled,
    ModholdNotReady,
)
from Modhold.modhold_download import (
    ModholdDownloader,
    ModholdDownloadResult,
    _safe_name,
    failure_reason,
    find_cached,
    refusal_is_permanent,
    sidecar_path,
)

DATA = b"hello modhold archive" * 50
SHA = hashlib.sha256(DATA).hexdigest()
CORRUPT = b"x" * len(DATA)       # same length, wrong bytes


class _Api:
    def __init__(self, link=None, error=None):
        self.link, self.error, self.calls = link, error, []

    def file_download(self, file_id):
        self.calls.append(file_id)
        if self.error:
            raise self.error
        return self.link


class _Resp:
    def __init__(self, body, status=200, content_length=True):
        self._body = body
        self.status_code = status
        self.headers = {"Content-Length": str(len(body))} if content_length else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Session:
    def __init__(self, *bodies):
        self.queue = list(bodies)
        self.calls = []

    def get(self, url, stream=None, timeout=None, verify=None, headers=None):
        self.calls.append({"url": url, "headers": headers})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, _Resp) else _Resp(item)


def _link(name="Mod 1.0.zip", data=DATA, sha=SHA):
    return DownloadLink(url="https://modholdmods.com/dl/x?sig=1", size=len(data),
                        sha256=sha, name=name, expires="soon")


def _dl(tmp_path, link, *bodies, api=None):
    api = api or _Api(link)
    return ModholdDownloader(api, tmp_path, _Session(*bodies)), api


def test_downloads_verifies_and_writes_sidecar(tmp_path):
    dl, api = _dl(tmp_path, _link(), DATA)
    seen = []
    r = dl.download_file(7, progress_cb=lambda d, t: seen.append((d, t)))
    assert r.success and not r.from_cache
    assert r.file_path == tmp_path / "Mod 1.0.zip"
    assert r.file_path.read_bytes() == DATA
    assert seen[-1] == (len(DATA), len(DATA))
    assert sidecar_path(r.file_path).is_file()
    assert not list(tmp_path.glob("*.part"))


def test_signed_link_is_fetched_without_the_key(tmp_path):
    dl, _ = _dl(tmp_path, _link(), DATA)
    dl.download_file(7)
    assert dl._session.calls[0]["headers"] is None


def test_checksum_mismatch_retries_once_then_succeeds(tmp_path):
    dl, _ = _dl(tmp_path, _link(), CORRUPT, DATA)
    r = dl.download_file(7)
    assert r.success
    assert r.file_path.read_bytes() == DATA


def test_checksum_mismatch_twice_fails_and_leaves_nothing(tmp_path):
    dl, _ = _dl(tmp_path, _link(), CORRUPT, CORRUPT)
    r = dl.download_file(7)
    assert not r.success and "Checksum mismatch" in r.error
    assert list(tmp_path.iterdir()) == []


def test_size_mismatch_is_rejected(tmp_path):
    short = DATA[:-5]
    dl, _ = _dl(tmp_path, _link(), short, short)
    r = dl.download_file(7)
    assert not r.success and "Size mismatch" in r.error
    assert list(tmp_path.iterdir()) == []


def test_network_error_retries_then_reports(tmp_path):
    dl, _ = _dl(tmp_path, _link(), requests.ConnectionError("drop"),
                requests.ConnectionError("drop"))
    r = dl.download_file(7)
    assert not r.success and "drop" in r.error


def test_http_error_status_is_a_failure(tmp_path):
    dl, _ = _dl(tmp_path, _link(), _Resp(b"", 500), _Resp(b"", 500))
    assert not dl.download_file(7).success


def test_cache_hit_skips_the_api_when_the_page_data_is_known(tmp_path):
    (tmp_path / "Mod 1.0.zip").write_bytes(DATA)
    api = _Api(_link())
    dl = ModholdDownloader(api, tmp_path, _Session())
    r = dl.download_file(7, known_name="Mod 1.0.zip", known_size=len(DATA),
                         known_sha256=SHA)
    assert r.success and r.from_cache
    assert api.calls == []


def test_cache_requires_the_checksum_to_match(tmp_path):
    (tmp_path / "Mod 1.0.zip").write_bytes(b"x" * len(DATA))   # same size, wrong bytes
    assert find_cached(tmp_path, "Mod 1.0.zip", 7, SHA, len(DATA)) is None


def test_truncated_cached_archive_is_not_reused(tmp_path):
    (tmp_path / "Mod 1.0.zip").write_bytes(DATA[:10])
    assert find_cached(tmp_path, "Mod 1.0.zip", 7, SHA, len(DATA)) is None


def test_no_published_checksum_trusts_nothing(tmp_path):
    (tmp_path / "Mod 1.0.zip").write_bytes(DATA)
    assert find_cached(tmp_path, "Mod 1.0.zip", 7, "", len(DATA)) is None


def test_sidecar_lets_an_unchanged_file_skip_rehashing(tmp_path, monkeypatch):
    p = tmp_path / "Mod 1.0.zip"
    p.write_bytes(DATA)
    assert find_cached(tmp_path, p.name, 7, SHA, len(DATA)) == p   # hashes, writes sidecar
    from Modhold import modhold_download as m
    monkeypatch.setattr(m, "file_digest",
                        lambda *a, **k: pytest.fail("should not re-hash"))
    assert find_cached(tmp_path, p.name, 7, SHA, len(DATA)) == p


def test_a_changed_file_is_rehashed_despite_the_sidecar(tmp_path):
    p = tmp_path / "Mod 1.0.zip"
    p.write_bytes(DATA)
    find_cached(tmp_path, p.name, 7, SHA, len(DATA))
    p.write_bytes(b"y" * len(DATA))
    assert find_cached(tmp_path, p.name, 7, SHA, len(DATA)) is None


def test_numbered_sibling_copy_is_found(tmp_path):
    (tmp_path / "Mod 1.0 (1).zip").write_bytes(DATA)
    assert find_cached(tmp_path, "Mod 1.0.zip", 7, SHA, len(DATA)).name == "Mod 1.0 (1).zip"


def test_different_content_under_the_same_name_gets_a_numbered_name(tmp_path):
    (tmp_path / "Mod 1.0.zip").write_bytes(b"something else entirely")
    dl, _ = _dl(tmp_path, _link(), DATA)
    r = dl.download_file(7)
    assert r.success and r.file_path.name == "Mod 1.0 (1).zip"
    assert (tmp_path / "Mod 1.0.zip").read_bytes() == b"something else entirely"


@pytest.mark.parametrize("error,flag", [
    (ModholdNotEntitled("Supporters only until Dec 1.", 402), "not_entitled"),
    (ModholdAuthError("A Modhold key is needed.", 401), "not_connected"),
    (ModholdNotReady("Still being scanned.", 409), "not_ready"),
    (ModholdGone("Replaced by 1.2.", 410), "gone"),
])
def test_each_refusal_is_reported_distinctly_with_modholds_own_message(tmp_path, error, flag):
    api = _Api(error=error)
    r = ModholdDownloader(api, tmp_path, _Session()).download_file(7)
    assert not r.success and getattr(r, flag) is True
    assert [f for f in ("not_entitled", "not_connected", "not_ready", "gone")
            if getattr(r, f)] == [flag]                 # exactly one reason
    assert r.error == str(error)


def test_failure_reason_shows_the_server_message_as_sent():
    r = ModholdDownloadResult(False, error="Supporters only until Dec 1.", not_entitled=True)
    assert failure_reason(r) == "Supporters only until Dec 1."
    assert failure_reason(ModholdDownloadResult(False, error="Still scanning.", not_ready=True)) \
        == "Still scanning. Try again in a few minutes."


def test_failure_reason_falls_back_when_the_message_is_empty():
    assert "Pass" in failure_reason(ModholdDownloadResult(False, not_entitled=True))
    assert "Connect" in failure_reason(ModholdDownloadResult(False, not_connected=True))
    assert "scanning" in failure_reason(ModholdDownloadResult(False, not_ready=True))
    assert "replaced" in failure_reason(ModholdDownloadResult(False, gone=True))
    assert failure_reason(ModholdDownloadResult(False)) == "The download failed."


def test_only_refusals_that_will_not_change_are_permanent():
    perm = lambda **k: refusal_is_permanent(ModholdDownloadResult(False, **k))   # noqa: E731
    assert perm(not_entitled=True) and perm(gone=True)
    assert not perm(not_connected=True) and not perm(not_ready=True) and not perm()


def test_other_api_errors_are_plain_failures(tmp_path):
    api = _Api(error=ModholdError("server error", 500))
    r = ModholdDownloader(api, tmp_path, _Session()).download_file(7)
    assert not r.success and not r.not_entitled


def test_cancel_before_start_makes_no_calls(tmp_path):
    ev = threading.Event()
    ev.set()
    dl, api = _dl(tmp_path, _link(), DATA)
    r = dl.download_file(7, cancel=ev)
    assert r.cancelled and api.calls == []


def test_cancel_mid_stream_removes_the_partial(tmp_path):
    ev = threading.Event()
    big = b"z" * (600 * 1024)
    link = _link(data=big, sha=hashlib.sha256(big).hexdigest())
    dl, _ = _dl(tmp_path, link, big)
    r = dl.download_file(7, cancel=ev,
                         progress_cb=lambda d, t: ev.set())
    assert r.cancelled
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "passwd"),
    ("a\\b\\c.zip", "c.zip"),
    ('we:ird*na?me.zip', "we_ird_na_me.zip"),
    ("", "modhold_9.zip"),
    ("..", "modhold_9.zip"),
])
def test_archive_names_are_sanitised(raw, expected):
    assert _safe_name(raw, 9) == expected

"""Tests for Utils.wabbajack.downloaders.loverslab_source against a faked
requests.Session -- direct file responses, the HTML download-page hop, and
the needs_auth reporting for a missing or expired login."""
from __future__ import annotations

import threading

import pytest

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.downloaders import loverslab_source as ll

_FILE_PAGE = "https://www.loverslab.com/files/file/123-some-mod/"
_DOWNLOAD_PAGE = (
    '<a href="https://www.loverslab.com/files/file/123-some-mod/'
    '?do=download&amp;r=456&amp;confirm=1&amp;t=1&amp;csrfKey=xyz">Download</a>')


class _FakeResponse:
    def __init__(self, *, url, body=b"", text="", headers=None, status=200):
        self.url = url
        self._body = body
        self.text = text
        self.headers = headers or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ll.requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.cookies = {}
        self.requested = []

    def get(self, url, **kw):
        self.requested.append(url)
        return self.routes[url]

    def close(self):
        pass


def _html(url, text):
    return _FakeResponse(url=url, text=text, headers={"Content-Type": "text/html; charset=UTF-8"})


def _use(monkeypatch, routes):
    session = _FakeSession(routes)
    monkeypatch.setattr(ll.requests, "Session", lambda: session)
    return session


def test_follows_download_page_link_and_sends_cookies(tmp_path, monkeypatch):
    direct = ("https://www.loverslab.com/files/file/123-some-mod/"
              "?do=download&r=456&confirm=1&t=1&csrfKey=xyz")
    session = _use(monkeypatch, {
        _FILE_PAGE: _html(_FILE_PAGE, _DOWNLOAD_PAGE),
        direct: _FakeResponse(url=direct, body=b"archive bytes"),
    })
    dest = tmp_path / "mod.7z"
    result = ll.download_loverslab(
        wm.LoversLabState(url=_FILE_PAGE), dest, cookies={"ips4_member_id": "4242"})

    assert result.success, result.error
    assert dest.read_bytes() == b"archive bytes"
    assert session.requested == [_FILE_PAGE, direct]
    assert session.cookies == {"ips4_member_id": "4242"}


def test_streams_directly_when_url_serves_file(tmp_path, monkeypatch):
    _use(monkeypatch, {_FILE_PAGE: _FakeResponse(url=_FILE_PAGE, body=b"bytes")})
    dest = tmp_path / "mod.7z"
    result = ll.download_loverslab(
        wm.LoversLabState(url=_FILE_PAGE), dest, cookies={"ips4_member_id": "1"})
    assert result.success
    assert dest.read_bytes() == b"bytes"


def test_no_stored_session_needs_auth(monkeypatch):
    monkeypatch.setattr(ll.loverslab_auth, "load_session", lambda: {})
    monkeypatch.setattr(ll.requests, "Session", lambda: pytest.fail("no network expected"))
    result = ll.download_loverslab(wm.LoversLabState(url=_FILE_PAGE), "ignored")
    assert not result.success
    assert result.needs_auth


def test_expired_session_redirected_to_login_needs_auth(tmp_path, monkeypatch):
    _use(monkeypatch, {_FILE_PAGE: _html(
        "https://www.loverslab.com/login/", '<input name="_processLogin" value="x">')})
    result = ll.download_loverslab(
        wm.LoversLabState(url=_FILE_PAGE), tmp_path / "mod.7z", cookies={"ips4_member_id": "1"})
    assert not result.success
    assert result.needs_auth


def test_page_without_download_link_reports_error(tmp_path, monkeypatch):
    _use(monkeypatch, {_FILE_PAGE: _html(_FILE_PAGE, "<html>no links here</html>")})
    result = ll.download_loverslab(
        wm.LoversLabState(url=_FILE_PAGE), tmp_path / "mod.7z", cookies={"ips4_member_id": "1"})
    assert not result.success
    assert not result.needs_auth
    assert "download link" in result.error


def test_no_url_short_circuits():
    result = ll.download_loverslab(wm.LoversLabState(url=""), "ignored", cookies={"a": "b"})
    assert not result.success


def test_cancel_aborts_and_cleans_up(tmp_path, monkeypatch):
    _use(monkeypatch, {_FILE_PAGE: _FakeResponse(url=_FILE_PAGE, body=b"x" * 10)})
    cancel = threading.Event()
    cancel.set()
    dest = tmp_path / "mod.7z"
    result = ll.download_loverslab(
        wm.LoversLabState(url=_FILE_PAGE), dest, cancel=cancel, cookies={"ips4_member_id": "1"})
    assert not result.success
    assert result.error == "cancelled"
    assert not dest.exists()

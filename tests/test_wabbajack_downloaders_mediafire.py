"""Tests for Utils.wabbajack.downloaders.mediafire_source. No real network
access -- requests.get is monkeypatched with scripted fakes."""
from __future__ import annotations

import threading

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.downloaders import mediafire_source as mf


class _FakeResponse:
    def __init__(self, *, body=b"", text="", headers=None, status=200):
        self._body = body
        self.text = text
        self.headers = headers or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mf.requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_PAGE_HTML = (
    '<html><body><a aria-label="Download file" '
    'href="https://download2376.mediafire.com/abc123/def456/mymod.7z" '
    'id="downloadButton">Download</a></body></html>'
)


def test_download_mediafire_scrapes_direct_link_and_downloads(tmp_path, monkeypatch):
    body = b"the actual archive bytes"
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == "https://www.mediafire.com/file/abc/mymod.7z":
            return _FakeResponse(text=_PAGE_HTML)
        return _FakeResponse(body=body)

    monkeypatch.setattr(mf.requests, "get", fake_get)
    state = wm.MediaFireState(url="https://www.mediafire.com/file/abc/mymod.7z")
    dest = tmp_path / "mymod.7z"
    result = mf.download_mediafire(state, dest)

    assert result.success
    assert dest.read_bytes() == body
    assert calls == [
        "https://www.mediafire.com/file/abc/mymod.7z",
        "https://download2376.mediafire.com/abc123/def456/mymod.7z",
    ]


def test_download_mediafire_no_url_short_circuits():
    result = mf.download_mediafire(wm.MediaFireState(url=""), "ignored")
    assert not result.success
    assert "URL" in result.error


def test_download_mediafire_no_direct_link_found_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mf.requests, "get", lambda url, **kw: _FakeResponse(text="<html>nope</html>"))
    result = mf.download_mediafire(
        wm.MediaFireState(url="https://www.mediafire.com/file/abc/mymod.7z"), tmp_path / "a.7z")
    assert not result.success
    assert "direct download link" in result.error


def test_download_mediafire_page_fetch_failure_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mf.requests, "get", lambda url, **kw: _FakeResponse(status=404))
    result = mf.download_mediafire(
        wm.MediaFireState(url="https://www.mediafire.com/file/gone/mymod.7z"), tmp_path / "a.7z")
    assert not result.success
    assert "page fetch failed" in result.error


def test_download_mediafire_cancel_aborts_and_cleans_up(tmp_path, monkeypatch):
    def fake_get(url, **kwargs):
        if "mediafire.com/file" in url:
            return _FakeResponse(text=_PAGE_HTML)
        return _FakeResponse(body=b"x" * 50)

    monkeypatch.setattr(mf.requests, "get", fake_get)
    cancel = threading.Event()
    cancel.set()
    dest = tmp_path / "a.7z"
    result = mf.download_mediafire(
        wm.MediaFireState(url="https://www.mediafire.com/file/abc/mymod.7z"), dest, cancel=cancel)

    assert not result.success
    assert result.error == "cancelled"
    assert not dest.exists()

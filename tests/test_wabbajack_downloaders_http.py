"""Tests for Utils.wabbajack.downloaders.http_source (direct HTTP + Wabbajack
CDN downloads). No real network access -- ``requests.get``/``requests.Session``
calls are monkeypatched with small fakes, matching the repo's existing
monkeypatch-the-module convention (see test_collection_install_modio_offsite.py)
rather than a requests-mocking library.
"""
from __future__ import annotations

import threading

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.downloaders import http_source


class _FakeResponse:
    def __init__(self, *, body=b"", headers=None, json_data=None, status=200):
        self._body = body
        self.headers = headers or {}
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise http_source.requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def json(self):
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_http_writes_file_and_reports_bytes(tmp_path, monkeypatch):
    body = b"archive payload bytes"
    monkeypatch.setattr(
        http_source.requests, "get",
        lambda url, **kw: _FakeResponse(body=body, headers={"Content-Length": str(len(body))}))

    state = wm.HttpState(url="https://example.com/a.7z")
    dest = tmp_path / "a.7z"
    result = http_source.download_http(state, dest)

    assert result.success
    assert result.bytes_downloaded == len(body)
    assert dest.read_bytes() == body


def test_download_http_passes_headers(tmp_path, monkeypatch):
    captured = {}

    def fake_get(url, **kw):
        captured.update(kw)
        return _FakeResponse(body=b"x")

    monkeypatch.setattr(http_source.requests, "get", fake_get)
    state = wm.HttpState(url="https://example.com/a.7z", headers=["Authorization:Bearer tok"])
    http_source.download_http(state, tmp_path / "a.7z")
    assert captured["headers"] == {"Authorization": "Bearer tok"}


def test_download_http_no_url_fails_without_network_call():
    result = http_source.download_http(wm.HttpState(url=""), "ignored")
    assert not result.success
    assert "URL" in result.error


def test_download_http_cancel_event_aborts_and_cleans_up(tmp_path, monkeypatch):
    body = b"x" * 10
    monkeypatch.setattr(
        http_source.requests, "get", lambda url, **kw: _FakeResponse(body=body))
    cancel = threading.Event()
    cancel.set()
    dest = tmp_path / "a.7z"
    result = http_source.download_http(wm.HttpState(url="https://x/a"), dest, cancel=cancel)
    assert not result.success
    assert result.error == "cancelled"
    assert not dest.exists()


def test_download_http_raises_http_error_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        http_source.requests, "get", lambda url, **kw: _FakeResponse(status=404))
    result = http_source.download_http(wm.HttpState(url="https://x/missing"), tmp_path / "a.7z")
    assert not result.success


# ---------------------------------------------------------------------------
# WabbajackCDN
# ---------------------------------------------------------------------------

def test_download_wabbajack_cdn_concatenates_parts(tmp_path, monkeypatch):
    definition = _FakeResponse(json_data={
        "Size": 10,
        "Parts": [{"Url": "https://cdn/part1", "Size": 5}, {"Url": "https://cdn/part2", "Size": 5}],
    })
    part_bodies = {"https://cdn/part1": b"AAAAA", "https://cdn/part2": b"BBBBB"}

    def fake_get(url, **kw):
        if url == "https://cdn/definition":
            return definition
        return _FakeResponse(body=part_bodies[url])

    monkeypatch.setattr(http_source.requests, "get", fake_get)
    state = wm.WabbajackCDNState(url="https://cdn/definition")
    dest = tmp_path / "archive.7z"
    result = http_source.download_wabbajack_cdn(state, dest)

    assert result.success
    assert dest.read_bytes() == b"AAAAABBBBB"
    assert result.bytes_downloaded == 10


def test_download_wabbajack_cdn_no_parts_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        http_source.requests, "get",
        lambda url, **kw: _FakeResponse(json_data={"Parts": []}))
    result = http_source.download_wabbajack_cdn(
        wm.WabbajackCDNState(url="https://cdn/definition"), tmp_path / "a.7z")
    assert not result.success
    assert "no parts" in result.error


def test_download_wabbajack_cdn_part_missing_url_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(
        http_source.requests, "get",
        lambda url, **kw: _FakeResponse(json_data={"Parts": [{"Size": 1}]}))
    dest = tmp_path / "a.7z"
    result = http_source.download_wabbajack_cdn(wm.WabbajackCDNState(url="https://cdn/d"), dest)
    assert not result.success
    assert not dest.exists()


def test_download_wabbajack_cdn_no_url_fails():
    result = http_source.download_wabbajack_cdn(wm.WabbajackCDNState(url=""), "ignored")
    assert not result.success

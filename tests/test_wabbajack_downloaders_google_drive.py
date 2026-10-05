"""Tests for Utils.wabbajack.downloaders.google_drive_source. No real network
access -- a fake requests.Session is monkeypatched in, scripted per test."""
from __future__ import annotations

import threading

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.downloaders import google_drive_source as gd


class _FakeResponse:
    def __init__(self, *, body=b"", headers=None, cookies=None, text="", status=200):
        self._body = body
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.text = text
        self.status_code = status
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise gd.requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(dict(params or {}))
        return self._responses.pop(0)

    def close(self):
        pass


def test_download_no_confirm_needed(tmp_path, monkeypatch):
    body = b"small file, no interstitial"
    session = _FakeSession([_FakeResponse(body=body, headers={"Content-Length": str(len(body))})])
    monkeypatch.setattr(gd.requests, "Session", lambda: session)

    dest = tmp_path / "a.7z"
    result = gd.download_google_drive(wm.GoogleDriveState(file_id="abc123"), dest)

    assert result.success
    assert dest.read_bytes() == body
    assert session.calls == [{"id": "abc123"}]


def test_download_confirm_via_cookie(tmp_path, monkeypatch):
    body = b"large file contents after confirm"
    interstitial = _FakeResponse(cookies={"download_warning_xyz": "tok123"})
    real = _FakeResponse(body=body)
    session = _FakeSession([interstitial, real])
    monkeypatch.setattr(gd.requests, "Session", lambda: session)

    dest = tmp_path / "a.7z"
    result = gd.download_google_drive(wm.GoogleDriveState(file_id="abc123"), dest)

    assert result.success
    assert dest.read_bytes() == body
    assert interstitial.closed
    assert session.calls == [{"id": "abc123"}, {"id": "abc123", "confirm": "tok123"}]


def test_download_confirm_via_html_body(tmp_path, monkeypatch):
    body = b"large file contents after html confirm"
    interstitial = _FakeResponse(
        headers={"Content-Type": "text/html; charset=utf-8"},
        text="<html>...confirm=ABC-999&id=abc123...</html>")
    real = _FakeResponse(body=body)
    session = _FakeSession([interstitial, real])
    monkeypatch.setattr(gd.requests, "Session", lambda: session)

    dest = tmp_path / "a.7z"
    result = gd.download_google_drive(wm.GoogleDriveState(file_id="abc123"), dest)

    assert result.success
    assert dest.read_bytes() == body
    assert session.calls[1]["confirm"] == "ABC-999"


def test_download_no_file_id_short_circuits():
    result = gd.download_google_drive(wm.GoogleDriveState(file_id=""), "ignored")
    assert not result.success
    assert "file id" in result.error


def test_download_cancel_aborts_and_cleans_up(tmp_path, monkeypatch):
    session = _FakeSession([_FakeResponse(body=b"x" * 100)])
    monkeypatch.setattr(gd.requests, "Session", lambda: session)
    cancel = threading.Event()
    cancel.set()

    dest = tmp_path / "a.7z"
    result = gd.download_google_drive(wm.GoogleDriveState(file_id="abc123"), dest, cancel=cancel)

    assert not result.success
    assert result.error == "cancelled"
    assert not dest.exists()

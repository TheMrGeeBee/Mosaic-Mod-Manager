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
        self.urls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(dict(params or {}))
        self.urls.append(url)
        return self._responses.pop(0)

    def close(self):
        pass


# The page Drive actually serves for a file over its virus-scan threshold,
# captured 2026-11-06 from the share LoreRim uses for "Vigilant Boss Moveset".
# The confirm value is a hidden form field -- there is no ``confirm=`` text
# anywhere on the page -- and the form carries a one-off uuid with it.
_WARNING_PAGE = (
    '<!DOCTYPE html><html><head><title>Google Drive - Virus scan warning</title>'
    '</head><body><form id="download-form" action="https://drive.usercontent.google.com/download"'
    ' method="get"><input type="submit" id="uc-download-link" value="Download anyway"/>'
    '<input type="hidden" name="id" value="abc123">'
    '<input type="hidden" name="export" value="download">'
    '<input type="hidden" name="confirm" value="t">'
    '<input type="hidden" name="uuid" value="4c99b17b-0a51-4607-90df-8db8f6294d0d">'
    '</form></body></html>')


def test_download_no_confirm_needed(tmp_path, monkeypatch):
    body = b"small file, no interstitial"
    session = _FakeSession([_FakeResponse(body=body, headers={"Content-Length": str(len(body))})])
    monkeypatch.setattr(gd.requests, "Session", lambda: session)

    dest = tmp_path / "a.7z"
    result = gd.download_google_drive(wm.GoogleDriveState(file_id="abc123"), dest)

    assert result.success
    assert dest.read_bytes() == body
    assert session.calls == [{"id": "abc123", "export": "download"}]


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
    assert session.calls == [
        {"id": "abc123", "export": "download"},
        {"id": "abc123", "export": "download", "confirm": "tok123"}]


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


def test_download_confirms_through_the_warning_forms_hidden_fields(tmp_path, monkeypatch):
    """The shape Drive serves today: the confirm value is a form field, and
    the request has to carry the page's one-off uuid back with it."""
    body = b"the real archive bytes"
    interstitial = _FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"},
                                 text=_WARNING_PAGE)
    session = _FakeSession([interstitial, _FakeResponse(body=body)])
    monkeypatch.setattr(gd.requests, "Session", lambda: session)

    dest = tmp_path / "a.zip"
    result = gd.download_google_drive(wm.GoogleDriveState(file_id="abc123"), dest)

    assert result.success
    assert dest.read_bytes() == body
    assert interstitial.closed
    assert session.urls[1] == "https://drive.usercontent.google.com/download"
    assert session.calls[1] == {"id": "abc123", "export": "download",
                                "confirm": "t",
                                "uuid": "4c99b17b-0a51-4607-90df-8db8f6294d0d"}


def test_a_warning_page_is_never_written_out_as_the_file(tmp_path, monkeypatch):
    """Writing it was reported as a successful download, and only the
    installer's hash check noticed -- as the modlist's file not matching."""
    page = _FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"},
                         text=_WARNING_PAGE, body=_WARNING_PAGE.encode())
    still_page = _FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"},
                               text="<html>no</html>", body=b"<html>no</html>")
    session = _FakeSession([page, still_page])
    monkeypatch.setattr(gd.requests, "Session", lambda: session)

    dest = tmp_path / "a.zip"
    result = gd.download_google_drive(wm.GoogleDriveState(file_id="abc123"), dest)

    assert not result.success
    assert "web page instead of the file" in result.error
    assert not dest.exists()


def test_confirm_request_falls_back_to_a_bare_token_then_to_t():
    url, params = gd._confirm_request("<html>...confirm=ABC-999&id=abc123...</html>", "abc123")
    assert url == gd._DOWNLOAD_URL
    assert params["confirm"] == "ABC-999"

    url, params = gd._confirm_request("<html>nothing useful here</html>", "abc123")
    assert url == gd._DOWNLOAD_URL
    assert params == {"id": "abc123", "export": "download", "confirm": "t"}

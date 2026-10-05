"""Tests for Utils.wabbajack.downloaders.loverslab_auth -- the IPS4 login
flow (against a faked requests.Session) and session storage in both the
keyring and the encrypted-file fallback. No real network or keyring."""
from __future__ import annotations

import pytest
import requests

from Utils.wabbajack.downloaders import loverslab_auth as auth

_LOGIN_PAGE = '<form><input type="hidden" name="csrfKey" value="abc&amp;123"></form>'


class _FakeResponse:
    def __init__(self, *, text="", status=200, headers=None):
        self.text = text
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class _FakeSession:
    def __init__(self, *, page, post_text="", post_cookies=None):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self._page = page
        self._post_text = post_text
        self._post_cookies = post_cookies or {}
        self.posted = None

    def get(self, url, **kw):
        return self._page

    def post(self, url, data=None, **kw):
        self.posted = data
        for k, v in self._post_cookies.items():
            self.cookies.set(k, v)
        return _FakeResponse(text=self._post_text)

    def close(self):
        pass


def _use_session(monkeypatch, session):
    monkeypatch.setattr(auth.requests, "Session", lambda: session)


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

def test_login_success_returns_cookies_and_posts_form(monkeypatch):
    session = _FakeSession(
        page=_FakeResponse(text=_LOGIN_PAGE),
        post_cookies={"ips4_member_id": "4242", "ips4_login_key": "k"})
    _use_session(monkeypatch, session)

    cookies = auth.login("someone", "hunter2")

    assert cookies["ips4_member_id"] == "4242"
    assert session.posted == {
        "csrfKey": "abc&123", "auth": "someone", "password": "hunter2",
        "remember_me": "1", "_processLogin": "usernamepassword",
    }


def test_login_wrong_password(monkeypatch):
    _use_session(monkeypatch, _FakeSession(
        page=_FakeResponse(text=_LOGIN_PAGE), post_cookies={"ips4_member_id": "0"},
        post_text="<p>The password you entered is incorrect</p>"))
    with pytest.raises(auth.LoversLabAuthError, match="rejected"):
        auth.login("someone", "wrong")


def test_login_two_factor_reported_distinctly(monkeypatch):
    _use_session(monkeypatch, _FakeSession(
        page=_FakeResponse(text=_LOGIN_PAGE), post_text='<div class="ipsMfa">enter code</div>'))
    with pytest.raises(auth.LoversLabAuthError, match="two-factor"):
        auth.login("someone", "hunter2")


def test_login_cloudflare_challenge_reported_distinctly(monkeypatch):
    _use_session(monkeypatch, _FakeSession(
        page=_FakeResponse(text="Just a moment...", status=503)))
    with pytest.raises(auth.LoversLabAuthError, match="browser-verification"):
        auth.login("someone", "hunter2")


def test_login_missing_csrf_field(monkeypatch):
    _use_session(monkeypatch, _FakeSession(page=_FakeResponse(text="<html>changed</html>")))
    with pytest.raises(auth.LoversLabAuthError, match="login form"):
        auth.login("someone", "hunter2")


def test_login_empty_credentials_never_hits_network(monkeypatch):
    monkeypatch.setattr(auth.requests, "Session", lambda: pytest.fail("no network expected"))
    with pytest.raises(auth.LoversLabAuthError):
        auth.login("", "")


# ---------------------------------------------------------------------------
# session storage
# ---------------------------------------------------------------------------

def test_session_round_trip_via_keyring(monkeypatch):
    store = {}
    monkeypatch.setattr(auth, "_keyring_ok", lambda: True)
    monkeypatch.setattr(auth.keyring, "set_password",
                        lambda svc, user, val: store.__setitem__((svc, user), val))
    monkeypatch.setattr(auth.keyring, "get_password",
                        lambda svc, user: store.get((svc, user)))
    monkeypatch.setattr(auth.keyring, "delete_password",
                        lambda svc, user: store.pop((svc, user)))

    auth.save_session({"ips4_member_id": "4242"})
    assert auth.load_session() == {"ips4_member_id": "4242"}
    auth.clear_session()
    assert auth.load_session() == {}


def test_session_round_trip_via_encrypted_file(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_keyring_ok", lambda: False)
    monkeypatch.setattr(auth, "get_config_dir", lambda: tmp_path)

    auth.save_session({"ips4_member_id": "4242"})
    stored = (tmp_path / "loverslab_session.bin").read_bytes()
    assert b"4242" not in stored  # encrypted, not plaintext
    assert auth.load_session() == {"ips4_member_id": "4242"}
    auth.clear_session()
    assert auth.load_session() == {}


def test_load_session_with_nothing_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_keyring_ok", lambda: False)
    monkeypatch.setattr(auth, "get_config_dir", lambda: tmp_path)
    assert auth.load_session() == {}

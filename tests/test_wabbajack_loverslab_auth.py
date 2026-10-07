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


# The code form exactly as LoversLab serves it (structure seen 2026-10-07;
# values made up): it posts back to the page with hidden mfa_auth + csrfKey.
_MFA_URL = "https://www.loverslab.com/?&_mfaLogin=1"
_MFA_PAGE = """
<form action="https://www.loverslab.com/login/" method="post">
  <input type="hidden" name="csrfKey" value="loginform"><input type="email" name="auth">
</form>
<form action="https://www.loverslab.com/?&amp;_mfaLogin=1" method="post">
  <input type="hidden" name="mfa_auth" value="1">
  <input type="hidden" name="csrfKey" value="{csrf}">
  <input type="text" name="google_authenticator_auth_code" autocomplete="one-time-code">
  <button type="submit">Verify Code</button>
</form>"""


class _SeqSession(_FakeSession):
    """Answers each POST from a list of (text, cookies) in turn."""

    def __init__(self, page, posts):
        super().__init__(page=page)
        self._posts = list(posts)
        self.post_log = []
        self.closed = False

    def post(self, url, data=None, **kw):
        self.post_log.append((url, dict(data or {})))
        text, cookies = self._posts.pop(0)
        for k, v in cookies.items():
            self.cookies.set(k, v)
        r = _FakeResponse(text=text)
        r.url = _MFA_URL
        return r

    def close(self):
        self.closed = True


def _mfa_session(*after_code):
    return _SeqSession(_FakeResponse(text=_LOGIN_PAGE),
                       [(_MFA_PAGE.format(csrf="first"), {}), *after_code])


def test_login_with_two_factor_returns_a_code_challenge(monkeypatch):
    session = _mfa_session()
    _use_session(monkeypatch, session)
    with pytest.raises(auth.LoversLabMfaRequired) as info:
        auth.login("someone", "hunter2")
    ch = info.value.challenge
    assert ch.action == _MFA_URL
    assert ch.hidden == {"mfa_auth": "1", "csrfKey": "first"}
    assert ch.code_field == "google_authenticator_auth_code"
    assert not session.closed            # kept open for complete_mfa


def test_complete_mfa_posts_the_code_form_and_returns_cookies(monkeypatch):
    session = _mfa_session(("<html>welcome</html>", {"ips4_member_id": "4242"}))
    _use_session(monkeypatch, session)
    with pytest.raises(auth.LoversLabMfaRequired) as info:
        auth.login("someone", "hunter2")
    cookies = auth.complete_mfa(info.value.challenge, " 123 456 ")
    assert cookies["ips4_member_id"] == "4242"
    assert session.post_log[-1] == (_MFA_URL, {
        "mfa_auth": "1", "csrfKey": "first", "google_authenticator_auth_code": "123456"})
    assert session.closed


def test_a_wrong_code_can_be_retried_with_the_refreshed_form(monkeypatch):
    session = _mfa_session((_MFA_PAGE.format(csrf="second"), {}),
                           ("ok", {"ips4_member_id": "7"}))
    _use_session(monkeypatch, session)
    with pytest.raises(auth.LoversLabMfaRequired) as info:
        auth.login("someone", "hunter2")
    ch = info.value.challenge
    with pytest.raises(auth.LoversLabAuthError, match="didn't accept that code"):
        auth.complete_mfa(ch, "000000")
    assert not session.closed
    assert auth.complete_mfa(ch, "654321")["ips4_member_id"] == "7"
    assert session.post_log[-1][1]["csrfKey"] == "second"


def test_unrecognised_two_factor_page_logs_structure_without_values(monkeypatch):
    _use_session(monkeypatch, _FakeSession(
        page=_FakeResponse(text=_LOGIN_PAGE),
        post_text='<div class="ipsMfa"><form action="/x?csrfKey=SECRET">'
                  '<input type="hidden" name="q" value="SECRET"></form></div>'))
    with pytest.raises(auth.LoversLabAuthError, match="doesn't recognise") as info:
        auth.login("someone", "hunter2")
    assert "SECRET" not in info.value.diagnostic


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


def test_login_trace_logs_cookie_names_and_page_messages_but_no_values(monkeypatch):
    logs = []
    _use_session(monkeypatch, _FakeSession(
        page=_FakeResponse(text=_LOGIN_PAGE),
        post_cookies={"ips4_IPSSessionFront": "SESSIONSECRET", "ips4_member_id": "0"},
        post_text='<div class="ipsMessage ipsMessage_error">The password you entered '
                  'is incorrect</div>'))
    with pytest.raises(auth.LoversLabAuthError, match="rejected"):
        auth.login("someone", "hunter2", log=logs.append)
    assert len(logs) == 1
    line = logs[0]
    assert "ips4_IPSSessionFront" in line and "SESSIONSECRET" not in line
    assert "hunter2" not in line and "The password you entered is incorrect" in line

"""LoversLab login and session storage for the Wabbajack LoversLab downloader.

LoversLab runs Invision Community (IPS4): logging in is a GET of the login
page for its ``csrfKey`` hidden field, then a form POST of that key plus the
username/password. A successful login sets an ``ips4_member_id`` cookie to
the (non-zero) member id; that cookie set is the whole session.

Only the resulting session cookies are stored -- never the password. They go
in the system keyring under the same service name as the Nexus API key, with
the same machine-bound encrypted-file fallback (``Nexus.nexus_api``'s
``_derive_key``) when no keyring is available.

The username/password step was confirmed against the live site on
2026-10-07: for an account with two-factor auth it lands on
``/?&_mfaLogin=1`` with a code form (hidden ``mfa_auth`` + ``csrfKey``, text
``google_authenticator_auth_code``). :func:`login` then raises
:class:`LoversLabMfaRequired` carrying the open session, and
:func:`complete_mfa` posts that form back with the user's code. A
Cloudflare bot challenge in front of the site is reported distinctly.
"""
from __future__ import annotations

import html
import json
import os
import re

import keyring
import requests

from Nexus.nexus_api import _KEYRING_SERVICE, _derive_key, _keyring_ok
from Utils.ca_bundle import resolve_ca_bundle
from Utils.config_paths import get_config_dir

LOGIN_URL = "https://www.loverslab.com/login/"
_KEYRING_USER = "loverslab_session"
_SESSION_FILE = "loverslab_session.bin"
_CSRF_RE = re.compile(r'name="csrfKey"\s+value="([^"]+)"')
USER_AGENT = "MosaicModManager"


class LoversLabAuthError(Exception):
    """Logging in to LoversLab failed; the message is safe to show the user."""


class LoversLabMfaRequired(LoversLabAuthError):
    """The password was accepted and LoversLab now wants a two-factor code.
    Pass ``challenge`` and the code to :func:`complete_mfa`."""

    def __init__(self, challenge: "MfaChallenge"):
        super().__init__("Enter the code from your authenticator app.")
        self.challenge = challenge


class MfaChallenge:
    """A half-finished login: the session that passed the password step and
    the code form LoversLab answered with. Real shape (2026-10-07): a form
    posting back to ``/?&_mfaLogin=1`` with hidden ``mfa_auth`` and
    ``csrfKey`` and a ``google_authenticator_auth_code`` text field."""

    def __init__(self, session, action: str, hidden: "dict[str, str]", code_field: str):
        self.session = session
        self.action = action
        self.hidden = hidden
        self.code_field = code_field

    def close(self) -> None:
        try:
            if self.session is not None:
                self.session.close()
        except Exception:
            pass


def _find_code_form(page_html: str, page_url: str) -> "tuple[str, dict, str] | None":
    """``(action URL, hidden field values, code field name)`` of the form on
    the page that asks for a one-time code, or None."""
    from html.parser import HTMLParser
    from urllib.parse import urljoin

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms: "list[dict]" = []
            self._cur = None

        def handle_starttag(self, tag, attrs):
            a = {k: (v or "") for k, v in attrs}
            if tag == "form":
                self._cur = {"action": a.get("action", ""), "hidden": {}, "code": ""}
                self.forms.append(self._cur)
            elif tag == "input" and self._cur is not None and a.get("name"):
                name = a["name"]
                if a.get("type", "").lower() == "hidden":
                    self._cur["hidden"][name] = a.get("value", "")
                elif (name.endswith("_auth_code")
                      or a.get("autocomplete", "").lower() == "one-time-code"):
                    self._cur["code"] = name

        def handle_endtag(self, tag):
            if tag == "form":
                self._cur = None

    p = _P()
    try:
        p.feed(page_html or "")
    except Exception:
        return None
    form = next((f for f in p.forms if f["code"]), None)
    if form is None:
        return None
    return urljoin(page_url, html.unescape(form["action"])) or page_url, form["hidden"], form["code"]


def _page_messages(page_html: str) -> "list[str]":
    """Visible error/warning texts IPS4 shows on a page (its own wording,
    safe to log)."""
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.texts: "list[str]" = []

        def handle_starttag(self, tag, attrs):
            cls = dict(attrs).get("class") or ""
            if self.depth:
                self.depth += 1
            elif any(k in cls for k in ("ipsMessage_error", "ipsMessage_warning",
                                        "ipsType_warning", "ipsFieldRow_error")):
                self.depth = 1
                self.texts.append("")

        def handle_endtag(self, tag):
            if self.depth:
                self.depth -= 1

        def handle_data(self, data):
            if self.depth and data.strip():
                self.texts[-1] = (self.texts[-1] + " " + data.strip()).strip()

    p = _P()
    try:
        p.feed(page_html or "")
    except Exception:
        pass
    return [t[:200] for t in p.texts if t]


def _trace(step: str, resp, session) -> str:
    """One safe log line about a login response: URL without query, status,
    cookie NAMES (never values), IPS messages, whether a code form is there."""
    url = _strip_query(str(getattr(resp, "url", "") or ""))
    names = sorted(session.cookies.get_dict()) if session is not None else []
    code_form = _find_code_form(getattr(resp, "text", ""), url or LOGIN_URL) is not None
    msgs = _page_messages(getattr(resp, "text", ""))
    return (f"{step}: status {getattr(resp, 'status_code', '?')} at {url or '?'}; "
            f"cookies {names}; code form {'present' if code_form else 'absent'}"
            + (f"; page says {msgs}" if msgs else ""))


def _logged_in(session) -> bool:
    member_id = session.cookies.get("ips4_member_id") or ""
    return bool(member_id) and member_id != "0"


def describe_forms(page_html: str) -> str:
    """The structure of every form on a page -- action, method, each input's
    name and type, select options, and button labels -- with no field values
    (no tokens, no user data), so it's safe to log. Used to see what a login
    challenge page asks for."""
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.lines: "list[str]" = []
            self._button = False

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form":
                # Query strings can carry tokens (LoversLab puts csrfKey in some).
                self.lines.append(f"form action={_strip_query(a.get('action') or '')!r} "
                                  f"method={a.get('method', 'get')!r}")
            elif tag in ("input", "select", "textarea"):
                self.lines.append(f"  {tag} name={a.get('name')!r} type={a.get('type')!r}"
                                  + (f" autocomplete={a['autocomplete']!r}"
                                     if a.get("autocomplete") else ""))
            elif tag == "option":
                self.lines.append(f"    option value={a.get('value')!r}")
            elif tag == "button":
                self.lines.append(f"  button name={a.get('name')!r} type={a.get('type')!r}")
                self._button = True

        def handle_data(self, data):
            if self._button and data.strip():
                self.lines.append(f"    label {data.strip()[:60]!r}")

        def handle_endtag(self, tag):
            if tag == "button":
                self._button = False

    p = _P()
    try:
        p.feed(page_html or "")
    except Exception:
        pass
    return "\n".join(p.lines) or "(no forms)"


def _is_cloudflare_challenge(resp) -> bool:
    return resp.status_code in (403, 503) and (
        "cf-ray" in {k.lower() for k in resp.headers}
        or "Just a moment" in (resp.text or ""))


def login(username: str, password: str, log=None) -> "dict[str, str]":
    """Log in and return the session cookies. Raises
    :class:`LoversLabAuthError` with a user-presentable message on failure."""
    if not username or not password:
        raise LoversLabAuthError("Enter both your LoversLab username and password.")
    verify = resolve_ca_bundle() or True
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    try:
        page = session.get(LOGIN_URL, timeout=30, verify=verify)
        if _is_cloudflare_challenge(page):
            raise LoversLabAuthError(
                "LoversLab is showing a browser-verification challenge, so Mosaic "
                "can't log in automatically right now. Try again later.")
        page.raise_for_status()
        m = _CSRF_RE.search(page.text)
        if not m:
            raise LoversLabAuthError(
                "Couldn't read LoversLab's login form (the page layout may have changed).")
        resp = session.post(LOGIN_URL, data={
            "csrfKey": html.unescape(m.group(1)),
            "auth": username,
            "password": password,
            "remember_me": "1",
            "_processLogin": "usernamepassword",
        }, timeout=30, verify=verify)
        resp.raise_for_status()
    except requests.RequestException as exc:
        session.close()
        raise LoversLabAuthError(f"Couldn't reach LoversLab: {exc}") from exc
    except LoversLabAuthError:
        session.close()
        raise

    if log:
        log("LoversLab login: " + _trace("password step", resp, session))
    if _logged_in(session):
        session.close()
        return dict(session.cookies.get_dict())
    code_form = _find_code_form(resp.text, str(getattr(resp, "url", "") or LOGIN_URL))
    if code_form is not None:
        # The session stays open: complete_mfa() finishes this login.
        raise LoversLabMfaRequired(MfaChallenge(session, *code_form))
    session.close()
    if "mfa" in resp.text.lower():
        err = LoversLabAuthError(
            "LoversLab asked for a kind of two-factor check Mosaic doesn't "
            "recognise.")
        err.diagnostic = f"final URL {_strip_query(str(getattr(resp, 'url', '')))!r}\n" \
                         f"{describe_forms(resp.text)}"
        raise err
    raise LoversLabAuthError("LoversLab rejected that username or password.")


def complete_mfa(challenge: MfaChallenge, code: str, log=None) -> "dict[str, str]":
    """Finish a login with the two-factor ``code`` and return the session
    cookies. A rejected code raises :class:`LoversLabAuthError` and leaves
    ``challenge`` ready for another try; call ``challenge.close()`` to give up."""
    code = "".join((code or "").split())
    if challenge.session is None:
        raise LoversLabAuthError("This login has expired. Log in again.")
    if not code:
        raise LoversLabAuthError("Enter the code from your authenticator app.")
    try:
        resp = challenge.session.post(
            challenge.action, data={**challenge.hidden, challenge.code_field: code},
            timeout=30, verify=resolve_ca_bundle() or True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LoversLabAuthError(f"Couldn't reach LoversLab: {exc}") from exc
    if log:
        log("LoversLab login: " + _trace("code step", resp, challenge.session))
    if _logged_in(challenge.session):
        cookies = dict(challenge.session.cookies.get_dict())
        challenge.close()
        return cookies
    again = _find_code_form(resp.text, str(getattr(resp, "url", "") or challenge.action))
    if again is not None:
        challenge.action, challenge.hidden, challenge.code_field = again
        raise LoversLabAuthError("LoversLab didn't accept that code. Check it and try again.")
    challenge.close()
    challenge.session = None   # this login is over; start again with the password
    raise LoversLabAuthError("LoversLab didn't accept that code. Log in again.")


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]


def _session_file():
    return get_config_dir() / _SESSION_FILE


def save_session(cookies: "dict[str, str]") -> None:
    payload = json.dumps(cookies)
    if _keyring_ok():
        try:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, payload)
            return
        except keyring.errors.KeyringError:
            pass
    from cryptography.fernet import Fernet
    p = _session_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(Fernet(_derive_key()).encrypt(payload.encode()))
    os.chmod(p, 0o600)


def load_session() -> "dict[str, str]":
    """The stored session cookies, or ``{}`` if not logged in."""
    payload = ""
    if _keyring_ok():
        try:
            payload = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER) or ""
        except keyring.errors.KeyringError:
            payload = ""
    if not payload:
        p = _session_file()
        if p.is_file():
            try:
                from cryptography.fernet import Fernet
                payload = Fernet(_derive_key()).decrypt(p.read_bytes()).decode()
            except Exception:
                payload = ""
    try:
        data = json.loads(payload) if payload else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def clear_session() -> None:
    if _keyring_ok():
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except keyring.errors.KeyringError:
            pass
    try:
        _session_file().unlink(missing_ok=True)
    except OSError:
        pass

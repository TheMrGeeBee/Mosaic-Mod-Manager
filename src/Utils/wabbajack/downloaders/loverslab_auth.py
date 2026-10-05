"""LoversLab login and session storage for the Wabbajack LoversLab downloader.

LoversLab runs Invision Community (IPS4): logging in is a GET of the login
page for its ``csrfKey`` hidden field, then a form POST of that key plus the
username/password. A successful login sets an ``ips4_member_id`` cookie to
the (non-zero) member id; that cookie set is the whole session.

Only the resulting session cookies are stored -- never the password. They go
in the system keyring under the same service name as the Nexus API key, with
the same machine-bound encrypted-file fallback (``Nexus.nexus_api``'s
``_derive_key``) when no keyring is available.

The form field names and the success cookie follow IPS4's standard login
flow; they haven't been exercised against the live site from this sandbox.
Two known ways the login can fail that aren't wrong-password are reported
distinctly: an account with two-factor auth enabled (IPS4 answers with an
MFA challenge page), and a Cloudflare bot challenge in front of the site.
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


def _is_cloudflare_challenge(resp) -> bool:
    return resp.status_code in (403, 503) and (
        "cf-ray" in {k.lower() for k in resp.headers}
        or "Just a moment" in (resp.text or ""))


def login(username: str, password: str) -> "dict[str, str]":
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
        raise LoversLabAuthError(f"Couldn't reach LoversLab: {exc}") from exc
    finally:
        session.close()

    member_id = session.cookies.get("ips4_member_id") or ""
    if member_id and member_id != "0":
        return dict(session.cookies.get_dict())
    if "mfa" in resp.text.lower():
        raise LoversLabAuthError(
            "This LoversLab account uses two-factor authentication, which Mosaic "
            "can't complete yet.")
    raise LoversLabAuthError("LoversLab rejected that username or password.")


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

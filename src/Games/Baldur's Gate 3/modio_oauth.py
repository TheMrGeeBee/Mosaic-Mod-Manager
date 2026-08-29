"""
modio_oauth.py  (Baldur's Gate 3)

mod.io login via the Email Request/Exchange flow — the desktop-appropriate
auth path per mod.io's own docs (no browser redirect or localhost callback
server needed, unlike Nexus's PKCE flow in Nexus.nexus_oauth). Required for
anything the app's read-only API key can't do — currently just "Like"
(mod.io's personal rating state needs a user access token).

Flow: request_email_code(email, api_key) sends a short numeric code to the
user's inbox; exchange_code(code, api_key) trades it for an access token
(mod.io documents ~1 year validity for this flow) which is then stored via
the same keyring/Fernet-file-fallback convention as modio_key.py's API key.

Domain/path confirmed by live-testing against the real mod.io API
(2026-08-29, with a disposable test key+account): mod.io retired
api.mod.io in favour of per-game g-{game_id}.modapi.io subdomains
(enforced since 2025-01-01 — api.mod.io now returns 401 error_ref 11001,
"deprecated api.mod.io domain"), AND the endpoint path itself is
/oauth/emailrequest + /oauth/emailexchange, not /authenticate/emailrequest
+ /authenticate/emailexchange as earlier (unverified) docs summaries
suggested — that path 404s even on the correct domain. Both request paths
and the emailexchange error shape for a bad code were confirmed live;
what's NOT independently confirmed is the exact success-response field
names below (access_token / refresh_token) — no real code was available to
complete a full exchange. If exchange_code() raises "did not return an
access token" on an otherwise-valid code, check the real response shape at
https://docs.mod.io/restapiref/#tag/Authentication first. No refresh-token
flow is implemented yet since the access token is meant to be long-lived;
add one (and confirm mod.io's actual refresh endpoint) only if tokens
start expiring in practice.
"""

from __future__ import annotations

from typing import Optional

import keyring  # type: ignore
import requests

from Utils.app_log import app_log
from Utils.ca_bundle import resolve_ca_bundle
from Utils.config_paths import get_config_dir

# Same per-game subdomain as modio_api.py's _API_ROOT — mirrored here rather
# than imported since this module is loaded standalone by file path (see
# app.py's _load_bg3_modio) and importing a sibling the same way is more
# fragile than one repeated constant.
_GAME_ID = 6715
_API_ROOT = f"https://g-{_GAME_ID}.modapi.io/v1"

# Same keyring service as modio_key.py / Nexus's tokens — one logical app
# entry, only the user/account differs.
_KEYRING_SERVICE = "MosaicModManager"
_KEYRING_ACCESS_KEY = "modio_oauth_access_token"
_KEYRING_REFRESH_KEY = "modio_oauth_refresh_token"
_TOKENS_FILE = "modio_oauth_tokens.bin"


class ModioOAuthError(Exception):
    """Raised on a failed mod.io email-login request."""


def _keyring_ok() -> bool:
    """Reuse the Nexus keyring probe (mirrors modio_key.py)."""
    try:
        from Nexus.nexus_oauth import _is_keyring_available
        return _is_keyring_available()
    except Exception:
        return True  # assume available if we can't check


def _derive_key() -> bytes:
    """Machine-bound Fernet key — identical derivation to modio_key.py's
    _derive_key (same salt, same rationale: opaque KDF input, changing it
    would silently break decryption of any existing fallback file)."""
    import base64
    import hashlib
    machine_id = ""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p) as f:
                machine_id = f.read().strip()
            if machine_id:
                break
        except OSError:
            continue
    if not machine_id:
        machine_id = "fallback-no-machine-id"
    dk = hashlib.pbkdf2_hmac("sha256", machine_id.encode(),
                             b"AmethystModManager", 100_000)
    return base64.urlsafe_b64encode(dk)


def _tokens_file_path():
    return get_config_dir() / _TOKENS_FILE


def _load_file_tokens() -> dict:
    p = _tokens_file_path()
    try:
        if not p.is_file():
            return {}
        from cryptography.fernet import Fernet
        import json as _json
        cipher = Fernet(_derive_key())
        return _json.loads(cipher.decrypt(p.read_bytes()))
    except Exception:
        return {}


def _save_file_tokens(access_token: str, refresh_token: str) -> None:
    from cryptography.fernet import Fernet
    import json as _json
    import os as _os
    p = _tokens_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cipher = Fernet(_derive_key())
    data = {"access_token": access_token, "refresh_token": refresh_token}
    p.write_bytes(cipher.encrypt(_json.dumps(data).encode()))
    _os.chmod(p, 0o600)


def _clear_file_tokens() -> None:
    p = _tokens_file_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def load_modio_tokens() -> Optional[dict]:
    """Return {"access_token": ..., "refresh_token": ...}, or None if the
    user hasn't logged in via email."""
    if not _keyring_ok():
        data = _load_file_tokens()
        return data or None
    try:
        access = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCESS_KEY)
        if not access:
            return None
        refresh = keyring.get_password(_KEYRING_SERVICE, _KEYRING_REFRESH_KEY) or ""
        return {"access_token": access, "refresh_token": refresh}
    except keyring.errors.KeyringError as e:
        app_log(f"Keyring unavailable for mod.io OAuth tokens: {e} — using file fallback")
        data = _load_file_tokens()
        return data or None


def save_modio_tokens(access_token: str, refresh_token: str = "") -> None:
    if not _keyring_ok():
        _save_file_tokens(access_token, refresh_token)
        return
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCESS_KEY, access_token)
        if refresh_token:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_REFRESH_KEY, refresh_token)
    except keyring.errors.KeyringError as e:
        app_log(f"Keyring unavailable for saving mod.io OAuth tokens: {e} — using file fallback")
        _save_file_tokens(access_token, refresh_token)


def clear_modio_tokens() -> None:
    """Log out: delete any stored mod.io access/refresh token."""
    _clear_file_tokens()
    if _keyring_ok():
        for key in (_KEYRING_ACCESS_KEY, _KEYRING_REFRESH_KEY):
            try:
                keyring.delete_password(_KEYRING_SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass
            except keyring.errors.KeyringError as e:
                app_log(f"Keyring unavailable when clearing mod.io OAuth tokens: {e}")


def _session() -> requests.Session:
    s = requests.Session()
    s.verify = resolve_ca_bundle() or True
    s.headers.update({"Accept": "application/json", "User-Agent": "MosaicModManager"})
    return s


def _error_message(resp: requests.Response) -> str:
    try:
        msg = (resp.json().get("error") or {}).get("message")
        if msg:
            return str(msg)
    except ValueError:
        pass
    return f"HTTP {resp.status_code}"


def request_email_code(email: str, api_key: str, timeout: float = 30.0) -> None:
    """Ask mod.io to send a security code to *email*. Raises ModioOAuthError
    on failure (invalid email, rate-limited, network error, etc)."""
    email = (email or "").strip()
    if not email:
        raise ModioOAuthError("Email address is required")
    if not api_key:
        raise ModioOAuthError("mod.io API key is required")
    try:
        resp = _session().post(
            f"{_API_ROOT}/oauth/emailrequest",
            data={"api_key": api_key, "email": email},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise ModioOAuthError(str(e)) from e
    if not resp.ok:
        raise ModioOAuthError(_error_message(resp))


def exchange_code(security_code: str, api_key: str, timeout: float = 30.0) -> str:
    """Exchange a security code for an access token, persist it, and return
    it. Raises ModioOAuthError on failure (wrong/expired code, network
    error, or a response mod.io didn't actually include a token in)."""
    code = (security_code or "").strip()
    if not code:
        raise ModioOAuthError("Security code is required")
    if not api_key:
        raise ModioOAuthError("mod.io API key is required")
    try:
        resp = _session().post(
            f"{_API_ROOT}/oauth/emailexchange",
            data={"api_key": api_key, "security_code": code},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise ModioOAuthError(str(e)) from e
    if not resp.ok:
        raise ModioOAuthError(_error_message(resp))
    body = resp.json()
    access_token = str(body.get("access_token") or "")
    if not access_token:
        raise ModioOAuthError("mod.io did not return an access token")
    refresh_token = str(body.get("refresh_token") or "")
    save_modio_tokens(access_token, refresh_token)
    return access_token

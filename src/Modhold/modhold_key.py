"""
modhold_key.py
Secure storage for the user's Modhold key (``mhm_...``) and display name.

Same scheme as the Nexus and mod.io keys: the system keyring under the shared
``MosaicModManager`` service, with a machine-bound Fernet-encrypted file as the
fallback when no keyring backend is available. Mosaic never sees a Modhold
password: the key arrives through Modhold's Connect flow (modhold_connect.py).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import keyring  # type: ignore

from Utils.app_log import app_log
from Utils.config_paths import get_config_dir

_KEYRING_SERVICE = "MosaicModManager"
_KEYRING_KEY_USER = "modhold_key"
_KEYRING_NAME_USER = "modhold_user"
_FILE_NAME = "modhold_key.bin"


def _file_path() -> Path:
    return get_config_dir() / _FILE_NAME


def _keyring_ok() -> bool:
    try:
        from Nexus.nexus_oauth import _is_keyring_available
        return _is_keyring_available()
    except Exception:
        return True


def _derive_key() -> bytes:
    """Fernet key bound to this machine's id, matching the Nexus/mod.io files."""
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


def _load_file() -> dict:
    p = _file_path()
    try:
        if not p.is_file():
            return {}
        from cryptography.fernet import Fernet
        data = json.loads(Fernet(_derive_key()).decrypt(p.read_bytes()))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_file(key: str, user: str) -> None:
    from cryptography.fernet import Fernet
    p = _file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"api_key": key, "user": user}).encode()
    p.write_bytes(Fernet(_derive_key()).encrypt(blob))
    os.chmod(p, 0o600)


def _clear_file() -> None:
    try:
        p = _file_path()
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def load_modhold_key() -> str:
    """Return the saved Modhold key, or "" when none is stored."""
    if not _keyring_ok():
        return str(_load_file().get("api_key", "")).strip()
    try:
        key = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY_USER)
        return (key or "").strip()
    except keyring.errors.KeyringError as e:
        app_log(f"Keyring unavailable for the Modhold key: {e} — using file fallback")
        return str(_load_file().get("api_key", "")).strip()


def load_modhold_user() -> str:
    """Return the Modhold display name saved with the key, or ""."""
    if not _keyring_ok():
        return str(_load_file().get("user", "")).strip()
    try:
        return (keyring.get_password(_KEYRING_SERVICE, _KEYRING_NAME_USER) or "").strip()
    except keyring.errors.KeyringError:
        return str(_load_file().get("user", "")).strip()


def save_modhold_key(key: str, user: str = "") -> None:
    """Persist the key (and the display name) to the keyring or the file fallback."""
    key, user = key.strip(), user.strip()
    if not _keyring_ok():
        _save_file(key, user)
        return
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY_USER, key)
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_NAME_USER, user)
    except keyring.errors.KeyringError as e:
        app_log(f"Keyring unavailable for saving the Modhold key: {e} — using file fallback")
        _save_file(key, user)


def clear_modhold_key() -> None:
    """Delete the stored key and name from the keyring and the file."""
    _clear_file()
    if not _keyring_ok():
        return
    for account in (_KEYRING_KEY_USER, _KEYRING_NAME_USER):
        try:
            keyring.delete_password(_KEYRING_SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError as e:
            app_log(f"Keyring unavailable when clearing the Modhold key: {e}")

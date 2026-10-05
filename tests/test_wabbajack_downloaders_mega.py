"""Tests for Utils.wabbajack.downloaders.mega_source -- link parsing, key
derivation, the API retry/error handling, and a full encrypt -> download ->
decrypt round trip. No real network access.

The round trip encrypts with a counter built independently of the module
under test (``Counter.new(128, initial_value=...)``, the construction Mega's
own reference clients use), so a mismatch in how the module derives its key
or CTR initial value would fail here rather than silently agreeing with
itself.
"""
from __future__ import annotations

import base64
import os
import struct
import threading

import pytest
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.downloaders import mega_source as mega


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _reference_encrypt(raw_key: bytes, plaintext: bytes) -> bytes:
    a = struct.unpack(">8I", raw_key)
    k = struct.pack(">4I", a[0] ^ a[4], a[1] ^ a[5], a[2] ^ a[6], a[3] ^ a[7])
    ctr = Counter.new(128, initial_value=((a[4] << 32) + a[5]) << 64)
    return AES.new(k, AES.MODE_CTR, counter=ctr).encrypt(plaintext)


class _FakeResponse:
    def __init__(self, *, body=b"", json_data=None, status=200):
        self._body = body
        self._json = json_data
        self.status_code = status
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mega.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Link parsing / key derivation
# ---------------------------------------------------------------------------

def test_parse_link_new_format():
    raw = os.urandom(32)
    handle, key = mega.parse_link(f"https://mega.nz/file/AbCd1234#{_b64(raw)}")
    assert handle == "AbCd1234"
    assert key == raw


def test_parse_link_legacy_format():
    raw = os.urandom(32)
    handle, key = mega.parse_link(f"https://mega.co.nz/#!AbCd1234!{_b64(raw)}")
    assert handle == "AbCd1234"
    assert key == raw


def test_parse_link_rejects_folder_sized_key():
    with pytest.raises(mega.MegaError, match="expected 32"):
        mega.parse_link(f"https://mega.nz/file/AbCd1234#{_b64(os.urandom(16))}")


def test_parse_link_rejects_non_mega_url():
    with pytest.raises(mega.MegaError, match="not a Mega file link"):
        mega.parse_link("https://example.com/file.7z")


def test_derive_key_and_iv_layout():
    raw = struct.pack(">8I", 1, 2, 3, 4, 5, 6, 7, 8)
    key, iv = mega.derive_key_and_iv(raw)
    assert key == struct.pack(">4I", 1 ^ 5, 2 ^ 6, 3 ^ 7, 4 ^ 8)
    assert iv == struct.pack(">4I", 5, 6, 0, 0)


# ---------------------------------------------------------------------------
# Full download round trip
# ---------------------------------------------------------------------------

def _install_fake_mega(monkeypatch, *, api_responses, ciphertext=b""):
    api_calls = []

    def fake_post(url, params=None, json=None, **kw):
        api_calls.append(json)
        return _FakeResponse(json_data=api_responses.pop(0))

    monkeypatch.setattr(mega.requests, "post", fake_post)
    monkeypatch.setattr(mega.requests, "get", lambda url, **kw: _FakeResponse(body=ciphertext))
    monkeypatch.setattr(mega.time, "sleep", lambda _s: None)
    return api_calls


def test_download_decrypts_round_trip_with_odd_chunks(tmp_path, monkeypatch):
    raw = os.urandom(32)
    plaintext = os.urandom(10_000) + b"tail that is not block aligned"
    ciphertext = _reference_encrypt(raw, plaintext)
    api_calls = _install_fake_mega(
        monkeypatch, api_responses=[[{"g": "https://gfs.mega.co.nz/dl/x", "s": len(plaintext)}]],
        ciphertext=ciphertext)
    # A chunk size that isn't a multiple of the 16-byte AES block exercises
    # keystream continuity across partial blocks.
    monkeypatch.setattr(mega, "_CHUNK_SIZE", 777)

    dest = tmp_path / "a.7z"
    result = mega.download_mega(
        wm.MegaState(url=f"https://mega.nz/file/AbCd1234#{_b64(raw)}"), dest)

    assert result.success, result.error
    assert dest.read_bytes() == plaintext
    assert api_calls == [[{"a": "g", "g": 1, "p": "AbCd1234"}]]


def test_download_retries_eagain_then_succeeds(tmp_path, monkeypatch):
    raw = os.urandom(32)
    plaintext = b"payload"
    api_calls = _install_fake_mega(
        monkeypatch,
        api_responses=[-3, [-3], [{"g": "https://gfs.mega.co.nz/dl/x", "s": len(plaintext)}]],
        ciphertext=_reference_encrypt(raw, plaintext))

    dest = tmp_path / "a.7z"
    result = mega.download_mega(
        wm.MegaState(url=f"https://mega.nz/file/AbCd1234#{_b64(raw)}"), dest)

    assert result.success, result.error
    assert dest.read_bytes() == plaintext
    assert len(api_calls) == 3


def test_download_reports_named_api_error(tmp_path, monkeypatch):
    raw = os.urandom(32)
    _install_fake_mega(monkeypatch, api_responses=[[-9]])
    result = mega.download_mega(
        wm.MegaState(url=f"https://mega.nz/file/AbCd1234#{_b64(raw)}"), tmp_path / "a.7z")
    assert not result.success
    assert "file not found" in result.error


def test_download_bad_link_short_circuits_without_network(monkeypatch):
    monkeypatch.setattr(mega.requests, "post", lambda *a, **k: pytest.fail("no API call expected"))
    result = mega.download_mega(wm.MegaState(url="https://example.com/x"), "ignored")
    assert not result.success


def test_download_no_url_short_circuits():
    result = mega.download_mega(wm.MegaState(url=""), "ignored")
    assert not result.success


def test_download_cancel_aborts_and_cleans_up(tmp_path, monkeypatch):
    raw = os.urandom(32)
    _install_fake_mega(
        monkeypatch, api_responses=[[{"g": "https://gfs.mega.co.nz/dl/x", "s": 10}]],
        ciphertext=b"x" * 10)
    cancel = threading.Event()
    cancel.set()
    dest = tmp_path / "a.7z"
    result = mega.download_mega(
        wm.MegaState(url=f"https://mega.nz/file/AbCd1234#{_b64(raw)}"), dest, cancel=cancel)
    assert not result.success
    assert result.error == "cancelled"
    assert not dest.exists()

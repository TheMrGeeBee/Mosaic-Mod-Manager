"""Mega.nz public-link archive downloads.

Covers ``Archive.State``'s ``MegaDownloader`` ``$type``. Hand-rolled rather
than built on the ``mega.py`` package, whose declared license (CC BY-NC-SA)
is incompatible with Mosaic's GPL-3.

Only anonymous public *file* links are handled -- the only kind a Wabbajack
modlist references. The protocol:

1. The link carries a file handle and a 256-bit key, URL-safe base64:
   ``https://mega.nz/file/<handle>#<key>`` (or the legacy
   ``https://mega.nz/#!<handle>!<key>``).
2. The key decodes to eight big-endian 32-bit words ``a[0..7]``. The AES-128
   file key is ``a[i] ^ a[i+4]`` for ``i`` in 0..3; the CTR nonce is
   ``a[4], a[5]`` with a 64-bit block counter starting at 0. (``a[6], a[7]``
   are a MAC, not checked here: the orchestrator verifies the whole
   decrypted file's xxHash64 against ``Archive.hash``, which already catches
   a bad key or corrupted transfer.)
3. A ``g`` API call for the handle returns a temporary download URL (``g``)
   and the size (``s``).
4. The bytes at that URL are the AES-CTR ciphertext; they're decrypted while
   streaming to disk.

Mega answers with negative integer error codes rather than HTTP errors;
``-3`` (EAGAIN) means "try again shortly" and is retried with backoff.
"""
from __future__ import annotations

import base64
import re
import struct
import threading
import time
from pathlib import Path

import requests
from Cryptodome.Cipher import AES

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth_limit

from ..wabbajack_manifest import MegaState
from .http_source import DownloadCancelled, WabbajackDownloadResult

_API_URL = "https://g.api.mega.co.nz/cs"
_CHUNK_SIZE = 256 * 1024
_EAGAIN = -3
_RETRY_DELAYS = (2.0, 5.0, 10.0)
_ERROR_NAMES = {
    -2: "invalid request",
    -9: "file not found (removed or link invalid)",
    -11: "access denied",
    -16: "file blocked by Mega (takedown or ToS)",
    -17: "transfer quota exceeded",
    -18: "temporarily unavailable",
}

_LINK_RES = (
    re.compile(r"mega(?:\.co)?\.nz/file/([0-9A-Za-z_-]+)#([0-9A-Za-z_-]+)"),
    re.compile(r"mega(?:\.co)?\.nz/#!([0-9A-Za-z_-]+)!([0-9A-Za-z_-]+)"),
)


class MegaError(Exception):
    """A Mega link couldn't be parsed or the Mega API refused the request."""


def parse_link(url: str) -> "tuple[str, bytes]":
    """``(handle, raw 32-byte key)`` from a public Mega file link."""
    for pattern in _LINK_RES:
        m = pattern.search(url or "")
        if m:
            handle, key_b64 = m.group(1), m.group(2)
            try:
                raw = base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4))
            except (ValueError, TypeError) as exc:
                raise MegaError(f"malformed Mega key in {url!r}") from exc
            if len(raw) != 32:
                raise MegaError(
                    f"Mega key in {url!r} is {len(raw)} bytes, expected 32 "
                    "(folder links aren't supported, only file links)")
            return handle, raw
    raise MegaError(f"not a Mega file link: {url!r}")


def derive_key_and_iv(raw_key: bytes) -> "tuple[bytes, bytes]":
    """``(aes_key, ctr_initial_value)`` from a raw 32-byte Mega file key."""
    a = struct.unpack(">8I", raw_key)
    aes_key = struct.pack(">4I", a[0] ^ a[4], a[1] ^ a[5], a[2] ^ a[6], a[3] ^ a[7])
    iv = struct.pack(">4I", a[4], a[5], 0, 0)
    return aes_key, iv


def _api_get_download(handle: str, verify) -> dict:
    """Call Mega's ``g`` API for a public file handle, retrying EAGAIN."""
    for attempt in range(len(_RETRY_DELAYS) + 1):
        resp = requests.post(
            _API_URL, params={"id": int(time.time() * 1000) % 1_000_000},
            json=[{"a": "g", "g": 1, "p": handle}], timeout=30, verify=verify)
        resp.raise_for_status()
        body = resp.json()
        # The whole response can itself be a bare error code.
        result = body if isinstance(body, int) else (body[0] if body else -2)
        if result == _EAGAIN and attempt < len(_RETRY_DELAYS):
            time.sleep(_RETRY_DELAYS[attempt])
            continue
        if isinstance(result, int):
            raise MegaError(f"Mega API error {result}: "
                            f"{_ERROR_NAMES.get(result, 'unknown error')}")
        if not isinstance(result, dict) or not result.get("g"):
            raise MegaError("Mega API returned no download URL")
        return result
    raise MegaError("Mega API kept asking to retry (EAGAIN)")


def download_mega(state: MegaState, dest: Path, *, progress_cb=None,
                   cancel: "threading.Event | None" = None) -> WabbajackDownloadResult:
    """Download and decrypt a public Mega file (``Archive.State`` ``$type``
    ``MegaDownloader``)."""
    if not state.url:
        return WabbajackDownloadResult(success=False, error="no URL in MegaDownloader state")
    try:
        handle, raw_key = parse_link(state.url)
    except MegaError as exc:
        return WabbajackDownloadResult(success=False, error=str(exc))

    aes_key, iv = derive_key_and_iv(raw_key)
    verify = resolve_ca_bundle() or True
    try:
        info = _api_get_download(handle, verify)
    except (MegaError, requests.RequestException, ValueError) as exc:
        return WabbajackDownloadResult(success=False, error=str(exc))

    cipher = AES.new(aes_key, AES.MODE_CTR, nonce=b"", initial_value=iv)
    total = int(info.get("s") or 0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    try:
        with requests.get(info["g"], stream=True, timeout=60, verify=verify) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(_CHUNK_SIZE):
                    if cancel and cancel.is_set():
                        raise DownloadCancelled()
                    fh.write(cipher.decrypt(chunk))
                    downloaded += len(chunk)
                    bandwidth_limit.throttle(len(chunk), cancel)
                    if progress_cb:
                        progress_cb(downloaded, total)
    except DownloadCancelled:
        dest.unlink(missing_ok=True)
        return WabbajackDownloadResult(success=False, error="cancelled")
    except (requests.RequestException, OSError) as exc:
        dest.unlink(missing_ok=True)
        return WabbajackDownloadResult(success=False, error=str(exc))

    return WabbajackDownloadResult(success=True, file_path=dest, bytes_downloaded=downloaded)

"""
modhold_api.py
Client for the Modhold REST API (https://modholdmods.com/developers).

Public reads (search, a mod and its files, vote totals, loadouts) need no key.
Downloading a file needs the player's own key, sent as a bearer token, and
returns a link that is good for an hour, so a link is resolved right before
each download and never kept. Modhold allows 600 requests a minute; a small
token bucket keeps Mosaic well under that.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests

from version import __version__

BASE_URL = "https://modholdmods.com"
API_BASE = f"{BASE_URL}/api/v1"

# 600 requests a minute is 10 a second; stay under it.
_RATE_PER_SEC = 8.0
_BURST = 10
_MAX_RETRIES = 3
_TIMEOUT = 30
_LIST_CACHE_SECONDS = 60


class ModholdError(Exception):
    """Any Modhold API failure."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class ModholdAuthError(ModholdError):
    """No key was sent, or the key was removed (HTTP 401)."""


class ModholdNotEntitled(ModholdError):
    """The key's owner cannot get this file: paid, supporters-first or a supporter
    beta (HTTP 402). Modhold's own message says which."""


class ModholdNotReady(ModholdError):
    """The file is still waiting on Modhold's virus scan (HTTP 409); try again later."""


class ModholdGone(ModholdError):
    """The file is an older version the author has replaced (HTTP 410)."""


class ModholdNotFound(ModholdError):
    """The mod, file or loadout does not exist (or is not public)."""


class ModholdRateLimited(ModholdError):
    """Still rate limited after the retries."""


@dataclass
class DownloadLink:
    """What ``/files/{id}/download`` returns: a short-lived URL plus its checks."""
    url: str
    size: int
    sha256: str
    name: str = ""       # the archive's file name
    expires: str = ""    # when the link stops working (about an hour)


class _TokenBucket:
    def __init__(self, rate: float, burst: int,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self._burst,
                                   self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            self._sleep(wait)


class ModholdAPI:
    """Thin, injectable client. Pass ``session``/``clock``/``sleep`` in tests."""

    def __init__(self, api_key: str = "", *,
                 session: Optional[requests.Session] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._key = (api_key or "").strip()
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._bucket = _TokenBucket(_RATE_PER_SEC, _BURST, clock, sleep)
        self._cache: dict[tuple, tuple[float, Any]] = {}

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    # ------------------------------------------------------------------ core

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": f"MosaicModManager/{__version__}",
             "Accept": "application/json"}
        if self._key:
            h["Authorization"] = f"Bearer {self._key}"
        return h

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{API_BASE}{path}"
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        for attempt in range(_MAX_RETRIES + 1):
            self._bucket.take()
            try:
                resp = self._session.get(url, params=clean, headers=self._headers(),
                                         timeout=_TIMEOUT)
            except requests.RequestException as e:
                if attempt >= _MAX_RETRIES:
                    raise ModholdError(f"Could not reach Modhold: {e}") from e
                self._sleep(1.0 + attempt)
                continue
            status = resp.status_code
            if status == 429 or status >= 500:
                if attempt >= _MAX_RETRIES:
                    if status == 429:
                        raise ModholdRateLimited("Modhold rate limit hit.", status)
                    raise ModholdError(f"Modhold server error ({status}).", status)
                self._sleep(self._retry_after(resp, attempt))
                continue
            if status in (401, 403):
                raise ModholdAuthError(self._message(resp, "Not allowed."), status)
            if status == 402:
                raise ModholdNotEntitled(
                    self._message(resp, "This account cannot get this file."), status)
            if status == 409:
                raise ModholdNotReady(
                    self._message(resp, "The file is still being scanned."), status)
            if status == 410:
                raise ModholdGone(
                    self._message(resp, "This version has been replaced."), status)
            if status == 404:
                raise ModholdNotFound(self._message(resp, "Not found."), status)
            if status >= 400:
                raise ModholdError(self._message(resp, f"HTTP {status}."), status)
            try:
                return resp.json()
            except ValueError as e:
                raise ModholdError("Modhold sent a reply that is not JSON.", status) from e
        raise ModholdError("Modhold request failed.")  # pragma: no cover

    @staticmethod
    def _retry_after(resp, attempt: int) -> float:
        raw = (getattr(resp, "headers", None) or {}).get("Retry-After", "")
        try:
            return max(0.0, min(float(raw), 60.0))
        except (TypeError, ValueError):
            return float(2 ** attempt)

    @staticmethod
    def _message(resp, default: str) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict):
                for k in ("error", "message", "reason"):
                    if isinstance(body.get(k), str) and body[k]:
                        return body[k]
        except ValueError:
            pass
        return default

    def _cached_get(self, path: str, params: dict) -> Any:
        key = (path, tuple(sorted((k, str(v)) for k, v in params.items())),
               bool(self._key))
        hit = self._cache.get(key)
        now = self._clock()
        if hit and now - hit[0] < _LIST_CACHE_SECONDS:
            return hit[1]
        data = self._get(path, params)
        self._cache[key] = (now, data)
        return data

    # ------------------------------------------------------------- endpoints

    def list_mods(self, game: str, *, sort: str = "updated", q: str = "",
                  category: str = "", tag: str = "", page: int = 1) -> dict:
        """Search mods: ``{total, page, per_page, mods: [...]}``, 50 a page."""
        return self._cached_get("/mods", {"game": game, "sort": sort, "q": q,
                                          "category": category, "tag": tag,
                                          "page": page})

    def get_mod(self, mod_id: int) -> dict:
        """One mod with its files, requirements and changelog."""
        return self._get(f"/mods/{int(mod_id)}")

    def stats(self, mod_ids: list[int]) -> dict:
        """Vote and download totals for many mods in one call."""
        ids = ",".join(str(int(i)) for i in mod_ids)
        return self._get("/stats", {"ids": ids})

    def list_loadouts(self, game: str, q: str = "") -> list[dict]:
        """Public loadouts for a game, most installed first (up to 100)."""
        data = self._cached_get("/loadouts", {"game": game, "q": q})
        return list(data.get("loadouts", [])) if isinstance(data, dict) else []

    def get_loadout(self, loadout_id: int) -> dict:
        """One whole loadout in the ``modhold-loadout`` format."""
        return self._get(f"/loadouts/{int(loadout_id)}")

    def file_download(self, file_id: int) -> DownloadLink:
        """Resolve a file's one-hour download link. Needs the player's key."""
        if not self._key:
            raise ModholdAuthError("Connect your Modhold account to download.", 401)
        data = self._get(f"/files/{int(file_id)}/download")
        url = str(data.get("url") or "")
        if not url:
            raise ModholdError("Modhold did not return a download link.")
        try:
            size = int(data.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return DownloadLink(url=url, size=size,
                            sha256=str(data.get("sha256") or "").lower(),
                            name=str(data.get("name") or ""),
                            expires=str(data.get("expires") or ""))

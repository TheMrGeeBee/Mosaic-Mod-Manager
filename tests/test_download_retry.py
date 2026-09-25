"""A network blip must not open a browser tab per mod of a 2,000-mod collection
(each waiting up to 15 minutes): transient download failures are retried after a
delay; only a real refusal falls back to the browser."""
from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from Nexus.manual_download_watch import is_transient_failure, should_fallback_to_browser
from Utils.collections.collection_install import download_with_retry


@dataclass
class R:
    success: bool = False
    error: str = ""
    status_code: int = 0


OK = R(True)
CONN = R(False, "Connection failed: HTTPSConnectionPool(host='api.nexusmods.com'): Max retries", 0)
TIMEOUT = R(False, "Request timed out after 30s", 0)
NO_LINKS = R(False, "API returned no download links", 0)
ARCHIVED = R(False, "This file is not available", 403)


@pytest.mark.parametrize("result,transient,browser", [
    (OK, False, False),
    (CONN, True, False),
    (TIMEOUT, True, False),
    (R(False, "server error", 503), True, False),
    (R(False, "server error", 500), True, False),
    (R(False, "request timeout", 408), True, False),
    (NO_LINKS, False, True),                 # the API refusing an archived file: browser still helps
    (ARCHIVED, False, True),
    (R(False, "not found", 404), False, True),
    (R(False, "Invalid or expired API key", 401), False, False),
    (R(False, "rate limited", 429), False, False),
    (R(False, "All mirrors failed. Last error: x", 0), False, True),
])
def test_classification(result, transient, browser):
    assert is_transient_failure(result) is transient
    assert should_fallback_to_browser(result) is browser


def _seq(*results):
    it = iter(results)
    calls = []

    def call():
        calls.append(1)
        return next(it)

    return call, calls


def test_retries_a_transient_failure_until_it_succeeds():
    call, calls = _seq(CONN, TIMEOUT, OK)
    logs = []
    assert download_with_retry(call, threading.Event(), logs.append, "Mod", delays=(0, 0, 0)) is OK
    assert len(calls) == 3 and len(logs) == 2 and "retry 1/3" in logs[0]


def test_gives_up_after_the_last_delay_and_returns_the_failure():
    call, calls = _seq(CONN, CONN, CONN, CONN)
    result = download_with_retry(call, threading.Event(), delays=(0, 0, 0))
    assert result is CONN and len(calls) == 4          # 1 try + 3 retries
    assert not should_fallback_to_browser(result)      # and it still must not open a browser


@pytest.mark.parametrize("first", [ARCHIVED, NO_LINKS, R(False, "rate limited", 429),
                                   R(False, "bad key", 401), OK])
def test_never_retries_anything_but_a_transient_failure(first):
    call, calls = _seq(first)
    assert download_with_retry(call, threading.Event(), delays=(0, 0, 0)) is first
    assert len(calls) == 1


def test_a_cancel_stops_the_retries():
    stop = threading.Event()
    stop.set()
    call, calls = _seq(CONN)
    assert download_with_retry(call, stop, delays=(5, 5, 5)) is CONN
    assert len(calls) == 1


def test_a_cancel_during_the_wait_stops_the_retries():
    stop = threading.Event()
    call, calls = _seq(CONN, OK)
    threading.Timer(0.05, stop.set).start()
    result = download_with_retry(call, stop, delays=(5, 5))
    assert result is CONN and len(calls) == 1


def test_none_result_is_passed_through():
    call, calls = _seq(None)
    assert download_with_retry(call, threading.Event(), delays=(0,)) is None

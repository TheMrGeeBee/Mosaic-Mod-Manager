"""ModholdAPI: params, bearer header, retries, error mapping, limiter, download link."""
from __future__ import annotations

import pytest
import requests

from Modhold import modhold_api
from Modhold.modhold_api import (
    API_BASE,
    ModholdAPI,
    ModholdAuthError,
    ModholdError,
    ModholdGone,
    ModholdNotEntitled,
    ModholdNotFound,
    ModholdNotReady,
    ModholdRateLimited,
    _TokenBucket,
)


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Session:
    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _api(*responses, key="", sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return (ModholdAPI(key, session=_Session(*responses), sleep=sleeps.append),
            sleeps)


def test_list_mods_sends_game_sort_page_and_drops_empty_params():
    api, _ = _api(_Resp(body={"total": 0, "mods": []}))
    api.list_mods("skyrim-se", sort="downloads", q="", page=2)
    call = api._session.calls[0]
    assert call["url"] == f"{API_BASE}/mods"
    assert call["params"] == {"game": "skyrim-se", "sort": "downloads", "page": 2}


def test_no_key_sends_no_authorization_header():
    api, _ = _api(_Resp(body={"mods": []}))
    api.list_mods("skyrim-se")
    headers = api._session.calls[0]["headers"]
    assert "Authorization" not in headers
    assert headers["User-Agent"].startswith("MosaicModManager/")


def test_key_is_sent_as_bearer():
    api, _ = _api(_Resp(body={"id": 12}), key="  mhm_abc  ")
    api.get_mod(12)
    assert api._session.calls[0]["headers"]["Authorization"] == "Bearer mhm_abc"


def test_list_calls_are_cached_for_a_minute():
    api, _ = _api(_Resp(body={"mods": [1]}))
    first = api.list_mods("skyrim-se")
    second = api.list_mods("skyrim-se")
    assert first is second
    assert len(api._session.calls) == 1


def test_429_waits_retry_after_then_succeeds():
    api, sleeps = _api(_Resp(429, headers={"Retry-After": "3"}),
                       _Resp(body={"id": 1}))
    assert api.get_mod(1) == {"id": 1}
    assert 3.0 in sleeps


def test_429_forever_raises_rate_limited():
    api, _ = _api(*[_Resp(429) for _ in range(10)])
    with pytest.raises(ModholdRateLimited):
        api.get_mod(1)


def test_server_error_retries_then_raises():
    api, _ = _api(*[_Resp(503) for _ in range(10)])
    with pytest.raises(ModholdError) as ei:
        api.get_mod(1)
    assert ei.value.status == 503


def test_network_error_retries_then_raises():
    api, _ = _api(*[requests.ConnectionError("down") for _ in range(10)])
    with pytest.raises(ModholdError):
        api.get_mod(1)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_statuses_map_to_auth_error(status):
    api, _ = _api(_Resp(status, body={"error": "Pass required"}), key="mhm_x")
    with pytest.raises(ModholdAuthError) as ei:
        api.get_mod(1)
    assert "Pass required" in str(ei.value)


@pytest.mark.parametrize("status,cls", [
    (402, ModholdNotEntitled), (409, ModholdNotReady), (410, ModholdGone),
])
def test_402_409_410_are_their_own_errors_with_the_servers_message(status, cls):
    api, _ = _api(_Resp(status, body={"error": "Written for players."}), key="mhm_x")
    with pytest.raises(cls) as ei:
        api.file_download(31)
    assert str(ei.value) == "Written for players." and ei.value.status == status
    assert len(api._session.calls) == 1                    # a refusal is never retried


def test_401_without_a_key_is_the_connect_message():
    api, _ = _api(_Resp(401, body={"error": "A Modhold key is needed."}), key="mhm_x")
    with pytest.raises(ModholdAuthError) as ei:
        api.get_mod(1)
    assert "key is needed" in str(ei.value)


def test_404_maps_to_not_found():
    api, _ = _api(_Resp(404))
    with pytest.raises(ModholdNotFound):
        api.get_loadout(99)


def test_non_json_reply_is_an_error():
    api, _ = _api(_Resp(200, body=None))
    with pytest.raises(ModholdError):
        api.get_mod(1)


def test_stats_joins_ids():
    api, _ = _api(_Resp(body={}))
    api.stats([12, 40, 77])
    assert api._session.calls[0]["params"] == {"ids": "12,40,77"}


def test_list_loadouts_unwraps_the_list():
    api, _ = _api(_Resp(body={"loadouts": [{"id": 3}]}))
    assert api.list_loadouts("skyrim-se") == [{"id": 3}]


def test_file_download_needs_a_key():
    api, _ = _api()
    with pytest.raises(ModholdAuthError):
        api.file_download(31)
    assert api._session.calls == []


def test_file_download_returns_link_size_and_lowercase_sha():
    # Shape confirmed against the live API for file 587 on 2026-11-21.
    api, _ = _api(_Resp(body={
        "url": "https://modholdmods.com/dl/f.zip?sig=x", "name": "F 1.0.1.zip",
        "size": 42, "sha256": "ABCD", "expires": "2026-11-21T20:49:00.000Z",
        "file": {"id": 31}, "mod": {"id": 12}, "game": {"slug": "skyrim-se"}}),
        key="mhm_x")
    link = api.file_download(31)
    assert (link.url, link.size, link.sha256) == (
        "https://modholdmods.com/dl/f.zip?sig=x", 42, "abcd")
    assert (link.name, link.expires) == ("F 1.0.1.zip", "2026-11-21T20:49:00.000Z")
    assert api._session.calls[0]["url"] == f"{API_BASE}/files/31/download"


def test_file_download_without_a_link_is_an_error():
    api, _ = _api(_Resp(body={"size": 1}), key="mhm_x")
    with pytest.raises(ModholdError):
        api.file_download(31)


def test_token_bucket_waits_when_empty_and_refills_with_time():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    bucket = _TokenBucket(rate=2.0, burst=2, clock=lambda: now[0], sleep=sleep)
    bucket.take()
    bucket.take()
    assert slept == []          # the burst is free
    bucket.take()
    assert slept and slept[0] == pytest.approx(0.5)   # 1 token at 2/s


def test_module_rate_stays_under_the_documented_limit():
    assert modhold_api._RATE_PER_SEC * 60 < 600

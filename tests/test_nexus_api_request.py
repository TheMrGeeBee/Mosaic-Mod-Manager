"""NexusAPI._get / _post_v3 share one request loop: 429 back-off, 401, errors."""
from __future__ import annotations

import pytest
import requests

from Nexus import nexus_api
from Nexus.nexus_api import API_BASE, V3_BASE, NexusAPI, NexusAPIError, RateLimitError


class _Resp:
    def __init__(self, status=200, body=None, text="", reason="Reason"):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = text
        self.reason = reason
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def _next(self, kind, url, kw):
        self.calls.append((kind, url, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kw):
        return self._next("get", url, kw)

    def post(self, url, **kw):
        return self._next("post", url, kw)


@pytest.fixture
def make(monkeypatch):
    sleeps = []
    monkeypatch.setattr(nexus_api.time, "sleep", sleeps.append)

    def _make(*responses):
        api = NexusAPI("fake-key")
        api._session = _Session(responses)
        monkeypatch.setattr(api, "_refresh_oauth_if_needed", lambda: None)
        return api, sleeps

    return _make


def test_get_returns_json_and_passes_params(make):
    api, _ = make(_Resp(body={"ok": 1}))
    assert api._get("/x.json", {"a": 1}) == {"ok": 1}
    kind, url, kw = api._session.calls[0]
    assert (kind, url) == ("get", API_BASE + "/x.json")
    assert kw["params"] == {"a": 1} and "timeout" in kw


def test_post_v3_sends_json_body(make):
    api, _ = make(_Resp(body={"ok": 2}))
    assert api._post_v3("/y", {"k": "v"}) == {"ok": 2}
    kind, url, kw = api._session.calls[0]
    assert (kind, url) == ("post", V3_BASE + "/y")
    assert kw["json"] == {"k": "v"}


@pytest.mark.parametrize("call", ["get", "post"])
def test_429_backs_off_linearly_then_succeeds(make, call):
    api, sleeps = make(_Resp(429), _Resp(429), _Resp(body={"ok": 1}))
    result = api._get("/x") if call == "get" else api._post_v3("/x", {})
    assert result == {"ok": 1}
    assert sleeps == [2.0, 4.0]


@pytest.mark.parametrize("call", ["get", "post"])
def test_persistent_429_raises_rate_limit_error(make, call):
    api, sleeps = make(_Resp(429), _Resp(429), _Resp(429))
    with pytest.raises(RateLimitError):
        api._get("/x") if call == "get" else api._post_v3("/x", {})
    assert sleeps == [2.0, 4.0, 6.0]


def test_401_raises_without_retry(make):
    api, sleeps = make(_Resp(401))
    with pytest.raises(NexusAPIError) as ei:
        api._get("/x")
    assert ei.value.status_code == 401 and not sleeps


def test_connection_error_is_wrapped(make):
    api, _ = make(requests.ConnectionError("boom"))
    with pytest.raises(NexusAPIError, match="Connection failed"):
        api._get("/x")


def test_timeout_is_wrapped(make):
    api, _ = make(requests.Timeout())
    with pytest.raises(NexusAPIError, match="timed out"):
        api._post_v3("/x", {})


def test_v3_error_prefers_message_then_title_then_reason(make):
    api, _ = make(_Resp(400, body={"message": "bad thing"}))
    with pytest.raises(NexusAPIError, match="bad thing"):
        api._post_v3("/x", {})
    api, _ = make(_Resp(400, body={"title": "A title"}))
    with pytest.raises(NexusAPIError, match="A title"):
        api._post_v3("/x", {})
    api, _ = make(_Resp(500, text="", reason="Server Error"))
    with pytest.raises(NexusAPIError, match="Server Error"):
        api._post_v3("/x", {})


def test_get_error_uses_the_shared_message_helper(make):
    api, _ = make(_Resp(404, body={"message": "Not found here"}))
    with pytest.raises(NexusAPIError) as ei:
        api._get("/x")
    assert ei.value.status_code == 404

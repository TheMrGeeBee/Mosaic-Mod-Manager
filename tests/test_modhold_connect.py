"""ModholdConnect: loopback-only listener, exact path, state check, one-shot."""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlparse

import pytest

from Modhold.modhold_connect import CALLBACK_PATH, ModholdConnect


def _fetch(port, path_and_query):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path_and_query}", timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def conn():
    got, errs, done = [], [], threading.Event()

    def on_key(k, u):
        got.append((k, u))
        done.set()

    c = ModholdConnect(on_key, errs.append, timeout=30)
    yield c, got, errs, done
    c.cancel()


def test_start_url_points_at_modhold_with_loopback_return_and_state(conn):
    c, *_ = conn
    url = c.start()
    q = parse_qs(urlparse(url).query)
    assert urlparse(url).netloc == "modholdmods.com"
    assert urlparse(url).path == "/connect"
    assert q["state"] == [c.state]
    back = urlparse(q["return"][0])
    assert back.hostname == "localhost"
    assert back.port == c.port and back.path == CALLBACK_PATH


def test_listener_is_bound_to_loopback_only(conn):
    c, *_ = conn
    c.start()
    assert c._server.server_address[0] == "127.0.0.1"


def test_valid_callback_delivers_key_and_user(conn):
    c, got, errs, done = conn
    c.start()
    status = _fetch(c.port, f"{CALLBACK_PATH}?modhold_key=mhm_abc"
                            f"&state={quote(c.state)}&user=Sunny")
    assert status == 200
    assert done.wait(5)
    assert got == [("mhm_abc", "Sunny")]
    assert errs == []


def test_wrong_state_is_refused_and_listener_keeps_waiting(conn):
    c, got, _, done = conn
    c.start()
    assert _fetch(c.port, f"{CALLBACK_PATH}?modhold_key=mhm_evil&state=nope") == 400
    assert got == []
    # a correct callback afterwards still works
    assert _fetch(c.port, f"{CALLBACK_PATH}?modhold_key=mhm_ok"
                          f"&state={quote(c.state)}") == 200
    assert done.wait(5)
    assert got[0][0] == "mhm_ok"


def test_missing_key_is_refused(conn):
    c, got, *_ = conn
    c.start()
    assert _fetch(c.port, f"{CALLBACK_PATH}?state={quote(c.state)}") == 400
    assert got == []


def test_other_paths_are_404(conn):
    c, got, *_ = conn
    c.start()
    assert _fetch(c.port, f"/elsewhere?modhold_key=mhm_x&state={quote(c.state)}") == 404
    assert got == []


def test_listener_stops_after_one_callback(conn):
    c, _, _, done = conn
    c.start()
    port = c.port
    _fetch(port, f"{CALLBACK_PATH}?modhold_key=mhm_a&state={quote(c.state)}")
    assert done.wait(5)
    c._thread.join(5)
    assert not c._thread.is_alive()
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}{CALLBACK_PATH}", timeout=2)


def test_timeout_reports_an_error_once():
    errs, done = [], threading.Event()

    def on_error(msg):
        errs.append(msg)
        done.set()

    c = ModholdConnect(lambda *_: None, on_error, timeout=0.05)
    c.start()
    assert done.wait(5)
    assert len(errs) == 1 and "Timed out" in errs[0]


def test_cancel_reports_nothing():
    errs = []
    c = ModholdConnect(lambda *_: None, errs.append, timeout=0.05)
    c.start()
    c.cancel()
    threading.Event().wait(0.2)
    assert errs == []

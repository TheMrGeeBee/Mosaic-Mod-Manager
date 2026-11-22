"""
modhold_connect.py
Sign in to Modhold through its Connect flow.

The player signs in on modholdmods.com and says yes once; Modhold then sends
the browser back to a localhost address with ``modhold_key``, ``state`` and
``user`` in the query. This module runs the one-shot listener on that address:

* bound to 127.0.0.1 only, on an ephemeral port;
* answers only the exact callback path, and only when ``state`` matches the
  random value we generated (anything else is refused);
* stops after the first valid callback, on cancel, or after the timeout.

The key is handed to the caller and never logged.
"""

from __future__ import annotations

import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, urlparse

from Modhold.modhold_api import BASE_URL

CALLBACK_PATH = "/modhold"
APP_NAME = "Mosaic Mod Manager"
DEFAULT_TIMEOUT = 300.0

_DONE_PAGE = (b"<!doctype html><meta charset=utf-8><title>Mosaic</title>"
              b"<body style='font-family:sans-serif;text-align:center;margin-top:4em'>"
              b"<h2>Connected to Modhold</h2><p>You can close this tab and "
              b"return to Mosaic.</p>")
_FAIL_PAGE = (b"<!doctype html><meta charset=utf-8><title>Mosaic</title>"
              b"<body style='font-family:sans-serif;text-align:center;margin-top:4em'>"
              b"<h2>Could not connect</h2><p>Go back to Mosaic and try again.</p>")


class ModholdConnect:
    """One sign-in attempt. ``start()`` returns the URL to open in the browser."""

    def __init__(self,
                 on_key: Callable[[str, str], None],
                 on_error: Callable[[str], None],
                 *, timeout: float = DEFAULT_TIMEOUT):
        self._on_key = on_key
        self._on_error = on_error
        self._timeout = timeout
        self._state = secrets.token_urlsafe(24)
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._timer: Optional[threading.Timer] = None
        self._finished = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        return self._state

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    def start(self) -> str:
        """Start listening and return the Connect URL to open."""
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                outer._handle(self)

            def log_message(self, *_args):  # never log: the query holds the key
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="modhold-connect", daemon=True)
        self._thread.start()
        self._timer = threading.Timer(self._timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()
        back = f"http://localhost:{self.port}{CALLBACK_PATH}"
        return (f"{BASE_URL}/connect?app={quote(APP_NAME)}"
                f"&return={quote(back, safe='')}&state={quote(self._state)}")

    def cancel(self) -> None:
        """Stop without reporting anything (the user closed the dialog)."""
        self._finish()

    # --------------------------------------------------------------- internals

    def _handle(self, req: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(req.path)
        if parsed.path != CALLBACK_PATH:
            self._reply(req, 404, _FAIL_PAGE)
            return
        q = parse_qs(parsed.query)
        got_state = (q.get("state") or [""])[0]
        key = (q.get("modhold_key") or [""])[0].strip()
        user = (q.get("user") or [""])[0].strip()
        if not secrets.compare_digest(got_state, self._state) or not key:
            self._reply(req, 400, _FAIL_PAGE)
            return
        with self._lock:
            if self._finished:
                self._reply(req, 400, _FAIL_PAGE)
                return
        self._reply(req, 200, _DONE_PAGE)
        self._finish()
        self._on_key(key, user)

    @staticmethod
    def _reply(req: BaseHTTPRequestHandler, code: int, body: bytes) -> None:
        req.send_response(code)
        req.send_header("Content-Type", "text/html; charset=utf-8")
        req.send_header("Content-Length", str(len(body)))
        req.send_header("Cache-Control", "no-store")
        req.end_headers()
        req.wfile.write(body)

    def _expire(self) -> None:
        if self._finish():
            self._on_error("Timed out waiting for Modhold. Try connecting again.")

    def _finish(self) -> bool:
        """Shut everything down once. True only for the call that did it."""
        with self._lock:
            if self._finished:
                return False
            self._finished = True
        if self._timer:
            self._timer.cancel()
        server = self._server
        if server:
            threading.Thread(target=server.shutdown, daemon=True).start()
            try:
                server.server_close()
            except OSError:
                pass
        return True

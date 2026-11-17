"""A dropped connection must not send a Wabbajack Nexus archive straight to the
manual-download queue: transient failures are retried, real refusals are not."""
from __future__ import annotations

import threading
from pathlib import Path

from Nexus.nexus_download import DownloadResult
from Utils.wabbajack.downloaders.nexus_source import download_nexus
from Utils.wabbajack.wabbajack_manifest import NexusState

CONN = DownloadResult(
    False, error="Connection failed: ('Connection aborted.', ConnectionResetError(104))")
OK = DownloadResult(True, file_path=Path("/x/a.zip"), bytes_downloaded=5)


def _state():
    return NexusState(game_name="VtMB", mod_id=434, file_id=1, name="Cheat Menu.zip")


class _Downloader:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def download_file(self, *a, **kw):
        self.calls += 1
        return self.results.pop(0)


def test_transient_failure_is_retried_then_succeeds():
    d = _Downloader(CONN, CONN, OK)
    logs = []
    r = download_nexus(_state(), d, Path("/x"), log=logs.append, retry_delays=(0, 0, 0))
    assert r.success and r.file_path == Path("/x/a.zip")
    assert d.calls == 3 and len(logs) == 2
    assert logs[0].startswith("Wabbajack: 'Cheat Menu.zip'") and "retry 1/3" in logs[0]


def test_gives_up_after_the_retries_and_reports_the_error():
    d = _Downloader(CONN, CONN, CONN, CONN)
    r = download_nexus(_state(), d, Path("/x"), retry_delays=(0, 0, 0))
    assert not r.success and "Connection failed" in r.error
    assert d.calls == 4


def test_real_refusal_is_not_retried():
    d = _Downloader(DownloadResult(False, error="No File found", status_code=404))
    r = download_nexus(_state(), d, Path("/x"), retry_delays=(0, 0, 0))
    assert not r.success and d.calls == 1


def test_rate_limit_is_not_retried():
    d = _Downloader(DownloadResult(False, error="Rate limit", status_code=429))
    download_nexus(_state(), d, Path("/x"), retry_delays=(0, 0, 0))
    assert d.calls == 1


def test_cancel_stops_the_wait():
    cancel = threading.Event()
    cancel.set()
    d = _Downloader(CONN, OK)
    r = download_nexus(_state(), d, Path("/x"), cancel=cancel, retry_delays=(5, 5))
    assert not r.success and d.calls == 1


def test_first_try_success_makes_one_call():
    d = _Downloader(OK)
    assert download_nexus(_state(), d, Path("/x"), retry_delays=(0,)).success
    assert d.calls == 1

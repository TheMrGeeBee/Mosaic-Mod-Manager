"""start_manual_install warns up front when no download folder is watched."""
from pathlib import Path
from types import SimpleNamespace

from Nexus import manual_download_watch as mdw


class _NoopWatcher:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


def _run(monkeypatch, dirs):
    monkeypatch.setattr(mdw, "scan_download_dirs", lambda: dirs)
    monkeypatch.setattr(mdw, "find_existing_archive", lambda *a, **k: None)
    monkeypatch.setattr(mdw, "ManualDownloadWatcher", _NoopWatcher)
    logs = []
    f = SimpleNamespace(file_id=1, file_name="x.zip", name="x",
                        size_in_bytes=0, size_kb=0)
    mdw.start_manual_install(api=None, game_domain="g", mod_id=1, files=[f],
                             open_url_fn=lambda u: None, log_fn=logs.append)
    return logs


def test_warns_when_nothing_watched(monkeypatch):
    logs = _run(monkeypatch, [])
    assert any("no download folder is being watched" in m for m in logs)


def test_silent_when_folder_watched(monkeypatch, tmp_path):
    logs = _run(monkeypatch, [Path(tmp_path)])
    assert not any("WARNING" in m for m in logs)

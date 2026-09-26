"""The release workflow's Nexus uploader (.github/scripts/nexus_upload.py), run end to
end against a local mock of the Nexus v3 API.

Why it exists: Nexus-Mods/upload-action never named the version being replaced, so the
old Main file landed as "Old version" and was archived by hand every release."""
from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "nexus_upload.py"


def _load(monkeypatch, base=None):
    if base:
        monkeypatch.setenv("NEXUSMODS_API_BASE", base)
    spec = importlib.util.spec_from_file_location("nexus_upload", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    return mod


class Mock:
    """Just enough of the API. ``archives`` = whether Nexus honours archiving."""

    def __init__(self):
        self.versions = [
            {"id": "100", "version": "1.0.0", "category": "archived", "position": "1.0"},
            {"id": "200", "version": "1.1.0", "category": "main", "position": "2.0"},
        ]
        self.parts: dict[int, bytes] = {}
        self.created: dict | None = None
        self.changelog: dict | None = None
        self.calls: list[tuple[str, str]] = []
        self.reject_previous = False
        self.archives = True
        self.headers_seen: list[str] = []


def _serve(mock: Mock):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, headers=None):
            raw = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_GET(self):
            mock.calls.append(("GET", self.path))
            if self.path.endswith("/versions"):
                self._send(200, {"data": {"versions": list(reversed(mock.versions))}})
            elif self.path.startswith("/uploads/"):
                self._send(200, {"data": {"state": "available"}})
            else:
                self._send(404, {})

        def do_PUT(self):
            n = int(self.path.rsplit("/", 1)[1])
            mock.parts[n] = self._body()
            self._send(200, headers={"ETag": f'"etag{n}"'})

        def do_POST(self):
            mock.calls.append(("POST", self.path))
            raw = self._body()
            if self.path == "/uploads/multipart":
                mock.headers_seen.append(self.headers.get("apikey", ""))
                port = self.server.server_address[1]
                self._send(200, {"data": {
                    "id": "up1", "part_size_bytes": 4,
                    "part_presigned_urls": [f"http://127.0.0.1:{port}/part/{i}" for i in (1, 2, 3)],
                    "complete_presigned_url": f"http://127.0.0.1:{port}/complete"}})
            elif self.path == "/complete":
                self._send(200)
            elif self.path == "/uploads/up1/finalise":
                self._send(200, {"data": {"id": "up1", "state": "created"}})
            elif self.path.endswith("/versions"):
                body = json.loads(raw)
                if mock.reject_previous and "previous_version_id" in body:
                    self._send(422, {"detail": "unknown previous_version_id"})
                    return
                mock.created = body
                if mock.archives and body.get("archive_existing_file") and body.get("previous_version_id"):
                    for v in mock.versions:
                        if v["id"] == body["previous_version_id"]:
                            v["category"] = "archived"
                else:
                    for v in mock.versions:            # Nexus's own fallback
                        if v["category"] == "main":
                            v["category"] = "old_version"
                mock.versions.append({"id": "300", "version": body["version"],
                                      "category": "main", "position": "3.0"})
                self._send(201, {"data": {"file": {}, "version": {"id": "300", "position": "3.0"}}})
            elif self.path.endswith("/changelogs"):
                mock.changelog = json.loads(raw)
                self._send(201, {})
            else:
                self._send(404, {})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def env(tmp_path, monkeypatch):
    mock = Mock()
    srv = _serve(mock)
    monkeypatch.setenv("NEXUSMODS_API_KEY", "secret-key")
    mod = _load(monkeypatch, f"http://127.0.0.1:{srv.server_address[1]}")
    f = tmp_path / "pkg.zip"
    f.write_bytes(b"ABCDEFGHIJ")                      # 10 bytes -> parts of 4, 4, 2
    yield mod, mock, f, tmp_path
    srv.shutdown()


def _args(f, *extra):
    return ["--file-id", "7", "--file", str(f), "--name", "Mosaic Mod Manager - AppImage",
            "--version", "1.2.0", *extra]


def test_happy_path_names_the_previous_version_and_archives_it(env, capsys):
    mod, mock, f, tmp = env
    (tmp / "cl.md").write_text("### Bug Fixes\n- fixed a thing\n")
    rc = mod.main(_args(f, "--archive-existing", "--update-mod-version",
                        "--mod-id", "9856949946459", "--changelog-file", str(tmp / "cl.md")))
    assert rc == 0
    assert b"".join(mock.parts[i] for i in (1, 2, 3)) == b"ABCDEFGHIJ"
    assert mock.created == {
        "upload_id": "up1", "name": "Mosaic Mod Manager - AppImage", "version": "1.2.0",
        "file_category": "main", "allow_mod_manager_download": False,
        "update_mod_version": True, "archive_existing_file": True,
        "previous_version_id": "200"}
    assert mock.changelog == {"version": "1.2.0", "changelog": "### Bug Fixes\n- fixed a thing"}
    assert {v["id"]: v["category"] for v in mock.versions}["200"] == "archived"
    out = capsys.readouterr().out
    assert "Verified: previous version 1.1.0 is Archived" in out and "::warning::" not in out
    assert mock.headers_seen == ["secret-key"]
    assert "secret-key" not in out


def test_mod_manager_download_can_be_enabled(env):
    mod, mock, f, _ = env
    assert mod.main(_args(f, "--allow-mod-manager-download", "true")) == 0
    assert mock.created["allow_mod_manager_download"] is True


def test_no_archive_flag_means_no_previous_version_and_no_lookup(env):
    mod, mock, f, _ = env
    assert mod.main(_args(f)) == 0
    assert "previous_version_id" not in mock.created and mock.created["archive_existing_file"] is False
    assert ("GET", "/mod-files/7/versions") not in mock.calls


def test_a_refused_previous_version_id_does_not_cost_the_release(env, capsys):
    mod, mock, f, _ = env
    mock.reject_previous = True
    assert mod.main(_args(f, "--archive-existing")) == 0
    assert mock.created is not None and "previous_version_id" not in mock.created
    out = capsys.readouterr().out
    assert "retrying without it" in out
    assert "is 'old_version', not archived" in out       # the failed guess is loud


def test_warns_when_nexus_did_not_archive(env, capsys):
    mod, mock, f, _ = env
    mock.archives = False
    assert mod.main(_args(f, "--archive-existing")) == 0
    assert "::warning::Previous version 1.1.0 is 'old_version'" in capsys.readouterr().out


def test_failed_lookup_still_uploads(env, capsys, monkeypatch):
    mod, mock, f, _ = env

    def boom(*_a, **_k):
        raise RuntimeError("nope")
    monkeypatch.setattr(mod, "list_versions", boom)
    assert mod.main(_args(f, "--archive-existing")) == 0
    assert mock.created is not None and "previous_version_id" not in mock.created
    assert "Could not look up the version being replaced" in capsys.readouterr().out


def test_missing_key_or_file_or_mod_id(env, tmp_path, monkeypatch):
    mod, mock, f, _ = env
    (tmp_path / "cl.md").write_text("x")
    assert mod.main(_args(tmp_path / "nope.zip")) == 2
    assert mod.main(_args(f, "--changelog-file", str(tmp_path / "cl.md"))) == 2   # no --mod-id
    monkeypatch.delenv("NEXUSMODS_API_KEY")
    assert mod.main(_args(f)) == 2
    assert mock.created is None


@pytest.mark.parametrize("versions, expected", [
    ([], None),
    ([{"id": "a", "category": "archived", "position": "1.0"}], None),
    ([{"id": "a", "category": "main", "position": "9.0"},
      {"id": "b", "category": "main", "position": "10.0"}], "b"),          # numeric, not textual
    ([{"id": "a", "category": "old_version", "position": "3.0"},
      {"id": "b", "category": "archived", "position": "4.0"}], "a"),       # no Main: newest live one
    ([{"id": "a", "category": "main", "position": "1.0"},
      {"id": "b", "category": "optional", "position": "5.0"}], "a"),       # Main wins over newer non-main
])
def test_find_previous(env, versions, expected):
    mod = env[0]
    got = mod.find_previous(versions)
    assert (got["id"] if got else None) == expected

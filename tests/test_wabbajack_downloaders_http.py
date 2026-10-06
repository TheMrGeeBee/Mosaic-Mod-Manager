"""Tests for Utils.wabbajack.downloaders.http_source (direct HTTP + Wabbajack
CDN downloads). No real network access -- ``requests.get``/``requests.Session``
calls are monkeypatched with small fakes, matching the repo's existing
monkeypatch-the-module convention (see test_collection_install_modio_offsite.py)
rather than a requests-mocking library.
"""
from __future__ import annotations

import threading

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.downloaders import http_source


class _FakeResponse:
    def __init__(self, *, body=b"", headers=None, json_data=None, status=200):
        self._body = body
        self.headers = headers or {}
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise http_source.requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def json(self):
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_http_writes_file_and_reports_bytes(tmp_path, monkeypatch):
    body = b"archive payload bytes"
    monkeypatch.setattr(
        http_source.requests, "get",
        lambda url, **kw: _FakeResponse(body=body, headers={"Content-Length": str(len(body))}))

    state = wm.HttpState(url="https://example.com/a.7z")
    dest = tmp_path / "a.7z"
    result = http_source.download_http(state, dest)

    assert result.success
    assert result.bytes_downloaded == len(body)
    assert dest.read_bytes() == body


def test_download_http_passes_headers(tmp_path, monkeypatch):
    captured = {}

    def fake_get(url, **kw):
        captured.update(kw)
        return _FakeResponse(body=b"x")

    monkeypatch.setattr(http_source.requests, "get", fake_get)
    state = wm.HttpState(url="https://example.com/a.7z", headers=["Authorization:Bearer tok"])
    http_source.download_http(state, tmp_path / "a.7z")
    assert captured["headers"] == {"Authorization": "Bearer tok"}


def test_download_http_no_url_fails_without_network_call():
    result = http_source.download_http(wm.HttpState(url=""), "ignored")
    assert not result.success
    assert "URL" in result.error


def test_download_http_cancel_event_aborts_and_cleans_up(tmp_path, monkeypatch):
    body = b"x" * 10
    monkeypatch.setattr(
        http_source.requests, "get", lambda url, **kw: _FakeResponse(body=body))
    cancel = threading.Event()
    cancel.set()
    dest = tmp_path / "a.7z"
    result = http_source.download_http(wm.HttpState(url="https://x/a"), dest, cancel=cancel)
    assert not result.success
    assert result.error == "cancelled"
    assert not dest.exists()


def test_download_http_raises_http_error_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        http_source.requests, "get", lambda url, **kw: _FakeResponse(status=404))
    result = http_source.download_http(wm.HttpState(url="https://x/missing"), tmp_path / "a.7z")
    assert not result.success


# ---------------------------------------------------------------------------
# WabbajackCDN -- layout observed on authored-files.wabbajack.org: the URL is
# a folder (a bare GET is an HTML page); <url>/definition.json.gz is gzipped
# JSON with Parts (no URLs); each part is at <url>/parts/<Index>.
# ---------------------------------------------------------------------------

import gzip  # noqa: E402
import json  # noqa: E402

from Utils.wabbajack.wabbajack_hash import hash_bytes  # noqa: E402

# Verbatim from the real Halgari's Helper definition (note the server's own
# misspelt ServerAssingedUniqueId).
_HALGARI_DEFINITION = (
    '{"Author":"github/halgari","OriginalFileName":"HalgarisHelper.wabbajack",'
    '"Size":1588954,"Hash":"83fJglT61Dk=","Parts":[{"Size":1588954,"Offset":0,'
    '"Hash":"83fJglT61Dk=","Index":0}],"ServerAssignedUniqueId":null,'
    '"MungedName":"HalgarisHelper.wabbajack_5d55cc2d-2dbd-49ba-82d5-a66fb0572c54",'
    '"ServerAssingedUniqueId":"5d55cc2d-2dbd-49ba-82d5-a66fb0572c54","UploadedAt":1698095073}')


class _CdnResponse(_FakeResponse):
    @property
    def content(self):
        return self._body


def _cdn_server(monkeypatch, base, parts, *, gzipped=True, size=None, whole_hash=None,
                part_hashes=None):
    """Fake CDN: parts listed out of order on purpose; ``requests`` records
    every URL fetched."""
    whole = b"".join(parts)
    definition = {
        "Size": len(whole) if size is None else size,
        "Hash": hash_bytes(whole) if whole_hash is None else whole_hash,
        "Parts": [{"Index": i, "Size": len(b), "Offset": sum(map(len, parts[:i])),
                   "Hash": (part_hashes or {}).get(i, hash_bytes(b))}
                  for i, b in reversed(list(enumerate(parts)))],
    }
    raw = json.dumps(definition).encode()
    routes = {f"{base}/definition.json.gz": gzip.compress(raw) if gzipped else raw,
              base: b"<html>CDN landing page</html>"}
    routes.update({f"{base}/parts/{i}": b for i, b in enumerate(parts)})
    fetched = []

    def fake_get(url, **kw):
        fetched.append(url)
        if url not in routes:
            return _CdnResponse(status=404)
        return _CdnResponse(body=routes[url])

    monkeypatch.setattr(http_source.requests, "get", fake_get)
    return fetched


def test_real_halgari_definition_parses():
    d = json.loads(_HALGARI_DEFINITION)
    assert [p["Index"] for p in d["Parts"]] == [0] and "Url" not in d["Parts"][0]


def test_cdn_downloads_parts_in_index_order_from_parts_urls(tmp_path, monkeypatch):
    base = "https://authored-files.wabbajack.org/Mod.wabbajack_uuid"
    parts = [b"AAAA" * 10, b"BBBB" * 10, b"CCCC" * 10]
    fetched = _cdn_server(monkeypatch, base, parts)
    dest = tmp_path / "Mod.7z"

    result = http_source.download_wabbajack_cdn(wm.WabbajackCDNState(url=base), dest)

    assert result.success, result.error
    assert dest.read_bytes() == b"".join(parts)
    assert fetched == [f"{base}/definition.json.gz", f"{base}/parts/0",
                       f"{base}/parts/1", f"{base}/parts/2"]
    assert base not in fetched  # the bare URL is an HTML page, never used


def test_cdn_accepts_definition_already_decompressed(tmp_path, monkeypatch):
    base = "https://authored-files.wabbajack.org/Mod.wabbajack_uuid"
    _cdn_server(monkeypatch, base, [b"payload"], gzipped=False)
    result = http_source.download_wabbajack_cdn(wm.WabbajackCDNState(url=base), tmp_path / "x")
    assert result.success, result.error


def test_cdn_percent_encodes_spaces_and_apostrophes(tmp_path, monkeypatch):
    raw_url = "https://authored-files.wabbajack.org/A Dragonborn's Fate.wabbajack_cb8be6e1"
    encoded = "https://authored-files.wabbajack.org/A%20Dragonborn%27s%20Fate.wabbajack_cb8be6e1"
    fetched = _cdn_server(monkeypatch, encoded, [b"data"])
    result = http_source.download_wabbajack_cdn(wm.WabbajackCDNState(url=raw_url), tmp_path / "x")
    assert result.success, result.error
    assert fetched[0] == f"{encoded}/definition.json.gz"


def test_cdn_base_url_is_idempotent_and_drops_trailing_slash():
    once = http_source.cdn_base_url("https://h.org/A Dragonborn's Fate.wabbajack_x/")
    assert once == "https://h.org/A%20Dragonborn%27s%20Fate.wabbajack_x"
    assert http_source.cdn_base_url(once) == once


def test_cdn_part_hash_mismatch_fails_and_cleans_up(tmp_path, monkeypatch):
    base = "https://authored-files.wabbajack.org/Mod.wabbajack_uuid"
    _cdn_server(monkeypatch, base, [b"one", b"two"], part_hashes={1: "wrong=="})
    dest = tmp_path / "x"
    result = http_source.download_wabbajack_cdn(wm.WabbajackCDNState(url=base), dest)
    assert not result.success and "part 1" in result.error
    assert not dest.exists()


def test_cdn_whole_file_hash_mismatch_fails(tmp_path, monkeypatch):
    base = "https://authored-files.wabbajack.org/Mod.wabbajack_uuid"
    _cdn_server(monkeypatch, base, [b"one"], whole_hash="wrong==")
    result = http_source.download_wabbajack_cdn(wm.WabbajackCDNState(url=base), tmp_path / "x")
    assert not result.success and "CDN's hash" in result.error


def test_cdn_missing_definition_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(http_source.requests, "get", lambda url, **kw: _CdnResponse(status=404))
    result = http_source.download_wabbajack_cdn(
        wm.WabbajackCDNState(url="https://h.org/gone"), tmp_path / "x")
    assert not result.success and "definition" in result.error


def test_download_wabbajack_cdn_no_url_fails():
    result = http_source.download_wabbajack_cdn(wm.WabbajackCDNState(url=""), "ignored")
    assert not result.success

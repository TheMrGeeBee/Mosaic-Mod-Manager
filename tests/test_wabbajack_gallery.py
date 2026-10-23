"""Tests for Utils.wabbajack.wabbajack_gallery (no network: the JSON fetch
and the downloaders are faked). Entry shapes follow the real gallery as
seen 2026-10-07."""
from __future__ import annotations

import json
import time

import pytest

from Utils.wabbajack import wabbajack_gallery as g
from Utils.wabbajack.downloaders.http_source import WabbajackDownloadResult
from Utils.wabbajack.wabbajack_hash import hash_bytes
from Utils.wabbajack.wabbajack_manifest import GoogleDriveState, HttpState, WabbajackCDNState


def _entry(title, game="skyrimspecialedition", url=None, **extra):
    d = {
        "title": title, "author": "Curator", "description": f"{title} description",
        "game": game, "version": "1.0", "dateUpdated": "2026-08-01", "nsfw": False,
        "official": False, "utility_list": False, "force_down": False, "tags": ["Lore"],
        "links": {"image": f"https://img/{title}.jpg", "readme": "https://readme",
                  "download": url or f"https://authored-files.wabbajack.org/{title}.wabbajack_x",
                  "machineURL": title.replace(" ", ""), "discordURL": "https://discord"},
        "download_metadata": {"Hash": "", "Size": 100, "NumberOfArchives": 3,
                              "SizeOfArchives": 5000, "NumberOfInstalledFiles": 10,
                              "SizeOfInstalledFiles": 9000},
    }
    d.update(extra)
    return d


def _fake_json(repos):
    """repos: {name: list-of-entries | Exception}"""
    calls = []

    def get_json(url):
        calls.append(url)
        if url == g.REPOSITORIES_URL:
            return {name: f"https://repo/{name}.json" for name in repos}
        data = repos[url.rsplit("/", 1)[1][:-len(".json")]]
        if isinstance(data, Exception):
            raise data
        return data
    return get_json, calls


def test_parse_entry_and_game_matching_ignores_case():
    e = g.parse_entry(_entry("A", game="Fallout4"), "repo")
    assert (e.title, e.machine_url, e.repository, e.mosaic_game) == ("A", "A", "repo", "Fallout 4")
    assert (e.archive_count, e.archives_size, e.install_size) == (3, 5000, 9000)
    assert g.parse_entry(_entry("B", game="cyberpunk2077")).mosaic_game == "Cyberpunk 2077"
    assert g.parse_entry(_entry("C", game="baldursgate3")).mosaic_game is None
    assert g.mosaic_game_for_gallery("skyrimspecialedition") == "Skyrim Special Edition"


def test_load_gallery_merges_repositories_and_caches(tmp_path):
    get_json, calls = _fake_json({"one": [_entry("A"), _entry("B")], "two": [_entry("C")]})
    cache = tmp_path / "gallery.json"
    first = g.load_gallery(cache, get_json=get_json)
    assert sorted(e.title for e in first) == ["A", "B", "C"]
    n = len(calls)
    again = g.load_gallery(cache, get_json=get_json)
    assert len(calls) == n                      # served from the fresh cache
    assert sorted(e.title for e in again) == ["A", "B", "C"]
    g.load_gallery(cache, get_json=get_json, force=True)
    assert len(calls) > n


def test_a_failing_repository_is_skipped_and_logged(tmp_path):
    get_json, _ = _fake_json({"good": [_entry("A")], "bad": OSError("404")})
    logs = []
    entries = g.load_gallery(tmp_path / "c.json", get_json=get_json, log=logs.append)
    assert [e.title for e in entries] == ["A"]
    assert any("bad" in m for m in logs)


def test_stale_cache_is_used_when_the_network_fails(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"fetched_at": time.time() - 99999,
                                 "entries": [_entry("Old")]}))

    def offline(url):
        raise OSError("offline")
    assert [e.title for e in g.load_gallery(cache, get_json=offline)] == ["Old"]
    with pytest.raises(g.GalleryError):
        g.load_gallery(tmp_path / "none.json", get_json=offline)


def test_download_state_for_each_real_host():
    def state(url):
        return g.download_state_for(g.parse_entry(_entry("X", url=url)))
    assert isinstance(state("https://authored-files.wabbajack.org/ONE.wabbajack_93bd"),
                      WabbajackCDNState)
    s = state("https://drive.google.com/file/d/15amRrHCVGUOtn3ZuKFpQKGF4hHbY2Cmu/view?usp=sharing")
    assert isinstance(s, GoogleDriveState) and s.file_id == "15amRrHCVGUOtn3ZuKFpQKGF4hHbY2Cmu"
    assert isinstance(state("https://github.com/x/y/releases/download/1.5.6/Aurora.wabbajack"),
                      HttpState)


def _with_hash(data: bytes):
    e = _entry("List")
    e["download_metadata"]["Hash"] = hash_bytes(data)
    return g.parse_entry(e)


def test_fetch_modlist_file_downloads_verifies_and_reuses(tmp_path, monkeypatch):
    from Utils.wabbajack.downloaders import http_source
    data = b"PK fake wabbajack"
    calls = []

    def fake_cdn(state, dest, *, progress_cb=None, cancel=None):
        calls.append(state.url)
        dest.write_bytes(data)
        return WabbajackDownloadResult(success=True, file_path=dest)
    monkeypatch.setattr(http_source, "download_wabbajack_cdn", fake_cdn)
    entry = _with_hash(data)
    path = g.fetch_modlist_file(entry, tmp_path)
    assert path == tmp_path / "List.wabbajack" and path.read_bytes() == data
    assert g.fetch_modlist_file(entry, tmp_path) == path
    assert len(calls) == 1                       # the verified copy is reused


def test_fetch_modlist_file_rejects_a_hash_mismatch(tmp_path, monkeypatch):
    from Utils.wabbajack.downloaders import http_source

    def fake_cdn(state, dest, *, progress_cb=None, cancel=None):
        dest.write_bytes(b"something else")
        return WabbajackDownloadResult(success=True, file_path=dest)
    monkeypatch.setattr(http_source, "download_wabbajack_cdn", fake_cdn)
    with pytest.raises(g.GalleryError, match="doesn't match"):
        g.fetch_modlist_file(_with_hash(b"expected"), tmp_path)
    assert list(tmp_path.iterdir()) == []

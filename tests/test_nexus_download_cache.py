"""NexusDownloader reuses an identical archive instead of re-fetching it.

Before, an NXM click always streamed the file and, when it already existed,
saved it beside the original as ``Name (1).zip``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Nexus.nexus_api import NexusDownloadLink, NexusModFile  # noqa: E402
from Nexus.nexus_download import (  # noqa: E402
    DownloadResult,
    NexusDownloader,
    _write_sidecar_file_id,
)
from Nexus.nxm_handler import NxmLink  # noqa: E402

DOMAIN, MOD, FILE = "skyrimspecialedition", 12345, 678
SIZE = 10_000
NAME = f"Cool Mod-{MOD}-1-0-1700000000.7z"          # a .7z: no zip integrity check applies


class FakeAPI:
    def __init__(self, size=SIZE, info_fails=False):
        self.size, self.info_fails = size, info_fails
        self.info_calls = self.link_calls = 0

    def get_file_info(self, domain, mod_id, file_id):
        self.info_calls += 1
        if self.info_fails:
            raise RuntimeError("api down")
        return NexusModFile(file_id=file_id, name="Cool Mod", version="1.0", category_name="MAIN",
                            file_name=NAME, size_in_bytes=self.size)

    def get_download_links(self, **kw):
        self.link_calls += 1
        return [NexusDownloadLink(name="CDN", short_name="cdn", URI="https://example.invalid/x")]


@pytest.fixture
def streamed(monkeypatch):
    """Stand-in for the network: records that a download happened."""
    calls = []

    def fake(self, url, file_name, dest_dir, progress_cb, cancel, game_domain, mod_id, file_id):
        calls.append(file_name)
        out = dest_dir / "fresh.7z"
        out.write_bytes(b"x" * SIZE)
        return DownloadResult(success=True, file_path=out, file_name=out.name,
                              game_domain=game_domain, mod_id=mod_id, file_id=file_id)
    monkeypatch.setattr(NexusDownloader, "_stream_download", fake)
    return calls


def _link():
    return NxmLink(game_domain=DOMAIN, mod_id=MOD, file_id=FILE, key="k", expires=9999999999)


def _archive(dl_dir, name=NAME, size=SIZE, sidecar=True):
    p = dl_dir / name
    p.write_bytes(b"y" * size)
    if sidecar:
        _write_sidecar_file_id(p, FILE)
    return p


def test_nxm_click_reuses_an_archive_mosaic_downloaded_before(tmp_path, streamed):
    have = _archive(tmp_path)
    api = FakeAPI()
    res = NexusDownloader(api, tmp_path).download_from_nxm(_link())
    assert res.success and res.from_cache and res.file_path == have
    assert api.link_calls == 0 and streamed == []          # the NXM key is not even used
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".7z"] == [NAME]   # no "(1)"


def test_nxm_click_reuses_an_archive_downloaded_by_hand(tmp_path, streamed):
    have = _archive(tmp_path, sidecar=False)               # saved from the Nexus site: no sidecar
    res = NexusDownloader(FakeAPI(), tmp_path).download_from_nxm(_link())
    assert res.from_cache and res.file_path == have and streamed == []


def test_a_truncated_archive_is_removed_and_fetched_again(tmp_path, streamed):
    partial = _archive(tmp_path, size=SIZE // 3)
    api = FakeAPI()
    res = NexusDownloader(api, tmp_path).download_from_nxm(_link())
    assert res.success and not res.from_cache and len(streamed) == 1 and api.link_calls == 1
    assert not partial.exists()


def test_a_different_file_of_the_same_mod_is_not_reused(tmp_path, streamed):
    other = tmp_path / f"Other Mod-{MOD}-2-0-1700000001.7z"
    other.write_bytes(b"z" * (SIZE * 3))
    _write_sidecar_file_id(other, FILE + 1)
    res = NexusDownloader(FakeAPI(), tmp_path).download_from_nxm(_link())
    assert not res.from_cache and len(streamed) == 1 and other.exists()


def test_without_a_known_size_nothing_is_reused(tmp_path, streamed):
    """A sidecar is written when a download starts, so it cannot prove completeness."""
    _archive(tmp_path)
    api = FakeAPI(info_fails=True)
    res = NexusDownloader(api, tmp_path).download_from_nxm(_link())
    assert res.success and not res.from_cache and len(streamed) == 1


def test_caller_supplied_name_and_size_skip_the_file_info_call(tmp_path, streamed):
    have = _archive(tmp_path)
    api = FakeAPI()
    res = NexusDownloader(api, tmp_path).download_from_nxm(
        _link(), known_file_name=NAME, expected_size_bytes=SIZE)
    assert res.from_cache and res.file_path == have
    assert api.info_calls == 0 and api.link_calls == 0


def test_direct_download_still_reuses_the_cache(tmp_path, streamed):
    have = _archive(tmp_path)
    api = FakeAPI()
    res = NexusDownloader(api, tmp_path).download_file(
        DOMAIN, MOD, FILE, known_file_name=NAME, expected_size_bytes=SIZE)
    assert res.from_cache and res.file_path == have and api.link_calls == 0

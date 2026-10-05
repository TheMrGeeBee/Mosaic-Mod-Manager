"""Tests for Utils.wabbajack.downloaders' resolve_downloader dispatch table."""
from __future__ import annotations

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.downloaders import (
    download_google_drive,
    download_http,
    download_mediafire,
    download_wabbajack_cdn,
    is_unsupported,
    resolve_downloader,
)


def test_resolve_downloader_http():
    assert resolve_downloader(wm.HttpState()) is download_http


def test_resolve_downloader_wabbajack_cdn():
    assert resolve_downloader(wm.WabbajackCDNState()) is download_wabbajack_cdn


def test_resolve_downloader_google_drive():
    assert resolve_downloader(wm.GoogleDriveState()) is download_google_drive


def test_resolve_downloader_mediafire():
    assert resolve_downloader(wm.MediaFireState()) is download_mediafire


def test_resolve_downloader_unimplemented_sources_return_none():
    # Nexus is deliberately excluded (needs a live NexusDownloader instance,
    # see the module docstring); Mega/LoversLab aren't implemented yet.
    for state in (wm.NexusState(), wm.MegaState(), wm.LoversLabState(), wm.ManualState()):
        assert resolve_downloader(state) is None


def test_is_unsupported():
    assert is_unsupported(wm.UnknownState(type_name="Future"))
    assert not is_unsupported(wm.HttpState())

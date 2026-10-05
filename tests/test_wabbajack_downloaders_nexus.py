"""Tests for Utils.wabbajack.downloaders.nexus_source -- the thin adapter
from a Wabbajack NexusState to the existing NexusDownloader. The downloader
itself is faked (no real Nexus/network access), matching
test_collection_install_modio_offsite.py's fake-the-collaborator convention.
"""
from __future__ import annotations

from Utils.wabbajack import wabbajack_manifest as wm
from Utils.wabbajack.downloaders import nexus_source


class _FakeDownloadResult:
    def __init__(self, success, file_path=None, error="", bytes_downloaded=0):
        self.success = success
        self.file_path = file_path
        self.error = error
        self.bytes_downloaded = bytes_downloaded


class _FakeNexusDownloader:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def download_file(self, domain, mod_id, file_id, **kwargs):
        self.calls.append((domain, mod_id, file_id, kwargs))
        return self._result


def test_nexus_domain_for_lowercases_by_default():
    assert nexus_source.nexus_domain_for("SkyrimSpecialEdition") == "skyrimspecialedition"


def test_nexus_domain_for_override():
    assert nexus_source.nexus_domain_for("FalloutNewVegas") == "newvegas"


def test_nexus_domain_for_empty():
    assert nexus_source.nexus_domain_for("") == ""
    assert nexus_source.nexus_domain_for(None) == ""


def test_download_nexus_success_translates_result(tmp_path):
    fake = _FakeNexusDownloader(_FakeDownloadResult(True, file_path=tmp_path / "a.7z", bytes_downloaded=123))
    state = wm.NexusState(game_name="SkyrimSpecialEdition", mod_id=1, file_id=2, name="Mod")
    result = nexus_source.download_nexus(state, fake, tmp_path)

    assert result.success
    assert result.bytes_downloaded == 123
    assert fake.calls == [("skyrimspecialedition", 1, 2, {
        "dest_dir": tmp_path, "progress_cb": None, "cancel": None, "known_file_name": "Mod"})]


def test_download_nexus_failure_passes_through_error(tmp_path):
    fake = _FakeNexusDownloader(_FakeDownloadResult(False, error="rate limited"))
    state = wm.NexusState(game_name="SkyrimSpecialEdition", mod_id=1, file_id=2)
    result = nexus_source.download_nexus(state, fake, tmp_path)
    assert not result.success
    assert result.error == "rate limited"


def test_download_nexus_incomplete_reference_short_circuits(tmp_path):
    fake = _FakeNexusDownloader(_FakeDownloadResult(True))
    state = wm.NexusState(game_name="SkyrimSpecialEdition", mod_id=0, file_id=2)
    result = nexus_source.download_nexus(state, fake, tmp_path)
    assert not result.success
    assert fake.calls == []  # never called the real downloader with a bad reference

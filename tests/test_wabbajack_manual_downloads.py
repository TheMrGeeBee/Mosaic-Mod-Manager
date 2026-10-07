"""Tests for Utils.wabbajack.manual_downloads: finding a hand-downloaded
archive by size and hash."""
from __future__ import annotations

from Utils.wabbajack import manual_downloads as md
from Utils.wabbajack.wabbajack_hash import hash_bytes


def test_finder_matches_size_and_hash_and_skips_partials(tmp_path):
    data = b"real archive"
    (tmp_path / "wrong.zip").write_bytes(b"x" * len(data))
    (tmp_path / "real.zip.crdownload").write_bytes(data)
    finder = md.DownloadFinder(len(data), hash_bytes(data), [tmp_path])
    assert finder.find() is None
    (tmp_path / "real.zip").write_bytes(data)
    assert finder.find() == tmp_path / "real.zip"


def test_finder_hashes_each_unchanged_candidate_once(tmp_path, monkeypatch):
    data = b"real archive"
    (tmp_path / "wrong.zip").write_bytes(b"x" * len(data))
    calls = []
    real = md.hash_file
    monkeypatch.setattr(md, "hash_file", lambda p: calls.append(p) or real(p))
    finder = md.DownloadFinder(len(data), hash_bytes(data), [tmp_path])
    finder.find()
    finder.find()
    assert calls == [tmp_path / "wrong.zip"]


def test_finder_ignores_missing_folders_and_unknown_hashes(tmp_path):
    assert md.DownloadFinder(5, "abc", [tmp_path / "nope"]).find() is None
    assert md.DownloadFinder(5, "", [tmp_path]).find() is None


def test_download_folders_watches_downloads_even_when_hidden_in_mosaic(tmp_path, monkeypatch):
    """Real setup: the default folder was disabled in Mosaic's Downloads
    panel, yet the browser still saves there."""
    import importlib
    from Utils.downloads import download_locations as dl
    real = importlib.reload(md).download_folders     # undo conftest's stub
    monkeypatch.setattr(dl, "get_default_downloads_dir", lambda: tmp_path)
    monkeypatch.setattr(dl, "is_default_downloads_disabled", lambda: True)
    monkeypatch.setattr(dl, "load_extra_download_locations", lambda: [str(tmp_path / "missing")])
    assert real() == [tmp_path]

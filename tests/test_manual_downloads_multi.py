"""Watching for several manual downloads at once.

The VtMB "Fashionista - Steam" list has 77 manual archives. A per-archive
watcher makes that strictly serial: a file downloaded out of order is
invisible until its turn, and one that never arrives blocks the rest.
"""
from __future__ import annotations

from pathlib import Path

from Utils.wabbajack.manual_downloads import MultiDownloadFinder
from Utils.wabbajack.wabbajack_hash import hash_file
from Utils.wabbajack.wabbajack_manifest import Archive


def _archive(tmp: Path, name: str, data: bytes) -> Archive:
    """Write *data* to a scratch file just to learn its hash."""
    probe = tmp / f".probe-{name}"
    probe.write_bytes(data)
    a = Archive(name=name, hash=hash_file(probe), size=len(data))
    probe.unlink()
    return a


def test_finds_a_file_downloaded_out_of_order(tmp_path: Path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    a1 = _archive(tmp_path, "first.7z", b"one" * 10)
    a2 = _archive(tmp_path, "second.7z", b"two" * 20)
    a3 = _archive(tmp_path, "third.7z", b"three" * 30)
    finder = MultiDownloadFinder([a1, a2, a3], [dl])

    # user grabs the third one first
    (dl / "third.7z").write_bytes(b"three" * 30)
    hits = finder.find_any()
    assert hits == {a3.hash: dl / "third.7z"}
    assert {a.hash for a in finder.pending()} == {a1.hash, a2.hash}


def test_finds_several_in_one_scan(tmp_path: Path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    a1 = _archive(tmp_path, "a.7z", b"aaa")
    a2 = _archive(tmp_path, "b.7z", b"bbbb")
    finder = MultiDownloadFinder([a1, a2], [dl])
    (dl / "a.7z").write_bytes(b"aaa")
    (dl / "b.7z").write_bytes(b"bbbb")
    assert set(finder.find_any()) == {a1.hash, a2.hash}
    assert finder.pending() == []


def test_a_missing_archive_does_not_block_the_others(tmp_path: Path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    never = _archive(tmp_path, "never.7z", b"absent")
    got = _archive(tmp_path, "got.7z", b"present")
    finder = MultiDownloadFinder([never, got], [dl])
    (dl / "got.7z").write_bytes(b"present")
    assert set(finder.find_any()) == {got.hash}
    assert [a.hash for a in finder.pending()] == [never.hash]


def test_partial_downloads_are_ignored(tmp_path: Path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    a = _archive(tmp_path, "big.7z", b"payload")
    finder = MultiDownloadFinder([a], [dl])
    (dl / "big.7z.part").write_bytes(b"payload")
    assert finder.find_any() == {}
    assert finder.pending()


def test_same_size_different_content_is_not_a_match(tmp_path: Path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    a = _archive(tmp_path, "wanted.7z", b"AAAA")
    finder = MultiDownloadFinder([a], [dl])
    (dl / "decoy.7z").write_bytes(b"BBBB")      # same size, wrong bytes
    assert finder.find_any() == {}
    assert finder.pending()


def test_file_completing_later_is_picked_up_on_a_later_scan(tmp_path: Path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    a = _archive(tmp_path, "slow.7z", b"complete-payload")
    finder = MultiDownloadFinder([a], [dl])
    assert finder.find_any() == {}             # nothing yet
    (dl / "slow.7z").write_bytes(b"complete-payload")
    assert set(finder.find_any()) == {a.hash}


def test_resolve_drops_an_archive_handled_elsewhere(tmp_path: Path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    a1 = _archive(tmp_path, "a.7z", b"aaa")
    a2 = _archive(tmp_path, "b.7z", b"bbbb")
    finder = MultiDownloadFinder([a1, a2], [dl])
    finder.resolve(a1)                          # "I downloaded it elsewhere" / skipped
    assert [x.hash for x in finder.pending()] == [a2.hash]
    (dl / "a.7z").write_bytes(b"aaa")
    assert finder.find_any() == {}              # no longer watched for


def test_each_candidate_is_hashed_once_per_change(tmp_path: Path, monkeypatch):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    a = _archive(tmp_path, "x.7z", b"stuff")
    (dl / "decoy.7z").write_bytes(b"other")     # same size, never matches
    finder = MultiDownloadFinder([a], [dl])

    import Utils.wabbajack.manual_downloads as md
    calls = []
    real = md.hash_file
    monkeypatch.setattr(md, "hash_file", lambda p, *a, **k: (calls.append(p), real(p))[1])

    finder.find_any()
    first = len(calls)
    finder.find_any()                            # unchanged -> no re-hash
    assert len(calls) == first

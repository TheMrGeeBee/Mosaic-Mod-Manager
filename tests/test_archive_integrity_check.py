"""_zip_is_intact must judge container format by content, not by filename.

Nexus serves archives whose extension does not match their container — a RAR
uploaded as ".zip". Gating the ZIP check on the extension made such a file fail
integrity forever: the cache reported it incomplete, the caller deleted it as a
partial download and re-fetched it, and the replacement was byte-identical, so
it re-downloaded on every run.
"""
from __future__ import annotations

import zipfile

from Nexus.nexus_download import _zip_is_intact

RAR5_MAGIC = b"Rar!\x1a\x07\x01\x00"
SEVENZ_MAGIC = b"7z\xbc\xaf\x27\x1c"


def test_a_real_zip_is_intact(tmp_path):
    p = tmp_path / "real.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.txt", "hello")
    assert _zip_is_intact(p) is True


def test_a_rar_named_zip_is_not_rejected(tmp_path):
    """The reported case: re-downloaded on every run before this fix."""
    p = tmp_path / "actually-a-rar.zip"
    p.write_bytes(RAR5_MAGIC + b"\x00" * 512)
    assert _zip_is_intact(p) is True


def test_a_7z_named_zip_is_not_rejected(tmp_path):
    p = tmp_path / "actually-7z.zip"
    p.write_bytes(SEVENZ_MAGIC + b"\x00" * 512)
    assert _zip_is_intact(p) is True


def test_a_truncated_zip_is_still_rejected(tmp_path):
    """Content that claims to be a ZIP but is damaged must still fail, or a
    genuinely partial download would be accepted as complete."""
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("a.txt", "hello" * 100)
    data = good.read_bytes()
    bad = tmp_path / "truncated.zip"
    bad.write_bytes(data[: len(data) // 2])
    assert _zip_is_intact(bad) is False


def test_an_empty_file_named_zip_is_rejected(tmp_path):
    p = tmp_path / "empty.zip"
    p.write_bytes(b"")
    assert _zip_is_intact(p) is False


def test_non_zip_extensions_are_skipped_as_before(tmp_path):
    for name in ("mod.rar", "mod.7z", "mod.tar.gz"):
        p = tmp_path / name
        p.write_bytes(b"whatever")
        assert _zip_is_intact(p) is True


def test_an_unreadable_path_is_not_reported_intact(tmp_path):
    assert _zip_is_intact(tmp_path / "does-not-exist.zip") is False

"""Tests for gui_qt.app.MainWindow._stamp_modio_meta -- the mod.io Quick
Update completion handler that overwrites meta.ini's confirmed file_id after
a download, instead of trusting the install pipeline's own re-identification
heuristic.

Caught live: it built a fresh ModioMeta (mod_id/file_id/version/name/
profile_url/uploader/tags/latest_file_id/latest_version/installed only),
which silently reset liked/ignore_update/ignored_version to their dataclass
defaults on every Quick Update, and stomped latest_file_id/latest_version
down to the just-installed file even when the generic install pipeline's own
resolve_modio_meta() call (which runs moments earlier, post-install, with a
fresh mod.io query) had already found a newer one -- undoing that fresher
info and making a genuinely out-of-date mod look "confirmed" up to date, or
vice versa. Fixed by re-reading meta.ini fresh at stamp time and only
overriding file_id/version/installed."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from gui_qt.app import MainWindow, _load_bg3_modio


def _write_meta_ini(path, lines: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "[General]\n" + "\n".join(f"{k} = {v}" for k, v in lines.items())
    path.write_text(body, encoding="utf-8")


def _stamp(meta_path, stale_meta, file):
    fake_self = SimpleNamespace(_append_log=lambda *_a: None)
    MainWindow._stamp_modio_meta(fake_self, meta_path, stale_meta, file)


def test_stamp_preserves_liked_and_ignore_flags(tmp_path):
    meta_path = tmp_path / "Some Mod" / "meta.ini"
    _write_meta_ini(meta_path, {
        "modioModId": "4343518",
        "modioFileId": "7430200",
        "modioVersion": "2.0.0.55",
        "modioLatestFileId": "7430300",
        "modioLatestVersion": "2.0.0.61",
        "modioLiked": "1",
        "modioIgnoreUpdate": "1",
        "modioIgnoredVersion": "2.0.0.59",
    })
    modio_meta = _load_bg3_modio("modio_meta")
    stale_meta = modio_meta.ModioMeta(mod_id=4343518, latest_file_id=7430300)
    file = SimpleNamespace(file_id=7430300, version="2.0.0.61")

    _stamp(meta_path, stale_meta, file)

    current = modio_meta.read_modio_meta(meta_path)
    assert current.file_id == 7430300
    assert current.version == "2.0.0.61"
    assert current.liked is True
    assert current.ignore_update is True
    assert current.ignored_version == "2.0.0.59"


def test_stamp_does_not_clobber_a_fresher_latest_file_id(tmp_path):
    """The generic install pipeline's own resolve_modio_meta() call runs
    AFTER the download, moments before this stamp -- if it already found an
    even newer file (7430399) than what Quick Update resolved before
    downloading (7430300), that fresher latest_file_id must survive."""
    meta_path = tmp_path / "Some Mod" / "meta.ini"
    _write_meta_ini(meta_path, {
        "modioModId": "4343518",
        "modioFileId": "7430300",       # just installed, matches `file` below
        "modioVersion": "2.0.0.61",
        "modioLatestFileId": "7430399",  # resolve_modio_meta found this newer one
        "modioLatestVersion": "2.0.0.63",
    })
    modio_meta = _load_bg3_modio("modio_meta")
    # The stale pre-download snapshot Quick Update resolved against.
    stale_meta = modio_meta.ModioMeta(mod_id=4343518, latest_file_id=7430300)
    file = SimpleNamespace(file_id=7430300, version="2.0.0.61")

    _stamp(meta_path, stale_meta, file)

    current = modio_meta.read_modio_meta(meta_path)
    assert current.file_id == 7430300
    assert current.latest_file_id == 7430399
    assert current.latest_version == "2.0.0.63"


def test_stamp_falls_back_to_installed_file_when_no_latest_known_yet(tmp_path):
    meta_path = tmp_path / "Fresh Mod" / "meta.ini"
    _write_meta_ini(meta_path, {"modioModId": "555"})
    modio_meta = _load_bg3_modio("modio_meta")
    stale_meta = modio_meta.ModioMeta(mod_id=555, latest_file_id=999)
    file = SimpleNamespace(file_id=999, version="1.0.0")

    _stamp(meta_path, stale_meta, file)

    current = modio_meta.read_modio_meta(meta_path)
    assert current.file_id == 999
    assert current.latest_file_id == 999
    assert current.latest_version == "1.0.0"

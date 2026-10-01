"""Test for Utils.mods.mod_install._try_resolve_modio -- round-tripping
liked/ignore_update/ignored_version instead of letting resolve_modio_meta's
freshly-built ModioMeta silently wipe them.

Real bug (found by ultrareview of feat/collection-export-publish): the
documented fix in gui_qt/app.py's _stamp_modio_meta only round-trips
meta.ini AFTER _install_paths finishes (see test_modio_stamp_meta.py). But
_try_resolve_modio -- called from _write_install_meta during every mod.io
install, including Quick Update and Change Version -- already overwrote
meta.ini with a brand-new default ModioMeta moments earlier. By the time
_stamp_modio_meta did its "preserve from disk" read, it was reading back
the already-wiped values, so the original Quick-Update regression (Liked
star and Ignore-Update/pinned-version silently cleared) still reproduced."""
from __future__ import annotations

from Utils.mods import mod_install


def _write_meta_ini(path, lines: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "[General]\n" + "\n".join(f"{k} = {v}" for k, v in lines.items())
    path.write_text(body, encoding="utf-8")


def test_try_resolve_modio_preserves_liked_and_ignore_flags(tmp_path, monkeypatch):
    dest_root = tmp_path / "Some Mod"
    meta_path = dest_root / "meta.ini"
    _write_meta_ini(meta_path, {
        "modioModId": "4343518",
        "modioFileId": "7430200",
        "modioVersion": "2.0.0.55",
        "modioLiked": "1",
        "modioIgnoreUpdate": "1",
        "modioIgnoredVersion": "2.0.0.59",
    })

    modio_key = mod_install._load_bg3_sibling("modio_key")
    monkeypatch.setattr(modio_key, "load_modio_key", lambda: "fake-key")

    modio_meta = mod_install._load_bg3_sibling("modio_meta")
    # What resolve_modio_meta() actually returns: a brand-new ModioMeta with
    # liked/ignore_update/ignored_version at dataclass defaults, since it has
    # no way to know about the mod's prior on-disk state.
    fresh = modio_meta.ModioMeta(
        mod_id=4343518, file_id=7430300, version="2.0.0.61",
        latest_file_id=7430300, latest_version="2.0.0.61")
    monkeypatch.setattr(modio_meta, "resolve_modio_meta", lambda *a, **k: fresh)

    mod_install._try_resolve_modio(
        meta_path, dest_root, tmp_path / "archive.zip", lambda *_a: None)

    current = modio_meta.read_modio_meta(meta_path)
    assert current.file_id == 7430300
    assert current.version == "2.0.0.61"
    assert current.liked is True
    assert current.ignore_update is True
    assert current.ignored_version == "2.0.0.59"


def test_try_resolve_modio_defaults_are_fine_on_a_brand_new_mod(tmp_path, monkeypatch):
    """No pre-existing meta.ini to round-trip from -- defaults should just
    pass through untouched, same as before this fix."""
    dest_root = tmp_path / "New Mod"
    meta_path = dest_root / "meta.ini"
    dest_root.mkdir(parents=True)

    modio_key = mod_install._load_bg3_sibling("modio_key")
    monkeypatch.setattr(modio_key, "load_modio_key", lambda: "fake-key")

    modio_meta = mod_install._load_bg3_sibling("modio_meta")
    fresh = modio_meta.ModioMeta(
        mod_id=999, file_id=111, version="1.0.0",
        latest_file_id=111, latest_version="1.0.0")
    monkeypatch.setattr(modio_meta, "resolve_modio_meta", lambda *a, **k: fresh)

    mod_install._try_resolve_modio(
        meta_path, dest_root, tmp_path / "archive.zip", lambda *_a: None)

    current = modio_meta.read_modio_meta(meta_path)
    assert current.file_id == 111
    assert current.liked is False
    assert current.ignore_update is False
    assert current.ignored_version == ""

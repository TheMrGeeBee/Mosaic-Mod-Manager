"""Resume detection and nameless-archive cache reuse.

Both come from a real re-import of the VtMB list after its profiles were
deleted: everything rebuilt from scratch even though shared staging still
held the mods, and 2 of 81 archives re-downloaded while 79 were reused.
"""
from __future__ import annotations

from pathlib import Path

import Utils.wabbajack.wabbajack_install as wi
from Utils.wabbajack.wabbajack_hash import hash_file
from Utils.wabbajack.wabbajack_manifest import (Archive, InlineFileDirective,
                                                ModList)


def _modlist(*tos: str) -> ModList:
    return ModList(directives=[InlineFileDirective(to=t, source_data_id="d") for t in tos])


# --------------------------------------------------------------------------
# prebuilt_mod_count
# --------------------------------------------------------------------------

def test_counts_only_mod_folders_that_exist(tmp_path: Path):
    ml = _modlist("mods/Alpha/a.esp", "mods/Beta/b.esp", "mods/Gamma/c.esp",
                  "profiles/P/modlist.txt")          # not a mod -> not counted
    staging = tmp_path / "mods"
    (staging / "Alpha").mkdir(parents=True)
    (staging / "Beta").mkdir()
    assert wi.prebuilt_mod_count(ml, staging) == (2, 3)


def test_counts_a_mod_once_however_many_files_it_has(tmp_path: Path):
    ml = _modlist("mods/Alpha/a.esp", "mods/Alpha/b.esp", "mods/Alpha/c.esp")
    staging = tmp_path / "mods"
    (staging / "Alpha").mkdir(parents=True)
    assert wi.prebuilt_mod_count(ml, staging) == (1, 1)


def test_missing_staging_is_zero_not_an_error(tmp_path: Path):
    ml = _modlist("mods/Alpha/a.esp")
    assert wi.prebuilt_mod_count(ml, tmp_path / "absent") == (0, 1)


def test_no_mod_directives_means_nothing_to_offer(tmp_path: Path):
    ml = _modlist("profiles/P/modlist.txt")
    staging = tmp_path / "mods"
    staging.mkdir()
    assert wi.prebuilt_mod_count(ml, staging) == (0, 0)


# --------------------------------------------------------------------------
# _cached: a nameless archive must still be reusable
# --------------------------------------------------------------------------

def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hash_file(path)


def test_nameless_archive_is_reused_from_cache(tmp_path: Path):
    """Regression: an archive with no Name downloads under its hash, but
    _cached used to require a name and so re-fetched it every run."""
    data = b"payload"
    digest = _write(tmp_path / "tmp", data)
    stored = tmp_path / wi._download_name(Archive(name="", hash=digest))
    _write(stored, data)
    archive = Archive(name="", hash=digest, size=len(data))
    assert wi._cached(archive, tmp_path) == stored


def test_named_archive_still_reused(tmp_path: Path):
    data = b"payload"
    digest = _write(tmp_path / "Mod.7z", data)
    archive = Archive(name="Mod.7z", hash=digest, size=len(data))
    assert wi._cached(archive, tmp_path) == tmp_path / "Mod.7z"


def test_wrong_hash_is_not_a_cache_hit(tmp_path: Path):
    data = b"payload"
    _write(tmp_path / "Mod.7z", data)
    archive = Archive(name="Mod.7z", hash="not-the-hash", size=len(data))
    assert wi._cached(archive, tmp_path) is None


def test_short_file_is_not_a_cache_hit(tmp_path: Path):
    digest = _write(tmp_path / "Mod.7z", b"partial")
    archive = Archive(name="Mod.7z", hash=digest, size=999)
    assert wi._cached(archive, tmp_path) is None

"""Modhold update checking: counterpart matching, persistence, the modlist flag."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
from pathlib import Path

import pytest

from Modhold.modhold_api import ModholdError, ModholdNotFound
from Modhold.modhold_meta import (
    ModholdMeta,
    read_modhold_meta,
    read_modhold_meta_fast,
    update_pending,
    write_modhold_meta,
)
from Modhold.modhold_update_checker import (
    check_for_updates,
    find_counterpart,
    has_modhold_mods,
    is_update,
)

SHA_A, SHA_B = "aa" * 32, "bb" * 32


def _file(fid, kind="main", label="Main file", version="1.0", sha=SHA_A, name=None):
    return {"id": fid, "kind": kind, "label": label, "version": version,
            "size": 10, "sha256": sha, "name": name or f"f{fid}.zip"}


class _Api:
    def __init__(self, details=None, errors=None):
        self.details = details or {}
        self.errors = errors or {}
        self.calls = []
        self._lock = threading.Lock()

    def get_mod(self, mod_id):
        with self._lock:
            self.calls.append(mod_id)
        if mod_id in self.errors:
            raise self.errors[mod_id]
        return self.details[mod_id]


def _detail(mod_id, files, version="2.0"):
    return {"id": mod_id, "version": version, "files": files, "can_download": True}


def _install(staging: Path, name, **kw):
    base = dict(mod_id=12, file_id=100, version="1.0", sha256=SHA_A, file_label="Main file",
                file_kind="main", game="skyrim-se", name=name)
    base.update(kw)
    write_modhold_meta(staging / name / "meta.ini", ModholdMeta(**base))
    return staging / name / "meta.ini"


@pytest.fixture
def staging(tmp_path):
    s = tmp_path / "mods"
    s.mkdir()
    return s


def test_up_to_date_mod_is_not_reported(staging):
    _install(staging, "Shop")
    api = _Api({12: _detail(12, [_file(100)])})
    assert check_for_updates(staging, api) == []


def test_newer_file_is_reported_and_persisted(staging):
    meta_path = _install(staging, "Shop")
    api = _Api({12: _detail(12, [_file(150, version="1.1", sha="cc" * 32)])})
    out = check_for_updates(staging, api)
    assert len(out) == 1
    info = out[0]
    assert (info.mod_name, info.has_update, info.latest_file_id, info.latest_version) == (
        "Shop", True, 150, "1.1")
    saved = read_modhold_meta(meta_path)
    assert (saved.latest_file_id, saved.latest_version) == (150, "1.1")
    assert (saved.file_id, saved.version) == (100, "1.0")      # installed state untouched
    assert update_pending(saved)


def test_file_replaced_in_place_with_another_checksum_is_an_update(staging):
    meta_path = _install(staging, "Shop")
    api = _Api({12: _detail(12, [_file(100, sha=SHA_B)])})
    assert [i.has_update for i in check_for_updates(staging, api)] == [True]
    assert update_pending(read_modhold_meta(meta_path))


def test_a_rollback_is_not_an_update_and_never_flags(staging):
    meta_path = _install(staging, "Shop", file_id=200, version="2.0", sha256=SHA_B)
    api = _Api({12: _detail(12, [_file(150, version="1.5", sha=SHA_A)])})
    assert check_for_updates(staging, api) == []
    saved = read_modhold_meta(meta_path)
    assert saved.latest_file_id == 200 and saved.latest_sha256 == SHA_B
    assert not update_pending(saved)


def test_counterpart_found_by_label_when_the_id_was_replaced(staging):
    _install(staging, "Shop")
    files = [_file(300, kind="optional", label="Add-on"),
             _file(310, label="Main file", version="1.2")]
    assert find_counterpart(read_modhold_meta(staging / "Shop" / "meta.ini"),
                            files)["id"] == 310
    out = check_for_updates(staging, _Api({12: _detail(12, files)}))
    assert [(i.latest_file_id, i.has_update) for i in out] == [(310, True)]


def test_counterpart_found_by_kind_when_there_is_one_main_file(staging):
    meta = ModholdMeta(mod_id=1, file_id=5, file_kind="main", file_label="Old label")
    files = [_file(9, label="New label"), _file(8, kind="optional", label="Extra")]
    assert find_counterpart(meta, files)["id"] == 9
    # two mains: ambiguous, so no guess
    assert find_counterpart(meta, [_file(9), _file(10)]) is None


def test_installed_file_gone_with_no_counterpart_is_unknown(staging):
    _install(staging, "Shop", file_label="", file_kind="")
    out = check_for_updates(staging, _Api({12: _detail(12, [_file(900, label="X")], version="3.0")}))
    assert len(out) == 1 and out[0].unknown and not out[0].has_update
    assert out[0].latest_version == "3.0"


def test_ignored_version_is_not_reported_but_is_remembered(staging):
    meta_path = _install(staging, "Shop", ignore_update=True, ignored_version="1.1")
    api = _Api({12: _detail(12, [_file(150, version="1.1")])})
    assert check_for_updates(staging, api) == []
    saved = read_modhold_meta(meta_path)
    assert saved.latest_file_id == 150 and not update_pending(saved)
    # a newer one than the ignored version flags again
    api2 = _Api({12: _detail(12, [_file(160, version="1.2")])})
    assert len(check_for_updates(staging, api2)) == 1


def test_one_failing_mod_does_not_stop_the_others(staging):
    _install(staging, "Gone", mod_id=1, file_id=10)
    _install(staging, "Broken", mod_id=2, file_id=20)
    _install(staging, "Fine", mod_id=3, file_id=30)
    api = _Api({3: _detail(3, [_file(31, version="9")])},
               errors={1: ModholdNotFound("x", 404), 2: ModholdError("boom", 500)})
    logs = []
    out = check_for_updates(staging, api, progress_cb=logs.append)
    assert [i.mod_name for i in out] == ["Fine"]
    assert any("no longer on Modhold" in m for m in logs)
    assert any("boom" in m for m in logs)


def test_each_mod_is_read_once_even_with_several_folders(staging):
    _install(staging, "Shop main", file_id=100)
    _install(staging, "Shop addon", file_id=101, file_label="Add-on", file_kind="optional")
    api = _Api({12: _detail(12, [_file(150), _file(160, kind="optional", label="Add-on")])})
    out = check_for_updates(staging, api)
    assert api.calls == [12]
    assert {i.mod_name for i in out} == {"Shop main", "Shop addon"}


def test_only_names_limits_the_check_and_non_modhold_folders_are_ignored(staging):
    _install(staging, "A", mod_id=1, file_id=1)
    _install(staging, "B", mod_id=2, file_id=2)
    (staging / "Plain").mkdir()
    (staging / "Plain" / "meta.ini").write_text("[General]\nmodid=5\n")
    (staging / "NoMeta").mkdir()
    api = _Api({1: _detail(1, [_file(9)]), 2: _detail(2, [_file(9)])})
    out = check_for_updates(staging, api, only_names={"A"})
    assert api.calls == [1] and [i.mod_name for i in out] == ["A"]


def test_no_modhold_mods_makes_no_calls(staging):
    (staging / "Plain").mkdir()
    (staging / "Plain" / "meta.ini").write_text("[General]\nmodid=5\n")
    api = _Api()
    assert check_for_updates(staging, api) == [] and api.calls == []


def test_has_modhold_mods_is_a_cheap_text_check(staging):
    assert not has_modhold_mods(staging)
    (staging / "Plain").mkdir()
    (staging / "Plain" / "meta.ini").write_text("[General]\nmodid=5\n")
    assert not has_modhold_mods(staging)
    (staging / "Zero").mkdir()
    (staging / "Zero" / "meta.ini").write_text("[General]\nmodholdModId = 0\n")
    assert not has_modhold_mods(staging)
    _install(staging, "Shop")
    assert has_modhold_mods(staging)
    assert not has_modhold_mods(staging / "missing")


@pytest.mark.parametrize("installed,counter,expected", [
    (dict(file_id=100), {"id": 101}, True),
    (dict(file_id=100), {"id": 99}, False),
    (dict(file_id=100, sha256=SHA_A), {"id": 100, "sha256": SHA_A}, False),
    (dict(file_id=100, sha256=SHA_A), {"id": 100, "sha256": SHA_B}, True),
    (dict(file_id=100, sha256=""), {"id": 100, "sha256": SHA_B}, False),
])
def test_is_update(installed, counter, expected):
    assert is_update(ModholdMeta(mod_id=1, **installed), counter) is expected


def test_update_pending_needs_ids_and_respects_ignore():
    assert not update_pending(ModholdMeta())
    assert not update_pending(ModholdMeta(mod_id=1, file_id=0, latest_file_id=5))
    assert update_pending(ModholdMeta(mod_id=1, file_id=1, latest_file_id=5))
    assert not update_pending(ModholdMeta(mod_id=1, file_id=1, latest_file_id=5,
                                          latest_version="2", ignore_update=True,
                                          ignored_version="2"))


def test_fast_reader_skips_non_modhold_and_reads_modhold(staging):
    (staging / "Plain").mkdir()
    (staging / "Plain" / "meta.ini").write_text("[General]\nmodid=5\n")
    assert read_modhold_meta_fast(staging / "Plain" / "meta.ini") is None
    assert read_modhold_meta_fast(staging / "nope" / "meta.ini") is None
    p = _install(staging, "Shop")
    assert read_modhold_meta_fast(p).mod_id == 12


def test_modlist_paints_the_modhold_update_flag_for_any_game(staging):
    """A Modhold update shows in the real flag path, with the version and author
    filled in for a mod that has no Nexus data."""
    from gui_qt.modlist.modlist_data import FLAG_MODHOLD_UPDATE, read_meta_for_entries
    from Utils.mods.modlist import ModEntry

    _install(staging, "Shop", uploader="Seth", latest_file_id=150, latest_version="1.1")
    _install(staging, "Current", mod_id=7, file_id=5, latest_file_id=5)
    (staging / "Plain").mkdir()
    (staging / "Plain" / "meta.ini").write_text("[General]\nmodid=5\nversion=3\n")
    entries = [ModEntry(n, True, False) for n in ("Shop", "Current", "Plain")]
    versions, _inst, flags, _cat, updates, *_rest, authors = read_meta_for_entries(
        entries, staging)
    assert flags["Shop"] & FLAG_MODHOLD_UPDATE
    assert "Shop" in updates
    assert not flags.get("Current", 0) & FLAG_MODHOLD_UPDATE
    assert not flags.get("Plain", 0) & FLAG_MODHOLD_UPDATE
    assert versions["Shop"] == "1.0" and authors["Shop"] == "Seth"

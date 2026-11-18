"""The end-of-install "N mods did not install" audit (find_missing_mods)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Utils.collections.collection_install import (  # noqa: E402
    build_install_report,
    find_missing_mods,
)


def _mod(name, fid, mod_id=1):
    return SimpleNamespace(mod_name=name, file_id=fid, mod_id=mod_id)


def _staged(tmp_path, *folders):
    for f in folders:
        (tmp_path / f).mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_a_mod_installed_in_this_run_is_not_missing(tmp_path):
    st = _staged(tmp_path, "Alpha")
    assert find_missing_mods([_mod("Alpha", 10)], {10: "Alpha"}, {10: {"status": "installed"}}, st) == []


def test_a_mod_an_earlier_run_installed_is_not_reported_as_failed(tmp_path):
    """The bug: after a resumed install the 'existing' mods were all flagged missing."""
    st = _staged(tmp_path, "Old Mod")
    outcomes = {10: {"name": "Old Mod", "status": "existing", "detail": "Old Mod"}}
    assert find_missing_mods([_mod("Old Mod", 10)], {}, outcomes, st) == []


def test_an_existing_mod_whose_folder_is_gone_is_still_reported(tmp_path):
    outcomes = {10: {"name": "Gone", "status": "existing", "detail": "Gone"}}
    got = find_missing_mods([_mod("Gone", 10)], {}, outcomes, tmp_path)
    assert [(g[0], g[3]) for g in got] == [("Gone", "existing")]


def test_a_deferred_mod_that_staged_nothing_is_reported_with_its_reason(tmp_path):
    outcomes = {10: {"status": "deferred", "detail": "fomod"}}
    got = find_missing_mods([_mod("Kezyma's UI", 10, 48987)], {}, outcomes, tmp_path)
    assert got == [("Kezyma's UI", 48987, 10, "deferred", "fomod")]


def test_a_mod_with_no_recorded_outcome_is_unknown_and_a_manual_skip_is_not_missing(tmp_path):
    mods = [_mod("A", 1), _mod("B", 2), _mod("NoFile", 0)]
    got = find_missing_mods(mods, {}, {2: {"status": "skipped_manual"}}, tmp_path)
    assert [(g[0], g[3]) for g in got] == [("A", "unknown")]


def test_no_staging_folder_means_everything_is_missing(tmp_path):
    got = find_missing_mods([_mod("A", 1)], {1: "A"}, {}, None)
    assert [g[0] for g in got] == ["A"]


def test_the_report_counts_only_the_real_misses(tmp_path):
    st = _staged(tmp_path, "Old", "New")
    mods = [_mod("Old", 1), _mod("New", 2), _mod("Broken", 3)]
    outcomes = {1: {"status": "existing", "detail": "Old"},
                2: {"status": "installed"}, 3: {"status": "deferred", "detail": "fomod"}}
    missing = find_missing_mods(mods, {2: "New"}, outcomes, st)
    report = build_install_report(missing, total=3)
    assert report["failed_count"] == 1 and report["failed"][0]["name"] == "Broken"

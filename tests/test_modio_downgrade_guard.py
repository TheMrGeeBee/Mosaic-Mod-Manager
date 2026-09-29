"""Tests for the mod.io "update" comparisons in modio_update_checker.py and
modio_quick_update.py (Baldur's Gate 3).

Caught live: mod.io's per-mod "live modfile" (what get_mods_latest_batch /
get_mod_detail report as the current release) can have a LOWER file id than
what's actually installed -- e.g. the author uploaded a newer file but never
promoted it to live, or rolled the live release back. The comparisons here
used bare "!=" / "==" against file_id, so a live-modfile id below the
installed one was treated as "an update" and silently downgraded the mod
under the "Update" banner. Fixed to only ever move forward (">" / "<=").
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_bg3(stem: str):
    # Same cache key ("<stem>_bg3") every BG3 sibling loader uses -- so
    # monkeypatching a class on the module this returns actually reaches the
    # instance modio_update_checker.py/modio_quick_update.py load internally
    # via their own _load_sibling(), not an unrelated separate copy.
    mod_name = f"{stem}_bg3"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    bg3_dir = (Path(__file__).resolve().parent.parent / "src" / "Games"
              / "Baldur's Gate 3")
    spec = importlib.util.spec_from_file_location(mod_name, str(bg3_dir / f"{stem}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _write_meta_ini(path: Path, lines: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "[General]\n" + "\n".join(f"{k} = {v}" for k, v in lines.items())
    path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# modio_update_checker.check_for_updates
# ---------------------------------------------------------------------------

class _FakeModioAPI:
    def __init__(self, summaries: dict):
        self._summaries = summaries

    def get_mods_latest_batch(self, mod_ids):
        return {mid: s for mid, s in self._summaries.items() if mid in mod_ids}


def _run_check(tmp_path, mod_id, installed_file_id, live_file_id,
              installed_version="1.0", live_version="1.1", monkeypatch=None):
    checker = _load_bg3("modio_update_checker")
    modio_api = _load_bg3("modio_api")
    staging = tmp_path / "staging"
    _write_meta_ini(staging / "Some Mod" / "meta.ini", {
        "modioModId": str(mod_id),
        "modioFileId": str(installed_file_id),
        "modioVersion": installed_version,
        "modioUploader": "someone",
        "modioTags": "UI",
    })
    summary = modio_api.ModioModSummary(
        mod_id=mod_id, name="Some Mod", profile_url="https://mod.io/x",
        latest_file_id=live_file_id, latest_version=live_version)
    monkeypatch.setattr(
        modio_api, "ModioAPI", lambda api_key: _FakeModioAPI({mod_id: summary}))
    return checker.check_for_updates(staging, api_key="fake-key")


def test_older_live_release_is_not_flagged_as_update(tmp_path, monkeypatch):
    """The exact bug: installed file_id 300, mod.io's live modfile is 250
    (older) -- must not be reported as an available update."""
    results = _run_check(tmp_path, mod_id=111, installed_file_id=300,
                         live_file_id=250, monkeypatch=monkeypatch)
    assert results == []


def test_genuinely_newer_live_release_is_still_flagged(tmp_path, monkeypatch):
    """Regression guard: the fix must not break real forward updates."""
    results = _run_check(tmp_path, mod_id=111, installed_file_id=300,
                         live_file_id=400, monkeypatch=monkeypatch)
    assert len(results) == 1
    assert results[0].has_update is True
    assert results[0].latest_file_id == 400


def test_equal_file_id_is_not_flagged(tmp_path, monkeypatch):
    results = _run_check(tmp_path, mod_id=111, installed_file_id=300,
                         live_file_id=300, monkeypatch=monkeypatch)
    assert results == []


# ---------------------------------------------------------------------------
# modio_quick_update.resolve_modio_quick_update_target
# ---------------------------------------------------------------------------

def test_quick_update_skips_when_live_release_is_older_than_installed(tmp_path):
    quick_update = _load_bg3("modio_quick_update")
    modio_meta = _load_bg3("modio_meta")
    staging = tmp_path / "staging"
    meta_path = staging / "Some Mod" / "meta.ini"
    _write_meta_ini(meta_path, {
        "modioModId": "111",
        "modioFileId": "300",
        "modioLatestFileId": "250",  # older than installed
    })

    outcome, payload = quick_update.resolve_modio_quick_update_target(
        api=SimpleNamespace(), staging_root=staging, mod_name="Some Mod")

    assert outcome == "skipped"
    assert payload == "already up to date"


def test_quick_update_still_queues_a_genuine_forward_update(tmp_path):
    quick_update = _load_bg3("modio_quick_update")
    staging = tmp_path / "staging"
    _write_meta_ini(staging / "Some Mod" / "meta.ini", {
        "modioModId": "111",
        "modioFileId": "300",
        "modioLatestFileId": "400",
    })
    fake_file = SimpleNamespace(file_id=400, version="1.1", binary_url="https://x/dl")
    fake_api = SimpleNamespace(get_file=lambda mod_id, file_id: fake_file)

    outcome, payload = quick_update.resolve_modio_quick_update_target(
        api=fake_api, staging_root=staging, mod_name="Some Mod")

    assert outcome == "queued"
    _mod_name, _meta, file = payload
    assert file.file_id == 400

"""Script Merger's wizard used to pin a single Nexus file_id and skip the
download step entirely whenever WitcherScriptMerger.exe already existed --
so a user who installed it once never got asked about a newer file, even
when the author shipped a compatibility-relevant update (The Witcher 3's
2026-09-29 "Remastered" update needed a new Script Merger the next day;
the old pinned file_id 59566 is now Nexus's own OLD_VERSION, current Main
is 74645). Fixed by stamping a small marker file with the file_id an
install actually came from, and asking (not silently reusing) whenever an
existing install's marker doesn't match the wizard's currently pinned id."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import wizards_qt.script_merger_view as m  # noqa: E402
from Utils.modding_tools.xedit_tools import applications_dir  # noqa: E402


class FakeGame:
    name = "The Witcher 3"

    def __init__(self, tmp_path: Path):
        self._staging = tmp_path / "profile" / "mods"
        self._staging.mkdir(parents=True)

    def get_mod_staging_path(self):
        return self._staging

    def get_game_path(self):
        return None


class Ctx:
    def refresh_modlist(self):
        pass


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _make(tmp_path, app):
    game = FakeGame(tmp_path)
    view = m.ScriptMergerView(game, log_fn=lambda _m: None, on_close=lambda: None, ctx=Ctx())
    return view, game


def _install_exe(game):
    d = applications_dir(game, m._MERGER_DIR)
    d.mkdir(parents=True, exist_ok=True)
    (d / m._MERGER_EXE).write_text("stub")
    return d


def test_no_marker_reads_as_not_installed(tmp_path, app):
    view, game = _make(tmp_path, app)
    assert view._installed_file_id() == 0


def test_write_then_read_round_trips(tmp_path, app):
    view, game = _make(tmp_path, app)
    _install_exe(game)
    view._write_installed_file_id()
    assert view._installed_file_id() == m._NEXUS_FILE_ID


def test_current_install_skips_straight_to_proton(tmp_path, app):
    view, game = _make(tmp_path, app)
    _install_exe(game)
    view._write_installed_file_id()
    view._advance_from_deploy()
    assert view._stack.currentIndex() == m._PG_PROTON


def test_stale_install_with_no_marker_prompts_instead_of_skipping(tmp_path, app):
    view, game = _make(tmp_path, app)
    _install_exe(game)  # no marker written -- pre-tracking install
    view._advance_from_deploy()
    # Must NOT silently jump to Proton -- the confirm overlay should be
    # blocking that, so the wizard is still sitting on the deploy page.
    assert view._stack.currentIndex() == m._PG_DEPLOY


def test_stale_install_with_mismatched_marker_prompts(tmp_path, app):
    view, game = _make(tmp_path, app)
    d = _install_exe(game)
    (d / m._VERSION_MARKER).write_text("59566", encoding="utf-8")
    assert view._installed_file_id() == 59566
    view._advance_from_deploy()
    assert view._stack.currentIndex() == m._PG_DEPLOY


def test_update_choice_keep_current_goes_to_proton(tmp_path, app):
    view, game = _make(tmp_path, app)
    _install_exe(game)
    view._on_update_choice(True)
    assert view._stack.currentIndex() == m._PG_PROTON


def test_update_choice_update_now_goes_to_download(tmp_path, app):
    view, game = _make(tmp_path, app)
    _install_exe(game)
    view._on_update_choice(False)
    assert view._stack.currentIndex() == m._PG_DOWNLOAD


def test_fresh_install_with_no_exe_goes_to_download(tmp_path, app):
    view, game = _make(tmp_path, app)
    view._advance_from_deploy()
    assert view._stack.currentIndex() == m._PG_DOWNLOAD

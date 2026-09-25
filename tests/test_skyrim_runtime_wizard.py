"""The Skyrim Runtime wizard: shows the installed runtime and reverts a Mosaic
switch. Instantiated for real (headless) with a fake game folder."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pe_builder import build_pe  # noqa: E402
from test_skyrim_runtime import DST, NEW, ORIG, SRC, StubHpatchz, write_swap  # noqa: E402
from Utils.modding_tools import skyrim_runtime as sr  # noqa: E402


@dataclass
class FakeGame:
    name: str
    root: Path
    steam_id: str = "489830"
    logs: list = field(default_factory=list)

    def get_game_path(self):
        return self.root


@dataclass
class Ctx:
    restores: list = field(default_factory=list)

    def run_restore(self, on_done):
        self.restores.append(on_done)
        return True


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch, app):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("Utils.exe_launch.exe_launch._wine_process_alive", lambda _n: False)
    root = tmp_path / "Skyrim Special Edition"
    for rel, data in ORIG.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    game = FakeGame("Skyrim Special Edition", root)
    from Utils.config_paths import get_game_config_dir
    return game, get_game_config_dir(game.name)


def _view(game, ctx=None):
    from wizards_qt.skyrim_runtime_view import SkyrimRuntimeView
    logs = []
    v = SkyrimRuntimeView(game, log_fn=logs.append, on_close=lambda: None, ctx=ctx)
    v.logs = logs
    return v


def _swap(game, state_dir, tmp_path):
    tr = sr.load_transition(write_swap(tmp_path / "mod" / "RuntimeSwap"))
    sr.apply_transition(game.root, tr, state_dir, hpatchz="x", run=StubHpatchz())


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_fresh_install_says_nothing_to_revert(env):
    game, _ = env
    v = _view(game)
    assert "1.7.104.0" in v._check_label.text() and "nothing to revert" in v._check_label.text()
    assert not v._revert_btn.isVisibleTo(v) and not v._restore_btn.isVisibleTo(v)
    v.close()


def test_after_a_switch_it_offers_the_revert_and_undoes_it(env, tmp_path):
    game, state_dir = env
    _swap(game, state_dir, tmp_path)
    v = _view(game)
    assert "1.6.1170.0" in v._check_label.text() and "switched by Mosaic from 1.7.104.0" in v._check_label.text()
    assert v._revert_btn.isVisibleTo(v) and v._revert_btn.isEnabled()

    v._on_revert_clicked()
    assert _wait(lambda: sr.read_runtime_version(game.root) == SRC), v.logs
    assert _wait(lambda: v._done_btn.isEnabled())
    assert {rel: (game.root / rel).read_bytes() for rel in ORIG} == ORIG
    assert sr.load_state(state_dir) is None
    threading.Event().wait(0.05)
    v.close()


def test_deployed_game_offers_restore_and_blocks_the_revert(env, tmp_path):
    game, state_dir = env
    _swap(game, state_dir, tmp_path)
    (game.root / "Data" / ".mm_deployed").write_bytes(b"")
    ctx = Ctx()
    v = _view(game, ctx)
    assert v._restore_btn.isVisibleTo(v) and not v._revert_btn.isEnabled()
    assert "restore the game first" in v._check_label.text().lower()

    v._on_restore_clicked()
    assert len(ctx.restores) == 1
    (game.root / "Data" / ".mm_deployed").unlink()           # what the real Restore does
    ctx.restores[0](True)
    assert v._revert_btn.isEnabled()                         # Re-check ran after the restore
    v.close()


def test_a_failed_restore_says_so(env, tmp_path):
    game, state_dir = env
    _swap(game, state_dir, tmp_path)
    (game.root / "Data" / ".mm_deployed").write_bytes(b"")
    ctx = Ctx()
    v = _view(game, ctx)
    v._on_restore_clicked()
    ctx.restores[0](False)
    assert "Restore failed" in v._check_status.text()
    v.close()


def test_a_tampered_backup_is_refused_with_a_steam_verify_hint(env, tmp_path):
    game, state_dir = env
    _swap(game, state_dir, tmp_path)
    (sr.backup_dir(state_dir) / "Data" / "Skyrim.esm").write_bytes(b"tampered")
    v = _view(game)
    v._on_revert_clicked()
    assert _wait(lambda: "Verify integrity" in v._run_status.text()), v._run_status.text()
    assert sr.read_runtime_version(game.root) == DST         # nothing was restored
    v.close()


def test_the_wizard_is_registered_for_skyrim(app):
    from Games.Bethesda.skyrim_se import SkyrimSE
    from Utils.wizard_support.wizard_catalog import infer_category
    from wizards_qt import get_spec
    path = "wizards.skyrim_runtime.SkyrimRuntimeWizard"
    assert get_spec(path) is not None                       # a Qt view is registered for it
    tools = {t.id: t for t in SkyrimSE().wizard_tools}
    assert tools["downgrade_skyrimse"].dialog_class_path == path
    assert infer_category(tools["downgrade_skyrimse"]) == "Setup & Installers"

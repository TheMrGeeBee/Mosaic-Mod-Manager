"""The Skyrim Runtime wizard: shows the installed runtime and reverts a Mosaic
switch. Instantiated for real (headless) with a fake game folder."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
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

    def get_prefix_path(self):
        return None


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


def _swap_zip(tmp_path, name="SRS - BoAW 189855 v1.3.2.zip") -> Path:
    """A runtime-swap mod's archive, shaped like the ones on Nexus: the
    RuntimeSwap folder inside a wrapper directory."""
    folder = tmp_path / "archive" / "SRS" / "RuntimeSwap"
    write_swap(folder)
    out = tmp_path / "dl" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.make_archive(str(out.with_suffix("")), "zip", str(tmp_path / "archive"))
    made = out.with_suffix(".zip")
    if made != out:
        made.rename(out)
    return out


@pytest.fixture
def stub_patcher(monkeypatch):
    """hpatchz, without needing it installed. The view calls apply_transition
    itself and that takes its runner as a default argument, bound at import —
    so the stub goes in by wrapping the call, which keeps the real patching,
    hashing and rollback logic under test."""
    stub = StubHpatchz()
    real = sr.apply_transition
    monkeypatch.setattr(sr, "ensure_hpatchz", lambda **_kw: "hpatchz")
    monkeypatch.setattr(sr, "apply_transition",
                        lambda *a, **kw: real(*a, **{**kw, "run": stub}))
    return stub


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


# ---- applying a swap (the Wabbajack route: no collection names the mod) --------

def test_a_fresh_install_offers_the_switch(env):
    game, _ = env
    v = _view(game)
    assert v._apply_btn.isVisibleTo(v)
    assert "point Mosaic at a runtime-swap mod" in v._check_label.text()
    v.close()


def test_the_switch_is_hidden_while_one_is_already_applied(env, tmp_path):
    game, state_dir = env
    _swap(game, state_dir, tmp_path)
    v = _view(game)
    assert not v._apply_btn.isVisibleTo(v)      # revert first; don't stack them
    assert v._revert_btn.isVisibleTo(v)
    v.close()


def test_a_located_archive_is_read_and_applied(env, tmp_path, stub_patcher):
    game, state_dir = env
    v = _view(game, Ctx())
    try:
        v._on_swap_archive(_swap_zip(tmp_path))
        assert _wait(lambda: sr.read_runtime_version(game.root) == DST), v._run_status.text()
        assert _wait(lambda: v._done_btn.isEnabled())
        assert "Skyrim is now 1.6.1170.0" in v._run_status.text()
        assert "Don't verify the game's files in Steam" in v._run_status.text()
        # Every patched file landed, and the swap is recorded so it can be undone.
        assert {rel: (game.root / rel).read_bytes() for rel in NEW} == NEW
        assert (sr.load_state(state_dir) or {}).get("applied")
        assert v._prepared is None                      # the temp extract is gone
    finally:
        v.close()


def test_a_deployed_game_is_restored_first_and_then_switched(env, tmp_path, stub_patcher):
    game, state_dir = env
    marker = game.root / "Data" / ".mm_deployed"
    marker.write_bytes(b"")
    ctx = Ctx()
    v = _view(game, ctx)
    try:
        v._on_swap_archive(_swap_zip(tmp_path))
        assert _wait(lambda: bool(ctx.restores)), v._run_status.text()
        assert "Restoring the game to vanilla first" in v._run_status.text()
        marker.unlink()                                 # what the real Restore does
        ctx.restores[0](True)
        assert _wait(lambda: sr.read_runtime_version(game.root) == DST), v._run_status.text()
        assert _wait(lambda: v._done_btn.isEnabled())
    finally:
        v.close()


def test_a_failed_restore_patches_nothing(env, tmp_path, stub_patcher):
    game, state_dir = env
    (game.root / "Data" / ".mm_deployed").write_bytes(b"")
    ctx = Ctx()
    v = _view(game, ctx)
    try:
        v._on_swap_archive(_swap_zip(tmp_path))
        assert _wait(lambda: bool(ctx.restores))
        ctx.restores[0](False)
        assert "Restore failed" in v._run_status.text()
        assert sr.read_runtime_version(game.root) == SRC
        assert v._prepared is None
        assert v._done_btn.isEnabled()
    finally:
        v.close()


def test_a_deployed_game_with_no_restore_hook_says_where_to_restore(env, tmp_path, stub_patcher):
    game, _ = env
    (game.root / "Data" / ".mm_deployed").write_bytes(b"")
    v = _view(game, None)                               # no ctx: no run_restore
    try:
        v._on_swap_archive(_swap_zip(tmp_path))
        assert _wait(lambda: "Restore in the main window" in v._run_status.text()), \
            v._run_status.text()
        assert sr.read_runtime_version(game.root) == SRC
    finally:
        v.close()


def test_an_archive_that_is_not_a_runtime_swap_is_reported(env, tmp_path, stub_patcher):
    game, _ = env
    other = tmp_path / "dl" / "Some Other Mod 189855.zip"
    other.parent.mkdir(parents=True, exist_ok=True)
    plain = tmp_path / "plain"
    (plain / "Data").mkdir(parents=True)
    (plain / "Data" / "mod.esp").write_bytes(b"x")
    shutil.make_archive(str(other.with_suffix("")), "zip", str(plain))
    v = _view(game, Ctx())
    try:
        v._on_swap_archive(other)
        assert _wait(lambda: "isn't a runtime swap mod" in v._run_status.text()), \
            v._run_status.text()
        assert sr.read_runtime_version(game.root) == SRC
        assert v._done_btn.isEnabled()
    finally:
        v.close()


def test_a_swap_for_another_version_is_refused_by_the_hash_check(env, tmp_path, stub_patcher):
    """The archive's own manifest decides whether it fits, not its file name."""
    game, _ = env
    (game.root / "SkyrimSE.exe").write_bytes(ORIG["SkyrimSE.exe"] + b"-modified")
    v = _view(game, Ctx())
    try:
        v._on_swap_archive(_swap_zip(tmp_path))
        assert _wait(lambda: "not the original" in v._run_status.text()), v._run_status.text()
        assert "Verify integrity of game files" in v._run_status.text()
    finally:
        v.close()


def test_the_locate_page_also_scans_mosaics_download_cache(env, monkeypatch):
    game, _ = env
    from Utils import config_paths
    cache = Path("/tmp/does-not-matter")
    monkeypatch.setattr(config_paths, "list_all_cache_dirs", lambda name=None: [cache])
    v = _view(game)
    try:
        v._on_apply_clicked()
        v._goto_locate()
        assert v._locate_extra_dirs == [cache]
        assert v._locate_keywords == ["189855"]
    finally:
        v.close()

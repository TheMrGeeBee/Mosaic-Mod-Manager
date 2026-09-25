"""End-to-end: the app's collection preflight, driven through a real Qt event loop
with real worker threads, the real runtime-swap engine and a stub ``hpatchz``.

Only the app's preflight methods are bound onto a tiny harness window (booting the
whole main window would need a game, a profile and a Nexus login). The scenarios
are the ones that matter for "one click": the game is already right (no dialog),
needs the swap (dialog -> Fix -> patched -> continues), is deployed (Restore first),
can't be fixed (stays put, install not started), or the user cancels.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import stat
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from pe_builder import build_pe  # noqa: E402
from test_skyrim_runtime import NEW, ORIG, write_swap  # noqa: E402
from gui_qt.app import MainWindow  # noqa: E402
from gui_qt.collections.collection_preflight_overlay import PreflightOverlay  # noqa: E402
from Utils.modding_tools import skyrim_runtime as sr  # noqa: E402

SRS_MOD_ID, SRS_FILE_ID = 189855, 796868

_STUB = '''#!/usr/bin/env python3
import hashlib, sys
_f, old, patch, out = sys.argv[1:5]
_magic, expected, payload = open(patch, "rb").read().split(b"\\n", 2)
if hashlib.sha256(open(old, "rb").read()).hexdigest().encode() != expected:
    sys.stderr.write("oldData checksum mismatch\\n"); sys.exit(1)
open(out, "wb").write(payload)
'''


@dataclass
class Mod:
    mod_id: int = 0
    file_id: int = 0
    mod_name: str = ""
    file_name: str = ""
    size_bytes: int = 0
    md5: str = ""
    optional: bool = False


@dataclass
class FakeGame:
    name: str
    root: Path
    steam_id: str = "489830"

    def get_game_path(self):
        return self.root

    def get_prefix_path(self):
        return None

    def get_effective_mod_staging_path(self):
        return self.root.parent / "staging"


class Harness(QMainWindow):
    """Just enough of MainWindow for the preflight methods."""
    _preflight_ev = Signal(str, object)
    _op_log = Signal(str)

    def __init__(self):
        super().__init__()
        self.resize(900, 700)
        self.show()
        self.logs: list[str] = []
        self.notes: list[str] = []
        self._col_install_running = True
        self.restore_calls = 0
        self.restore_ok = True
        self._preflight_ev.connect(self._on_preflight_ev)
        self._op_log.connect(self.logs.append)

    def _append_log(self, msg):
        self.logs.append(msg)

    def _notify(self, text, state="info", sticky=False):
        self.notes.append(text)

    def _wizard_run_restore(self, on_done):
        self.restore_calls += 1
        if self.restore_ok:
            marker = self.game.root / "Data" / ".mm_deployed"
            marker.unlink(missing_ok=True)
        on_done(self.restore_ok)
        return True


for _name in ("_collection_preflight", "_preflight_dirs", "_preflight_gather",
              "_preflight_check_worker", "_on_preflight_ev", "_preflight_finish",
              "_preflight_fix", "_preflight_prepare_worker", "_preflight_apply",
              "_preflight_restore_then_apply"):
    setattr(Harness, _name, getattr(MainWindow, _name))


_KEEP_ALIVE: list = []


def _join_workers(timeout: float = 10.0) -> None:
    """Wait for every preflight worker thread (their last act is a signal emit)."""
    import threading
    for t in list(threading.enumerate()):
        if t.name.startswith("col-preflight"):
            t.join(timeout)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch, app):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path / "Downloads"))
    stub = tmp_path / "hpatchz"
    stub.write_text(_STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(sr, "ensure_hpatchz", lambda **_kw: str(stub))
    monkeypatch.setattr(sr, "find_hpatchz", lambda: str(stub))
    monkeypatch.setattr("Utils.exe_launch.exe_launch._wine_process_alive", lambda _n: False)

    root = tmp_path / "Skyrim Special Edition"
    for rel, data in ORIG.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    game = FakeGame("Skyrim Special Edition", root)

    from Utils.config_paths import get_download_cache_dir_for_game
    cache = get_download_cache_dir_for_game(game.name)
    cache.mkdir(parents=True, exist_ok=True)
    src = write_swap(tmp_path / "zipsrc" / "RuntimeSwap")
    archive = cache / "SRS-189855-1-1-0.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src.parent))
    (cache / (archive.name + ".fileid")).write_text(str(SRS_FILE_ID))

    h = Harness()
    h.game = game
    _KEEP_ALIVE.append(h)             # never let Python free a window a worker may still signal
    yield h, game
    _join_workers()
    h.close()
    QApplication.processEvents()


def _info(game, mods):
    return {"game": game, "mods": list(mods), "domain": "skyrimspecialedition",
            "local_manifest": {"info": {"gameVersions": ["1.7.104.0"]}}, "ok": False, "api": None}


def _wait(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return cond()


def _game_tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _run(h, game, mods):
    fired = []
    info = _info(game, mods)
    h._collection_preflight(info, lambda: fired.append(list(info["mods"])))
    return info, fired


SRS = Mod(SRS_MOD_ID, SRS_FILE_ID, "SRS - Best of All Worlds")
OTHER = Mod(4242, 1, "Some Mod")


def test_game_already_at_target_continues_without_a_dialog(env):
    h, game = env
    (game.root / "SkyrimSE.exe").write_bytes(NEW["SkyrimSE.exe"])
    info, fired = _run(h, game, [OTHER, SRS])
    assert _wait(lambda: fired)
    assert [m.mod_id for m in fired[0]] == [4242]           # the swap mod is never installed
    assert h.findChildren(PreflightOverlay) == []
    assert h._col_install_running                           # still true: the install goes on


def test_a_collection_without_the_swap_mod_is_not_touched(env):
    h, game = env
    info, fired = _run(h, game, [OTHER])
    assert _wait(lambda: fired)
    assert [m.mod_id for m in fired[0]] == [4242]
    assert h.findChildren(PreflightOverlay) == []
    assert (game.root / "SkyrimSE.exe").read_bytes() == ORIG["SkyrimSE.exe"]


def test_other_games_skip_the_skyrim_checks_entirely(env):
    h, game = env
    other = FakeGame("Fallout 4", game.root, steam_id="377160")
    info, fired = _run(h, other, [OTHER, SRS])
    assert _wait(lambda: fired)
    assert [m.mod_id for m in fired[0]] == [4242, SRS_MOD_ID]     # not Skyrim: nothing filtered
    assert (game.root / "SkyrimSE.exe").read_bytes() == ORIG["SkyrimSE.exe"]


def test_fix_and_continue_patches_the_game_then_proceeds(env):
    h, game = env
    info, fired = _run(h, game, [OTHER, SRS])
    assert _wait(lambda: h.findChildren(PreflightOverlay))
    assert not fired                                        # blocked until the user acts
    (overlay,) = h.findChildren(PreflightOverlay)
    assert overlay._mode == "fix"
    overlay._on_go()
    assert _wait(lambda: fired), h.logs
    assert _game_tree(game.root) == {rel: NEW[rel] for rel in ORIG}
    assert sr.read_runtime_version(game.root) == (1, 6, 1170, 0)
    assert [m.mod_id for m in fired[0]] == [4242]


def test_a_deployed_game_is_restored_first_then_patched(env):
    h, game = env
    (game.root / "Data" / ".mm_deployed").write_bytes(b"")
    info, fired = _run(h, game, [SRS])
    assert _wait(lambda: h.findChildren(PreflightOverlay))
    h.findChildren(PreflightOverlay)[0]._on_go()
    assert _wait(lambda: fired), h.logs
    assert h.restore_calls == 1
    assert sr.read_runtime_version(game.root) == (1, 6, 1170, 0)


def test_a_failed_restore_leaves_the_game_untouched_and_the_dialog_open(env):
    h, game = env
    (game.root / "Data" / ".mm_deployed").write_bytes(b"")
    h.restore_ok = False
    before = _game_tree(game.root)
    info, fired = _run(h, game, [SRS])
    assert _wait(lambda: h.findChildren(PreflightOverlay))
    (overlay,) = h.findChildren(PreflightOverlay)
    overlay._on_go()
    assert _wait(lambda: "Restore failed" in overlay._status.text())
    assert not fired and _game_tree(game.root) == before
    assert overlay._go.isEnabled()                          # the user can retry


def test_a_modified_game_file_refuses_the_swap_and_says_why(env):
    h, game = env
    (game.root / "Data" / "Skyrim.esm").write_bytes(b"TES4-edited-by-a-tool")
    before = _game_tree(game.root)
    info, fired = _run(h, game, [SRS])
    assert _wait(lambda: h.findChildren(PreflightOverlay))
    (overlay,) = h.findChildren(PreflightOverlay)
    overlay._on_go()
    assert _wait(lambda: "not the original" in overlay._status.text()), overlay._status.text()
    assert not fired and _game_tree(game.root) == before


def test_an_unsupported_version_cannot_be_fixed(env):
    h, game = env
    (game.root / "SkyrimSE.exe").write_bytes(build_pe((1, 6, 640, 0)))
    info, fired = _run(h, game, [SRS])
    assert _wait(lambda: h.findChildren(PreflightOverlay))
    (overlay,) = h.findChildren(PreflightOverlay)
    assert overlay._mode == "close" and not fired


def test_cancel_abandons_the_install_and_frees_the_lock(env):
    h, game = env
    info, fired = _run(h, game, [SRS])
    assert _wait(lambda: h.findChildren(PreflightOverlay))
    h.findChildren(PreflightOverlay)[0]._cancel.click()
    assert _wait(lambda: not h._col_install_running)
    assert not fired and any("cancelled" in n for n in h.notes)
    assert (game.root / "SkyrimSE.exe").read_bytes() == ORIG["SkyrimSE.exe"]


def test_the_swap_archive_is_not_left_extracted_in_temp(env):
    import tempfile
    h, game = env
    before = set(Path(tempfile.gettempdir()).glob("mosaic-runtime-swap-*"))
    info, fired = _run(h, game, [SRS])
    assert _wait(lambda: h.findChildren(PreflightOverlay))
    h.findChildren(PreflightOverlay)[0]._on_go()
    assert _wait(lambda: fired)
    assert set(Path(tempfile.gettempdir()).glob("mosaic-runtime-swap-*")) == before

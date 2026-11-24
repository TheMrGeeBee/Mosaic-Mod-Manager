"""BSA Memory Map under Proton fills the ±2 GB window around SkyrimSE.exe, so SKSE
plugins loading after it fail to hook ("dosemetha.dll: Failed to hook
GetLocationsCandidates", LoreRim 2026-11-23). Mosaic warns before Play and, only
when asked, sets bEnabled=0."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton  # noqa: E402

from Games.Bethesda.skyrim_se import SkyrimSE  # noqa: E402
from gui_qt.app import MainWindow  # noqa: E402
from gui_qt.overlays.confirm_overlay import ConfirmOverlay  # noqa: E402
from Utils.modding_tools import bsa_memory_map as bmm  # noqa: E402

INI = (b"[General]\r\n; switch the whole plugin\r\nbEnabled=1 ; on\r\n"
       b"bEnableMmap=1\r\n")
MOD = "Faster File Copy"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    game_root = tmp_path / "game"
    plugins = game_root / "Data" / "SKSE" / "Plugins"
    plugins.mkdir(parents=True)
    staging = tmp_path / "mods"
    staged = staging / MOD / "SKSE" / "Plugins"
    staged.mkdir(parents=True)
    profile_root = tmp_path / "root"
    prof = profile_root / "profiles" / "P"
    prof.mkdir(parents=True)
    g = SkyrimSE()
    monkeypatch.setattr(g, "get_game_path", lambda: game_root)
    monkeypatch.setattr(g, "get_profile_root", lambda: profile_root)
    monkeypatch.setattr(g, "get_mod_staging_path", lambda: staging)
    monkeypatch.setattr(g, "get_deploy_active", lambda: True)
    return g, plugins, staged, prof


def _deploy(plugins, staged, prof, ini=INI, link="symlink"):
    """Stage the mod and deploy it the way Mosaic does (symlink, hardlink or copy)."""
    (staged / "BSAMemoryMap.dll").write_bytes(b"MZ")
    (plugins / "BSAMemoryMap.dll").write_bytes(b"MZ")
    if ini is not None:
        (staged / "BSAMemoryMap.ini").write_bytes(ini)
        dst = plugins / "BSAMemoryMap.ini"
        if link == "symlink":
            dst.symlink_to(staged / "BSAMemoryMap.ini")
        elif link == "hardlink":
            os.link(staged / "BSAMemoryMap.ini", dst)
        else:
            dst.write_bytes(ini)
    (prof / "deployed_filemap.txt").write_text(
        f"SKSE/Plugins/BSAMemoryMap.dll\t{MOD}\nSKSE/Plugins/BSAMemoryMap.ini\t{MOD}\n",
        encoding="utf-8")


def test_nothing_when_the_plugin_is_not_deployed(setup):
    g, *_ = setup
    assert g.find_bsa_memory_map("P") is None


def test_nothing_when_it_is_already_switched_off(setup):
    g, plugins, staged, prof = setup
    _deploy(plugins, staged, prof, ini=INI.replace(b"bEnabled=1", b"bEnabled = 0"))
    assert g.find_bsa_memory_map("P") is None


def test_nothing_when_nothing_is_deployed(setup, monkeypatch):
    g, plugins, staged, prof = setup
    _deploy(plugins, staged, prof)
    monkeypatch.setattr(g, "get_deploy_active", lambda: False)
    assert g.find_bsa_memory_map("P") is None


def test_bEnableMmap_off_alone_still_counts_as_on(setup):
    g, plugins, staged, prof = setup
    _deploy(plugins, staged, prof, ini=INI.replace(b"bEnableMmap=1", b"bEnableMmap=0"))
    assert g.find_bsa_memory_map("P") is not None


def test_no_ini_counts_as_on_but_cannot_be_fixed(setup):
    g, plugins, staged, prof = setup
    _deploy(plugins, staged, prof, ini=None)
    f = g.find_bsa_memory_map("P")
    assert f is not None and not f.can_disable


@pytest.mark.parametrize("link", ["symlink", "hardlink", "copy"])
def test_disable_edits_the_staged_ini_so_it_survives_redeploy(setup, link):
    g, plugins, staged, prof = setup
    _deploy(plugins, staged, prof, link=link)
    f = g.find_bsa_memory_map("P")
    assert f is not None and f.can_disable
    assert len(f.ini_sources) == (2 if link == "copy" else 1)   # linked = one file
    bmm.disable(f)
    want = INI.replace(b"bEnabled=1 ", b"bEnabled=0 ")    # CRLF, comments, rest kept
    assert (staged / "BSAMemoryMap.ini").read_bytes() == want
    assert (plugins / "BSAMemoryMap.ini").read_bytes() == want
    if link == "symlink":
        assert (plugins / "BSAMemoryMap.ini").is_symlink()
    assert g.find_bsa_memory_map("P") is None


def test_case_insensitive_paths(setup):
    g, plugins, staged, prof = setup
    (plugins / "bsamemorymap.DLL").write_bytes(b"MZ")
    (plugins / "BsaMemoryMap.INI").write_bytes(b"benabled=true\n")
    f = g.find_bsa_memory_map("P")
    assert f is not None
    bmm.disable(f)
    assert (plugins / "BsaMemoryMap.INI").read_bytes() == b"benabled=0\n"


# ---- the app's launch guard ---------------------------------------------------

class Harness(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(900, 700)
        self.show()
        self._gs = type("GS", (), {"profile": "P"})()
        self.logs, self.notes = [], []

    def _append_log(self, m):
        self.logs.append(m)

    def _notify(self, m, kind):
        self.notes.append((m, kind))


Harness._guard_bsa_memory_map = MainWindow._guard_bsa_memory_map


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _buttons(h):
    (ov,) = h.findChildren(ConfirmOverlay)
    return ov, {b.text(): b for b in ov.findChildren(QPushButton)}


def test_guard_launches_straight_away_when_not_deployed(app, setup):
    g, *_ = setup
    h, fired = Harness(), []
    h._guard_bsa_memory_map(g, lambda: fired.append(1))
    assert fired == [1] and not h.findChildren(ConfirmOverlay)


def test_guard_switch_off_edits_the_ini_then_launches(app, setup):
    g, plugins, staged, prof = setup
    _deploy(plugins, staged, prof)
    h, fired = Harness(), []
    h._guard_bsa_memory_map(g, lambda: fired.append(1))
    assert fired == []
    ov, btns = _buttons(h)
    assert "Failed to hook" in " ".join(l.text() for l in ov.findChildren(QLabel))
    btns["Switch off and launch"].click()
    assert fired == [1] and b"bEnabled=0" in (staged / "BSAMemoryMap.ini").read_bytes()


def test_guard_launch_anyway_leaves_the_mod_alone(app, setup):
    g, plugins, staged, prof = setup
    _deploy(plugins, staged, prof)
    h, fired = Harness(), []
    h._guard_bsa_memory_map(g, lambda: fired.append(1))
    _buttons(h)[1]["Launch anyway"].click()
    assert fired == [1] and (staged / "BSAMemoryMap.ini").read_bytes() == INI


def test_guard_cancel_does_nothing(app, setup):
    g, plugins, staged, prof = setup
    _deploy(plugins, staged, prof)
    h, fired = Harness(), []
    h._guard_bsa_memory_map(g, lambda: fired.append(1))
    _buttons(h)[1]["Cancel"].click()
    assert fired == [] and (staged / "BSAMemoryMap.ini").read_bytes() == INI


def test_guard_without_an_ini_only_offers_launch_anyway(app, setup):
    g, plugins, staged, prof = setup
    _deploy(plugins, staged, prof, ini=None)
    h, fired = Harness(), []
    h._guard_bsa_memory_map(g, lambda: fired.append(1))
    _ov, btns = _buttons(h)
    assert set(btns) == {"Cancel", "Launch anyway"}
    btns["Launch anyway"].click()
    assert fired == [1]


def test_guard_never_blocks_on_an_internal_error(app):
    class Boom:
        def find_bsa_memory_map(self, _p):
            raise RuntimeError("boom")
    h, fired = Harness(), []
    h._guard_bsa_memory_map(Boom(), lambda: fired.append(1))
    assert fired == [1] and any("skipped" in m for m in h.logs)


def test_games_without_the_hook_launch_normally(app):
    h, fired = Harness(), []
    h._guard_bsa_memory_map(object(), lambda: fired.append(1))
    assert fired == [1]

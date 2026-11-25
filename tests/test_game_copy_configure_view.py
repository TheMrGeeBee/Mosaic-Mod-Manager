"""Configure Game's "Game Copy (Stock Game)" section."""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import Games.base_game as base_game  # noqa: E402
from Games.Bethesda.skyrim_se import SkyrimSE  # noqa: E402
from gui_qt.overlays.confirm_overlay import ConfirmOverlay  # noqa: E402
from gui_qt.views.configure_game_view import ConfigureGameView  # noqa: E402
from Utils import game_copy as gc  # noqa: E402
from Utils.profile.profile_state import merge_profile_settings, read_profile_settings  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch):
    steam = tmp_path / "lib" / "steamapps" / "common" / "Skyrim Special Edition"
    (steam / "Data").mkdir(parents=True)
    (steam / "SkyrimSE.exe").write_bytes(b"MZ")
    paths_json = tmp_path / "cfg" / "paths.json"
    paths_json.parent.mkdir(parents=True)
    paths_json.write_text(json.dumps({"game_path": str(steam)}), encoding="utf-8")
    monkeypatch.setattr(base_game, "get_game_config_path", lambda _n: paths_json)
    monkeypatch.setattr(base_game, "_find_steam_prefix", lambda *_a: None)
    monkeypatch.setattr(gc, "_prefs_file", lambda _n: tmp_path / "cfg" / "game_copies.json")
    root = tmp_path / "staging"
    for name in ("default", "GTS"):
        (root / "profiles" / name).mkdir(parents=True)
    g = SkyrimSE()
    monkeypatch.setattr(g, "get_profile_root", lambda: root)
    monkeypatch.setattr(g, "is_configured", lambda: True)
    monkeypatch.setattr(ConfigureGameView, "_start_prefix_scan", lambda self: None)
    return g, steam, root / "profiles", tmp_path


def _view(g, profile_dir):
    g.set_active_profile_dir(profile_dir)
    g.load_paths()
    v = ConfigureGameView(g, on_done=lambda *_a: None)
    v.resize(1200, 800)
    v.show()
    return v


def _make_copy(tmp_path, steam, build="1.6.1170"):
    dest = tmp_path / "Modlists" / "GTS" / gc.COPY_DIRNAME
    dest.mkdir(parents=True)
    (dest / "SkyrimSE.exe").write_bytes(b"MZ")
    (dest / gc.MARKER_NAME).write_text(json.dumps({
        "game": "Skyrim Special Edition", "build": build, "version": build,
        "source": str(steam), "method": "reflink",
        "created_at": "2026-11-24T03:30:00+00:00"}), encoding="utf-8")
    return dest


def test_default_profile_explains_copies_are_per_profile(app, env):
    g, _steam, profiles, _tmp = env
    v = _view(g, profiles / "default")
    assert "per profile" in v._copy_status.text()
    assert not v._copy_make_btn.isVisible()


def test_steam_profile_offers_to_make_a_copy(app, env):
    g, _steam, profiles, _tmp = env
    v = _view(g, profiles / "GTS")
    assert "uses the Steam install" in v._copy_status.text()
    assert v._copy_make_btn.isVisible() and not v._copy_delete_btn.isVisible()


def test_a_finished_copy_fills_the_game_folder_for_save(app, env):
    g, steam, profiles, tmp = env
    v = _view(g, profiles / "GTS")
    dest = _make_copy(tmp, steam)
    info = gc.read_marker(dest)
    v._on_copy_done((dest, info, None, (dest, "1.6.1170"), None))
    assert v._game_edit.text() == str(dest)
    assert "Press Save" in v._copy_status.text()
    assert v._copy_delete_btn.isVisible() and v._copy_steam_btn.isVisible()
    assert gc.last_parent(g.name) == tmp / "Modlists"


def test_no_reflink_asks_before_copying(app, env):
    g, steam, profiles, tmp = env
    v = _view(g, profiles / "GTS")
    started = []
    v._start_copy = lambda req, ch, full: started.append(full)
    err = gc.ReflinkUnavailable(16_000_000_000)
    v._on_copy_done((tmp / "x", None, err, (tmp / "x", "b"), None))
    (ov,) = v.window().findChildren(ConfirmOverlay)
    ov._finish(True)
    assert started == [True]


def test_profile_on_a_copy_shows_it_and_can_go_back(app, env):
    g, steam, profiles, tmp = env
    dest = _make_copy(tmp, steam)
    merge_profile_settings(profiles / "GTS", {"game_path": str(dest)})
    v = _view(g, profiles / "GTS")
    assert "own game copy: 1.6.1170" in v._copy_status.text()
    v._on_use_steam_install()
    assert v._game_edit.text() == str(steam) and "Press Save" in v._copy_status.text()


def test_delete_switches_this_profile_back_then_deletes(app, env):
    g, steam, profiles, tmp = env
    dest = _make_copy(tmp, steam)
    merge_profile_settings(profiles / "GTS", {"game_path": str(dest)})
    v = _view(g, profiles / "GTS")
    v._on_delete_copy()
    (ov,) = v.window().findChildren(ConfirmOverlay)
    ov._finish(True)
    for _ in range(200):
        app.processEvents()
        if not dest.exists() and not v._copy_busy:
            break
        import time
        time.sleep(0.01)
    assert not dest.exists()
    assert "game_path" not in read_profile_settings(profiles / "GTS")
    assert g.get_game_path() == steam and v._game_edit.text() == str(steam)


def test_delete_refuses_while_another_profile_uses_it(app, env):
    g, steam, profiles, tmp = env
    dest = _make_copy(tmp, steam)
    (profiles / "GTS_alt").mkdir()
    merge_profile_settings(profiles / "GTS", {"game_path": str(dest)})
    merge_profile_settings(profiles / "GTS_alt", {"game_path": str(dest)})
    v = _view(g, profiles / "GTS")
    v._on_delete_copy()
    assert "Still used by: GTS_alt" in v._copy_status.text()
    assert not v.window().findChildren(ConfirmOverlay) and dest.exists()


def test_making_a_copy_that_already_exists_offers_it(app, env):
    """Real case: the copy finished, the form went back to the Steam path before
    Save, and Make a game copy again said the folder 'isn't empty'."""
    g, steam, profiles, tmp = env
    v = _view(g, profiles / "GTS")
    dest = _make_copy(tmp, steam, build="1.6.1170")
    choices = gc.BuildChoices(current="Steam build 1", labels=["Steam build 1", "1.6.1170"],
                              names={"Steam build 1": "1.7.104", "1.6.1170": "1.6.1170"},
                              exe_file="SkyrimSE.exe", state_dir=tmp)
    v._start_copy((dest, "1.6.1170"), choices, False)
    assert v._game_edit.text() == str(dest) and "already made this copy" in v._copy_status.text()
    assert not v._copy_busy
    v._start_copy((dest, "Steam build 1"), choices, False)
    assert "not 1.7.104" in v._copy_status.text()


def test_steam_cloud_note_follows_the_profile_saves_box(app, env, monkeypatch):
    from PySide6.QtWidgets import QLabel

    from Utils.steam import steam_cloud
    g, _steam, profiles, _tmp = env
    monkeypatch.setattr(steam_cloud, "cloud_saves_warning",
                        lambda game: "Steam Cloud syncs this game's saves (17 in the cloud)")
    v = _view(g, profiles / "default")
    note = next(lbl for lbl in v.findChildren(QLabel) if "Steam Cloud syncs" in lbl.text())
    box = v._opt_checks["profile_saves"]
    box.setChecked(True)
    assert note.isVisible()
    box.setChecked(False)
    assert not note.isVisible()
    v.close()

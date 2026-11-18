"""OpenMW groundcover support and the openmw.cfg plugin list.

* Plugins marked as groundcover go to openmw.cfg as ``groundcover=`` and NOT as
  ``content=`` (otherwise OpenMW spawns every blade of grass as a normal object).
* A plugin disabled in Mosaic (a bare line in plugins.txt) is no longer written as
  ``content=``; it used to be, so disabling had no effect on OpenMW.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Games.Morrowind import openmw as openmw_mod  # noqa: E402
from Games.Morrowind.openmw import OpenMW  # noqa: E402
from Games.Morrowind.openmw_cfg import (  # noqa: E402
    looks_like_groundcover,
    read_groundcover,
    restore_openmw_cfg,
    set_groundcover,
    update_openmw_cfg,
    write_groundcover,
)
from Utils.deploy.deploy import LinkMode  # noqa: E402


def _cfg(tmp_path, plugins_txt: str, **kw) -> list[str]:
    (tmp_path / "plugins.txt").write_text(plugins_txt)
    update_openmw_cfg(tmp_path / "openmw.cfg", [tmp_path / "Data Files"], tmp_path / "plugins.txt", **kw)
    return (tmp_path / "openmw.cfg").read_text().splitlines()


# -- the plugin list: disabled plugins stay out -----------------------------------------------------------
def test_a_plugin_disabled_in_mosaic_is_not_written_as_content(tmp_path):
    cfg = _cfg(tmp_path, "*enabled.esp\ndisabled.esp\n*also.esp\n")
    content = [ln for ln in cfg if ln.startswith("content=")]
    assert content[-2:] == ["content=enabled.esp", "content=also.esp"]
    assert "content=disabled.esp" not in cfg


# -- groundcover storage ----------------------------------------------------------------------------------
def test_no_file_means_the_profile_never_used_groundcover(tmp_path):
    assert read_groundcover(tmp_path) is None
    assert read_groundcover(None) is None


def test_groundcover_round_trips_keeping_order_and_dropping_duplicates(tmp_path):
    write_groundcover(tmp_path, ["B.esp", "a.esp", "b.ESP", "", "C.esp"])
    assert read_groundcover(tmp_path) == ["B.esp", "a.esp", "C.esp"]


def test_set_groundcover_adds_and_removes_ignoring_case(tmp_path):
    set_groundcover(tmp_path, ["Grass A.esp", "Grass B.esp"], True)
    set_groundcover(tmp_path, ["grass a.ESP"], False)
    assert read_groundcover(tmp_path) == ["Grass B.esp"]
    set_groundcover(tmp_path, ["Grass B.esp"], True)               # no duplicate
    assert read_groundcover(tmp_path) == ["Grass B.esp"]
    set_groundcover(tmp_path, ["Grass B.esp"], False)
    assert read_groundcover(tmp_path) == []                          # an empty list is not "never used"


@pytest.mark.parametrize("name,expected", [
    ("FGM_AL.esp", True), ("grass_AI.esp", True), ("Ozzy's Grass - West Gash.esp", True),
    ("Aesthesia_Groundcover.esp", True), ("Sky_Main_Grass.esp", True),
    ("Graphic Herbalism.esp", False), ("OAAB_Data.esm", False),
    ("grass_something.esm", False),          # groundcover is never a master
    ("Some Mod.omwscripts", False),
])
def test_grass_looking_plugin_names_are_suggested(name, expected):
    assert looks_like_groundcover(name) is expected


# -- the cfg ------------------------------------------------------------------------------------------------
def test_groundcover_plugins_are_written_as_groundcover_and_not_as_content(tmp_path):
    cfg = _cfg(tmp_path, "*Mod.esp\n*FGM_AL.esp\n*Other.esp\n", groundcover_plugins=["FGM_AL.esp"])
    assert "groundcover=FGM_AL.esp" in cfg
    assert "content=FGM_AL.esp" not in cfg
    assert [ln for ln in cfg if ln.startswith("content=")][-2:] == ["content=Mod.esp", "content=Other.esp"]


def test_groundcover_matching_ignores_case(tmp_path):
    cfg = _cfg(tmp_path, "*FGM_AL.ESP\n", groundcover_plugins=["fgm_al.esp"])
    assert "groundcover=fgm_al.esp" in cfg                 # written as given by the caller
    assert not any(ln.lower() == "content=fgm_al.esp" for ln in cfg)


def test_no_groundcover_list_leaves_lines_already_in_the_cfg_alone(tmp_path):
    """A profile that never marked any (None) keeps what the OpenMW launcher wrote."""
    (tmp_path / "openmw.cfg").write_text("groundcover=ByLauncher.esp\n")
    cfg = _cfg(tmp_path, "*Mod.esp\n")
    assert "groundcover=ByLauncher.esp" in cfg


def test_an_empty_groundcover_list_clears_old_lines(tmp_path):
    (tmp_path / "openmw.cfg").write_text("groundcover=Old.esp\n")
    cfg = _cfg(tmp_path, "*Mod.esp\n", groundcover_plugins=[])
    assert not any(ln.startswith("groundcover=") for ln in cfg)


def test_restore_removes_groundcover_lines(tmp_path):
    _cfg(tmp_path, "*FGM.esp\n", groundcover_plugins=["FGM.esp"])
    restore_openmw_cfg(tmp_path / "openmw.cfg", [tmp_path / "Data Files"])
    assert "groundcover=FGM.esp" not in (tmp_path / "openmw.cfg").read_text()


# -- deploy -----------------------------------------------------------------------------------------------
def _game(tmp_path) -> OpenMW:
    root = tmp_path / "Morrowind"
    (root / "Data Files").mkdir(parents=True)
    (root / "Data Files" / "Morrowind.esm").write_text("vanilla")
    g = OpenMW()
    g._game_path = root
    g._staging_path = tmp_path / "staging"
    g._openmw_cfg_path = tmp_path / "cfg" / "openmw.cfg"
    prof = g.get_profile_root() / "profiles" / "default"
    prof.mkdir(parents=True)
    (prof / "modlist.txt").write_text("")
    g.set_active_profile_dir(prof)
    mods = g.get_effective_mod_staging_path()
    for mod, plugin in (("Grass", "FGM_AL.esp"), ("Quest", "Quest.esp"), ("Off", "Disabled.esp")):
        (mods / mod).mkdir(parents=True)
        (mods / mod / plugin).write_text("p")
    fm = g.get_effective_filemap_path()
    fm.parent.mkdir(parents=True, exist_ok=True)
    fm.write_text("FGM_AL.esp\tGrass\nQuest.esp\tQuest\nDisabled.esp\tOff\n")
    return g


def test_deploy_writes_marked_plugins_as_groundcover(tmp_path):
    g = _game(tmp_path)
    prof = g._active_profile_dir
    (prof / "plugins.txt").write_text("*FGM_AL.esp\n*Quest.esp\nDisabled.esp\n")
    write_groundcover(prof, ["FGM_AL.esp", "Disabled.esp", "NotInstalled.esp"])
    logs = []
    g.deploy(mode=LinkMode.HARDLINK, profile="default", log_fn=logs.append)
    cfg = (tmp_path / "cfg" / "openmw.cfg").read_text().splitlines()
    assert "groundcover=FGM_AL.esp" in cfg
    assert not any(ln.startswith("groundcover=") and "Disabled" in ln for ln in cfg)   # disabled stays out
    assert "groundcover=NotInstalled.esp" not in cfg                                  # not in the list
    content = [ln for ln in cfg if ln.startswith("content=")]
    assert content[-1] == "content=Quest.esp" and "content=FGM_AL.esp" not in cfg
    assert "content=Disabled.esp" not in cfg
    assert any("Groundcover: 1 plugin(s)" in ln for ln in logs)
    # the plugin file itself is still deployed
    assert (g._game_path / "Data Files" / "FGM_AL.esp").is_file()


def test_deploy_without_a_groundcover_list_touches_nothing(tmp_path):
    g = _game(tmp_path)
    (g._active_profile_dir / "plugins.txt").write_text("*FGM_AL.esp\n")
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "openmw.cfg").write_text("groundcover=ByLauncher.esp\n")
    g.deploy(mode=LinkMode.HARDLINK, profile="default")
    cfg = (tmp_path / "cfg" / "openmw.cfg").read_text().splitlines()
    assert "groundcover=ByLauncher.esp" in cfg and "content=FGM_AL.esp" in cfg


def test_only_openmw_offers_groundcover():
    from Games.base_game import BaseGame
    assert OpenMW.supports_groundcover is True and BaseGame.supports_groundcover is False
    assert openmw_mod.OpenMW().supports_groundcover


# -- the Plugins panel ------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _panel(tmp_path, qapp, names, supports=True):
    from PySide6.QtWidgets import QTableView

    from gui_qt.plugin.plugin_model import PluginModel
    from gui_qt.plugin.plugin_state import PluginRow
    model = PluginModel()
    model.set_rows([PluginRow(n, True) for n in names], profile_dir=tmp_path)
    view = QTableView()
    view.setModel(model)
    view.game = SimpleNamespace(supports_groundcover=supports)
    view.selectRow(0)
    return view, model


def _labels(view, row=0):
    from gui_qt.plugin.plugin_menu import build_context_menu
    m = build_context_menu(view, view.model().index(row, 0))
    return [] if m is None else [a.text() for a in m.actions() if a.text()]


def test_the_menu_offers_marking_and_a_one_click_grass_action(tmp_path, qapp):
    view, model = _panel(tmp_path, qapp, ["FGM_AL.esp", "grass_AI.esp", "Quest.esp"])
    labels = _labels(view, 2)
    assert "Mark as groundcover (OpenMW)" in labels
    assert "Mark all grass/groundcover plugins (2)" in labels


def test_marking_saves_the_list_and_flags_the_row(tmp_path, qapp):
    from gui_qt.plugin.plugin_menu import _set_groundcover
    from gui_qt.plugin.plugin_state import PF_GROUNDCOVER
    view, model = _panel(tmp_path, qapp, ["FGM_AL.esp", "grass_AI.esp", "Quest.esp"])
    told = []
    view.on_groundcover_changed = lambda n, marked: told.append((n, marked))
    _set_groundcover(view, [0, 1], True)
    assert read_groundcover(tmp_path) == ["FGM_AL.esp", "grass_AI.esp"]
    assert model.row(0).flags & PF_GROUNDCOVER and model.row(1).flags & PF_GROUNDCOVER
    assert not model.row(2).flags & PF_GROUNDCOVER
    assert told == [(2, True)]
    assert "Remove groundcover mark" in _labels(view, 0)
    _set_groundcover(view, [0], False)
    assert read_groundcover(tmp_path) == ["grass_AI.esp"] and not model.row(0).flags & PF_GROUNDCOVER


def test_the_all_grass_action_marks_every_unmarked_grass_plugin(tmp_path, qapp):
    from gui_qt.plugin.plugin_menu import build_context_menu
    view, model = _panel(tmp_path, qapp, ["FGM_AL.esp", "grass_AI.esp", "Quest.esp"])
    menu = build_context_menu(view, model.index(2, 0))
    next(a for a in menu.actions() if a.text().startswith("Mark all grass")).trigger()
    assert read_groundcover(tmp_path) == ["FGM_AL.esp", "grass_AI.esp"]
    assert not any(t.startswith("Mark all grass") for t in _labels(view, 2))      # nothing left to suggest


def test_other_games_never_see_groundcover(tmp_path, qapp):
    view, _ = _panel(tmp_path, qapp, ["FGM_AL.esp"], supports=False)
    assert not any("roundcover" in t for t in _labels(view))


def test_the_flags_column_shows_a_groundcover_badge(qapp):
    from gui_qt.plugin.plugin_state import PF_GROUNDCOVER
    from gui_qt.plugin.plugin_view import PluginDelegate
    items = PluginDelegate._flag_items(PF_GROUNDCOVER)
    assert [i[0] for i in items] == ["gc"]

"""Wizard menu categories: BethINI edits INI files (INI Tweaks); Wrye Bash's main
job is building patches and cleaning plugins (Patchers and Cleanup), even though it
also handles load order."""
from __future__ import annotations

import pytest

from Games.base_game import WizardTool
from Utils.wizard_support.wizard_catalog import group_by_category, infer_category
from wizards_qt.proton_step import ProtonStepWidget


def _tool(tool_id):
    return WizardTool(id=tool_id, label=tool_id, description="", dialog_class_path="x")


@pytest.mark.parametrize("tool_id", [
    "run_bethini_skyrimse", "run_bethini_fo4", "run_bethini_fonv", "run_bethini_starfield"])
def test_bethini_is_an_ini_tweak_tool(tool_id):
    assert infer_category(_tool(tool_id)) == "INI Tweaks"


@pytest.mark.parametrize("tool_id", [
    "run_wrye_bash_skyrimse", "run_wrye_bash_skyrim", "run_wrye_bash_fo4",
    "run_wrye_bash_fo3", "run_wrye_bash_fonv", "run_wrye_bash_oblivion",
    "run_wrye_bash_starfield", "run_wrye_bash_morrowind", "run_wrye_bash_enderalse"])
def test_wrye_bash_is_a_patcher(tool_id):
    assert infer_category(_tool(tool_id)) == "Patchers and Cleanup"


def test_an_explicit_category_still_wins():
    t = WizardTool(id="run_bethini_skyrimse", label="x", description="",
                   dialog_class_path="x", category="Other")
    assert infer_category(t) == "Other"


def test_the_real_skyrim_se_tool_list_is_grouped_as_asked():
    from Games.Bethesda.skyrim_se import SkyrimSE
    groups = dict(group_by_category(SkyrimSE().wizard_tools))
    assert any(t.id == "run_bethini_skyrimse" for t in groups.get("INI Tweaks", []))
    assert any(t.id == "run_wrye_bash_skyrimse" for t in groups.get("Patchers and Cleanup", []))
    for cat, tools in groups.items():
        if cat != "INI Tweaks":
            assert all("bethini" not in t.id for t in tools)
        if cat != "Patchers and Cleanup":
            assert all("wrye_bash" not in t.id for t in tools)


def test_the_always_use_configuration_countdown_is_ten_seconds():
    assert ProtonStepWidget.AUTO_CONTINUE_SECONDS == 10

"""BaseGame owns the staging / deploy-mode / prefix accessors that every game
handler used to re-declare identically."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from Games.base_game import BaseGame  # noqa: E402
from Utils.deploy.deploy import LinkMode  # noqa: E402

_HOISTED = {
    "get_mod_staging_path", "set_staging_path", "get_deploy_mode",
    "set_deploy_mode", "get_prefix_path", "set_prefix_path",
}


class _G(BaseGame):
    name = "Test Game"
    game_id = "test_game"
    exe_name = "test.exe"

    def get_mod_data_path(self):
        return None

    def save_paths(self):
        self.saved = getattr(self, "saved", 0) + 1


def test_staging_path_default_and_custom(tmp_path):
    g = _G()
    assert g.get_mod_staging_path().parts[-2:] == ("Test Game", "mods")
    g.set_staging_path(tmp_path)
    assert g.get_mod_staging_path() == tmp_path / "mods"
    g.set_staging_path(None)
    assert g._staging_path is None and g.saved == 2


def test_deploy_mode_and_prefix_round_trip(tmp_path):
    g = _G()
    g.set_deploy_mode(LinkMode.SYMLINK)
    assert g.get_deploy_mode() is LinkMode.SYMLINK
    assert g.get_prefix_path() is None
    g.set_prefix_path(str(tmp_path))
    assert g.get_prefix_path() == tmp_path
    g.set_prefix_path("")
    assert g.get_prefix_path() is None
    assert g.saved == 3


@pytest.mark.parametrize("path", sorted((SRC / "Games").rglob("*.py")))
def test_game_modules_do_not_redeclare_hoisted_methods(path):
    if path.name == "base_game.py":
        pytest.skip("the owner")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    dupes = {
        f"{c.name}.{n.name}"
        for c in ast.walk(tree) if isinstance(c, ast.ClassDef)
        for n in c.body
        if isinstance(n, ast.FunctionDef) and n.name in _HOISTED
    }
    # OpenMW genuinely has no Proton prefix, so it overrides those two.
    allowed = {"OpenMW.get_prefix_path", "OpenMW.set_prefix_path", "OpenMW.get_mod_staging_path"}
    assert dupes <= allowed, dupes - allowed

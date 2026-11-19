"""Dependency-based ordering of OpenMW .omwscripts files."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from Games.Morrowind.omw_script_order import (  # noqa: E402
    ScriptInfo,
    make_orderer,
    order_scripts,
    parse_omwscripts,
    scan_lua,
)
from Games.Morrowind.openmw_cfg import update_openmw_cfg  # noqa: E402


def _scan(table):
    return lambda n: table.get(n)


def test_parse_omwscripts_ignores_flags_comments_and_blanks(tmp_path):
    f = tmp_path / "a.omwscripts"
    f.write_text("# hi\n\nGLOBAL: scripts/a/g.lua\nPLAYER, NPC: scripts\\a\\p.lua\n")
    assert parse_omwscripts(f) == ["scripts/a/g.lua", "scripts/a/p.lua"]


def test_scan_lua_provides_and_uses():
    src = """
    local I = require('openmw.interfaces')
    local x = I.ErnPerkFramework.register({})
    -- I.Commented.thing()
    return { interfaceName = "FactionPerks", interface = {} }
    """
    provides, uses = scan_lua(src)
    assert provides == {"FactionPerks"}
    assert uses == {"ErnPerkFramework"}


def test_scan_lua_require_interfaces_forms():
    _, uses = scan_lua("local a = require('openmw.interfaces').Foo\n"
                       "local b = require(\"openmw.interfaces\")[\"Bar\"]\n")
    assert uses == {"Foo", "Bar"}


def test_user_moves_after_provider():
    info = {
        "FactionPerks.omwscripts": ScriptInfo(set(), {"ErnPerkFramework"}),
        "ErnPerkFramework.omwscripts": ScriptInfo({"ErnPerkFramework"}, set()),
    }
    names = ["A.esp", "FactionPerks.omwscripts", "B.esp", "ErnPerkFramework.omwscripts", "C.esp"]
    out, moves, notes = order_scripts(names, _scan(info))
    assert out == ["A.esp", "B.esp", "ErnPerkFramework.omwscripts", "FactionPerks.omwscripts", "C.esp"]
    assert [(m.name, m.after, m.because) for m in moves] == [
        ("FactionPerks.omwscripts", "ErnPerkFramework.omwscripts", "ErnPerkFramework")]
    assert notes == []


def test_correct_order_is_untouched():
    info = {
        "p.omwscripts": ScriptInfo({"X"}, set()),
        "u.omwscripts": ScriptInfo(set(), {"X"}),
    }
    names = ["p.omwscripts", "m.esp", "u.omwscripts"]
    out, moves, _ = order_scripts(names, _scan(info))
    assert out == names and moves == []


def test_chain_is_resolved():
    info = {
        "c.omwscripts": ScriptInfo(set(), {"B"}),
        "b.omwscripts": ScriptInfo({"B"}, {"A"}),
        "a.omwscripts": ScriptInfo({"A"}, set()),
    }
    out, _, notes = order_scripts(["c.omwscripts", "b.omwscripts", "a.omwscripts"], _scan(info))
    assert out == ["a.omwscripts", "b.omwscripts", "c.omwscripts"]
    assert notes == []


def test_cycle_is_reported_and_terminates():
    info = {
        "a.omwscripts": ScriptInfo({"A"}, {"B"}),
        "b.omwscripts": ScriptInfo({"B"}, {"A"}),
    }
    out, _, notes = order_scripts(["a.omwscripts", "b.omwscripts"], _scan(info))
    assert sorted(out) == ["a.omwscripts", "b.omwscripts"]
    assert notes


def test_unknown_provider_and_unscanned_script_are_ignored():
    info = {"u.omwscripts": ScriptInfo(set(), {"Nobody"})}
    names = ["u.omwscripts", "x.omwscripts"]
    out, moves, _ = order_scripts(names, _scan(info))
    assert out == names and moves == []


def test_make_orderer_reads_real_files_case_insensitively(tmp_path):
    data = tmp_path / "Data Files"
    (data / "Scripts" / "ern").mkdir(parents=True)
    (data / "Scripts" / "fp").mkdir(parents=True)
    (data / "ErnPerkFramework.omwscripts").write_text("GLOBAL: scripts/ern/g.lua\n")
    (data / "Scripts" / "ern" / "g.lua").write_text('return { interfaceName = "ErnPerkFramework" }')
    (data / "FactionPerks.omwscripts").write_text("PLAYER: scripts/fp/p.lua\n")
    (data / "Scripts" / "fp" / "p.lua").write_text("local I = require('openmw.interfaces')\nI.ErnPerkFramework.x()\n")
    out, moves, _ = make_orderer([data])(["FactionPerks.omwscripts", "ErnPerkFramework.omwscripts"])
    assert out == ["ErnPerkFramework.omwscripts", "FactionPerks.omwscripts"]
    assert len(moves) == 1


def test_update_openmw_cfg_applies_orderer(tmp_path):
    (tmp_path / "plugins.txt").write_text("*Mod.esp\n*b.omwscripts\n*a.omwscripts\n")
    info = {"b.omwscripts": ScriptInfo(set(), {"A"}), "a.omwscripts": ScriptInfo({"A"}, set())}
    logs: list[str] = []
    update_openmw_cfg(tmp_path / "openmw.cfg", [tmp_path / "Data Files"], tmp_path / "plugins.txt",
                      script_orderer=lambda n: order_scripts(n, _scan(info)), log_fn=logs.append)
    content = [ln for ln in (tmp_path / "openmw.cfg").read_text().splitlines() if ln.startswith("content=")]
    assert content[-3:] == ["content=Mod.esp", "content=a.omwscripts", "content=b.omwscripts"]
    assert any("b.omwscripts now loads after a.omwscripts" in m for m in logs)

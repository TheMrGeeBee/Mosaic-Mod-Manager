"""Native BG3 dependency load-order sort (Utils.mods.bg3_sort).

Covers two things ported over while looking at BG3ModManager-Redux's "Load
Order Advisor": resolve_load_order's cycle-detection guard (a genuine
circular dependency used to recurse forever — RecursionError, crashing
deploy), and compute_sort_plan's stable-partial-reorder property (only mods
the dependency graph has an opinion about may move; separators, disabled
mods, and mods with no .pak metadata must stay exactly where they are).

modlist.txt is highest-priority-first (top = wins file conflicts); a mod
that depends on another must load AFTER it in modsettings.lsx (BG3: later
entries override earlier ones) — which means the DEPENDENT ends up ABOVE its
dependency in modlist.txt, not below. That's the direction these tests check.
"""
from __future__ import annotations

from Utils.mods.bg3_sort import compute_sort_plan
from Utils.mods.modlist import ModEntry, write_modlist
from Utils.mods.modsettings import BG3ModInfo, resolve_load_order


def _info(uuid: str, name: str, deps: tuple[str, ...] = ()) -> BG3ModInfo:
    return BG3ModInfo(uuid=uuid, name=name, folder=name, version64="1",
                      dependencies=list(deps), source_mod=name)


class _FakeGame:
    """Minimal stand-in for BaldursGate3 — only what compute_sort_plan touches."""

    def __init__(self, profile_dir):
        self._active_profile_dir = profile_dir
        self.name = "Baldur's Gate 3"

    def get_effective_mod_staging_path(self):
        return self._active_profile_dir / "mods"

    def get_game_path(self):
        return None  # skip base-game/DLC UUID scanning in these tests


def _seed_modlist(tmp_path, entries: list[ModEntry]):
    modlist_path = tmp_path / "modlist.txt"
    write_modlist(modlist_path, entries)
    return modlist_path


# ---------------------------------------------------------------------------
# resolve_load_order cycle handling
# ---------------------------------------------------------------------------

def test_circular_dependency_does_not_recurse_forever():
    a = _info("uuid-a", "Mod A", deps=("uuid-b",))
    b = _info("uuid-b", "Mod B", deps=("uuid-a",))
    entries = [ModEntry(name="Mod A", enabled=True, locked=False),
              ModEntry(name="Mod B", enabled=True, locked=False)]
    mod_infos = {"uuid-a": a, "uuid-b": b}

    cycles: list[tuple[str, str]] = []
    ordered = resolve_load_order(entries, mod_infos, cycles=cycles)

    # No RecursionError, both mods still present exactly once, and the
    # back-edge was reported rather than silently swallowed.
    assert sorted(i.uuid for i in ordered) == ["uuid-a", "uuid-b"]
    assert cycles


def test_no_cycle_is_reported_for_a_simple_chain():
    a = _info("uuid-a", "Mod A", deps=("uuid-b",))
    b = _info("uuid-b", "Mod B")
    entries = [ModEntry(name="Mod A", enabled=True, locked=False),
              ModEntry(name="Mod B", enabled=True, locked=False)]
    cycles: list[tuple[str, str]] = []
    resolve_load_order(entries, {"uuid-a": a, "uuid-b": b}, cycles=cycles)
    assert cycles == []


# ---------------------------------------------------------------------------
# compute_sort_plan
# ---------------------------------------------------------------------------

def test_sort_moves_dependent_above_its_dependency(tmp_path, monkeypatch):
    # "Needs B" depends on "Provides B" but is listed BELOW it — wrong; the
    # dependent must end up above its dependency in modlist.txt.
    modlist_path = _seed_modlist(tmp_path, [
        ModEntry(name="Provides B", enabled=True, locked=False),
        ModEntry(name="Needs B", enabled=True, locked=False),
    ])
    game = _FakeGame(modlist_path.parent)

    needs_b = _info("uuid-needs", "Needs B", deps=("uuid-provides",))
    provides_b = _info("uuid-provides", "Provides B")
    monkeypatch.setattr(
        "Utils.mods.bg3_sort.scan_mod_paks",
        lambda *a, **kw: {"uuid-needs": needs_b, "uuid-provides": provides_b})

    plan = compute_sort_plan(game)

    assert [e.name for e in plan.new_entries] == ["Needs B", "Provides B"]
    assert plan.changed
    assert not plan.unresolved
    assert {m.name for m in plan.moves} == {"Needs B", "Provides B"}


def test_already_correct_order_produces_no_moves(tmp_path, monkeypatch):
    modlist_path = _seed_modlist(tmp_path, [
        ModEntry(name="Needs B", enabled=True, locked=False),
        ModEntry(name="Provides B", enabled=True, locked=False),
    ])
    game = _FakeGame(modlist_path.parent)

    needs_b = _info("uuid-needs", "Needs B", deps=("uuid-provides",))
    provides_b = _info("uuid-provides", "Provides B")
    monkeypatch.setattr(
        "Utils.mods.bg3_sort.scan_mod_paks",
        lambda *a, **kw: {"uuid-needs": needs_b, "uuid-provides": provides_b})

    plan = compute_sort_plan(game)

    assert [e.name for e in plan.new_entries] == ["Needs B", "Provides B"]
    assert not plan.changed
    assert plan.moves == []


def test_separators_disabled_and_unscanned_mods_are_left_untouched(tmp_path, monkeypatch):
    modlist_path = _seed_modlist(tmp_path, [
        ModEntry(name="Provides B", enabled=True, locked=False),
        ModEntry(name="My Section_separator", enabled=True, locked=True,
                 is_separator=True),
        ModEntry(name="Disabled Mod", enabled=False, locked=False),
        ModEntry(name="Loose File Mod", enabled=True, locked=False),
        ModEntry(name="Needs B", enabled=True, locked=False),
    ])
    game = _FakeGame(modlist_path.parent)

    needs_b = _info("uuid-needs", "Needs B", deps=("uuid-provides",))
    provides_b = _info("uuid-provides", "Provides B")
    # "Loose File Mod" has no .pak metadata at all — scan_mod_paks would
    # simply omit it, exactly like a real loose-file/data-only mod.
    monkeypatch.setattr(
        "Utils.mods.bg3_sort.scan_mod_paks",
        lambda *a, **kw: {"uuid-needs": needs_b, "uuid-provides": provides_b})

    plan = compute_sort_plan(game)

    names = [e.name for e in plan.new_entries]
    assert names == ["Needs B", "My Section_separator", "Disabled Mod",
                     "Loose File Mod", "Provides B"]
    moved_names = {m.name for m in plan.moves}
    assert moved_names == {"Needs B", "Provides B"}
    assert "Loose File Mod" not in moved_names


def test_unresolvable_dependency_is_reported(tmp_path, monkeypatch):
    modlist_path = _seed_modlist(tmp_path, [
        ModEntry(name="Needs Missing", enabled=True, locked=False),
    ])
    game = _FakeGame(modlist_path.parent)

    needs_missing = _info("uuid-needs-missing", "Needs Missing",
                          deps=("uuid-does-not-exist",))
    monkeypatch.setattr(
        "Utils.mods.bg3_sort.scan_mod_paks",
        lambda *a, **kw: {"uuid-needs-missing": needs_missing})

    plan = compute_sort_plan(game)

    assert not plan.moves
    assert any("Needs Missing" in line for line in plan.unresolved)


def test_empty_modlist_returns_unchanged_plan(tmp_path):
    modlist_path = _seed_modlist(tmp_path, [])
    game = _FakeGame(modlist_path.parent)
    plan = compute_sort_plan(game)
    assert plan.new_entries == []
    assert not plan.changed

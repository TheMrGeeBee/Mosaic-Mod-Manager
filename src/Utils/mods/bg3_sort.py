"""Neutral (GUI-free) logic for the native BG3 dependency load-order sort.

Computes the same dependency-correct order ``write_modsettings`` already
works out internally at deploy time (see ``Utils.mods.modsettings.
resolve_load_order``), but as an explicit, previewable action that writes the
result back into this profile's ``modlist.txt`` — so the Mods-tab priority
order agrees with what actually gets loaded, instead of the reorder only ever
happening invisibly inside ``modsettings.lsx`` generation.

No tkinter or Qt imports — the Qt view only handles the preview textbox and
the Apply button; all matching/planning logic lives here so it can be
unit-tested headlessly. Modeled directly on ``Utils.mods.bg3_import``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from Utils.mods.bg3_import import resolve_profile_modlist
from Utils.mods.modlist import ModEntry, read_modlist, write_modlist
from Utils.mods.modsettings import (
    BG3ModInfo,
    load_order_eligible,
    resolve_load_order,
    scan_game_data_uuids,
    scan_mod_paks,
)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class SortMove:
    name: str
    old_index: int
    new_index: int
    reason: str


@dataclass
class SortPlan:
    new_entries: list[ModEntry]
    moves: list[SortMove] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    modlist_path: Path | None = None

    @property
    def changed(self) -> bool:
        return bool(self.moves)


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _folder_order(ordered_infos: list[BG3ModInfo]) -> list[str]:
    """Collapse a lowest-priority-first BG3ModInfo order to folder names.

    A single staging folder can own several .pak files; keep the first
    occurrence's position (its highest-priority pak, since a folder's paks
    are inserted together unless a dependency from elsewhere interleaves).
    """
    seen: set[str] = set()
    names: list[str] = []
    for info in ordered_infos:
        if info.source_mod and info.source_mod not in seen:
            seen.add(info.source_mod)
            names.append(info.source_mod)
    return names


def compute_sort_plan(game, profile_name: str = "") -> SortPlan:
    """Read modlist.txt + scan staging + compute the dependency-sorted plan.

    Raises on error (undeterminable profile). Returns a SortPlan whose
    ``new_entries`` equals the current modlist.txt entries, unchanged, when
    there's nothing to reorder (no mods, or already in dependency order).

    Resolves the profile's modlist.txt via ``resolve_profile_modlist`` (the
    active profile, or *profile_name*/last-active as fallbacks) — for a
    caller that already knows the exact modlist.txt path (e.g. deploy(),
    which is passed an explicit *profile* argument rather than relying on
    possibly-stale active-profile state), use
    ``compute_sort_plan_for_modlist`` directly instead.
    """
    modlist_path = resolve_profile_modlist(game, profile_name)
    if modlist_path is None:
        raise RuntimeError("Could not determine the active profile.")
    return compute_sort_plan_for_modlist(game, modlist_path)


def compute_sort_plan_for_modlist(game, modlist_path: Path) -> SortPlan:
    """Same as ``compute_sort_plan``, but for an already-known modlist.txt path."""
    entries = read_modlist(modlist_path)
    enabled = [e for e in entries if e.enabled and not e.is_separator]
    if not enabled:
        return SortPlan(new_entries=list(entries), modlist_path=modlist_path)

    staging = game.get_effective_mod_staging_path()
    try:
        from Utils.profile.profile_state import read_excluded_mod_files
        excluded = {m: set(v) for m, v in
                    read_excluded_mod_files(modlist_path.parent, None).items()}
    except Exception:
        excluded = {}

    mod_infos = scan_mod_paks(staging, enabled, excluded=excluded)
    eligible = load_order_eligible(mod_infos)

    # resolve_load_order expects lowest-priority-first input (modsettings.lsx
    # convention); modlist.txt is highest-priority-first.
    cycles: list[tuple[str, str]] = []
    ordered_infos = resolve_load_order(list(reversed(enabled)), eligible,
                                       cycles=cycles)

    # Lowest-priority-first folder order -> highest-priority-first.
    new_sortable_order = list(reversed(_folder_order(ordered_infos)))

    # Only reorder the subsequence of modlist.txt positions that hold
    # "sortable" mods (enabled, non-separator, has .pak metadata). Every
    # other entry — separators, disabled mods, enabled mods with no .pak
    # metadata (loose-file installs) — keeps its exact original object and
    # index untouched.
    sortable_names = set(new_sortable_order)
    sortable_positions = [
        i for i, e in enumerate(entries)
        if e.enabled and not e.is_separator and e.name in sortable_names
    ]
    mods_by_name = {e.name: e for e in entries if e.name in sortable_names}

    new_entries = list(entries)
    moves: list[SortMove] = []
    if len(sortable_positions) == len(new_sortable_order):
        for list_pos, name in zip(sortable_positions, new_sortable_order):
            new_entries[list_pos] = mods_by_name[name]

        old_index_by_name = {e.name: i for i, e in enumerate(entries)
                             if e.name in sortable_names}
        for list_pos, name in zip(sortable_positions, new_sortable_order):
            old_index = old_index_by_name[name]
            if old_index == list_pos:
                continue
            info = next((i for i in ordered_infos if i.source_mod == name), None)
            reason = "reordered to satisfy dependencies"
            if info is not None and info.dependencies:
                dep_names = sorted({
                    d.name for u in info.dependencies
                    if (d := eligible.get(u)) is not None
                })
                if dep_names:
                    reason = f"depends on {', '.join(dep_names)}"
            moves.append(SortMove(name=name, old_index=old_index,
                                  new_index=list_pos, reason=reason))

    # Unresolved: cycles the sorter had to skip, plus dependencies that
    # aren't satisfied by any installed mod or known base-game/DLC module —
    # mirrors write_modsettings' own missing-dependency check so this agrees
    # with what deploy will actually warn about.
    unresolved: list[str] = []
    cycle_names = sorted({name for _uuid, name in cycles})
    for name in cycle_names:
        unresolved.append(f"{name}: circular dependency — could not be "
                          "fully ordered")

    # Dependencies never reference base-game/system module UUIDs — parse_meta_lsx
    # already strips those out — so mod_infos' own UUIDs plus any base-game/DLC
    # modules discovered on disk are all that's needed here.
    known_uuids = set(mod_infos.keys())
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path is not None:
        known_uuids |= scan_game_data_uuids(game_path / "Data")
    for info in eligible.values():
        missing = [d for d in info.dependencies if d not in known_uuids]
        if missing:
            unresolved.append(f"{info.name}: requires {len(missing)} mod(s) "
                              "that are not installed")

    return SortPlan(new_entries=new_entries, moves=moves,
                    unresolved=unresolved, modlist_path=modlist_path)


# ---------------------------------------------------------------------------
# Preview + apply
# ---------------------------------------------------------------------------

def format_preview(plan: SortPlan) -> tuple[str, str]:
    """Return (summary_line, detail_text) describing *plan* for display."""
    if not plan.moves:
        summary = "Load order already satisfies all known dependencies."
    else:
        summary = f"{len(plan.moves)} mod(s) need to move to satisfy dependencies."
    if plan.unresolved:
        summary += f"   {len(plan.unresolved)} issue(s) could not be resolved."

    lines: list[str] = []
    if plan.moves:
        lines.append("=== MOVES ===")
        for m in sorted(plan.moves, key=lambda m: m.new_index):
            lines.append(f"   {m.name}: position {m.old_index + 1} -> "
                        f"{m.new_index + 1}   ({m.reason})")
    else:
        lines.append("No moves needed.")

    if plan.unresolved:
        lines.append("")
        lines.append("=== UNRESOLVED ===")
        for line in plan.unresolved:
            lines.append(f"   {line}")

    return summary, "\n".join(lines)


def apply_plan(plan: SortPlan) -> Path:
    """Write the new order to the profile's modlist.txt; return its path."""
    write_modlist(plan.modlist_path, plan.new_entries)
    return plan.modlist_path

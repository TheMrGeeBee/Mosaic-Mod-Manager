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
    layer: str = ""


@dataclass
class SortPlan:
    new_entries: list[ModEntry]
    moves: list[SortMove] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    modlist_path: Path | None = None
    # Layered sort only: {mod: (layer id, reason)}, the resulting load order
    # (lowest priority first) and how many mods follow a collection's order.
    layers: dict[str, tuple[str, str]] = field(default_factory=dict)
    load_order: list[str] = field(default_factory=list)
    previous_load_order: list[str] = field(default_factory=list)
    collection_count: int = 0

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


# ---------------------------------------------------------------------------
# Layered sort ("Sort Load Order")
# ---------------------------------------------------------------------------

def compute_layered_plan(game, modlist_path: Path, log_fn=None) -> SortPlan:
    """Full load-order sort: layers (``Utils.mods.bg3_layers``) ordered first
    to last, the current order kept inside each layer, and — stronger than
    layers — dependencies and the user's Load Order Insights decisions.

    A collection profile's manifest-ordered mods are left alone: deploy
    orders them by the manifest regardless of modlist.txt.  Only mods the
    user added are sorted (deploy appends those after the collection's).

    Separators, disabled mods and mods without a load-order entry keep
    their exact modlist slots, like ``compute_sort_plan_for_modlist``.  The
    result satisfies every dependency, so deploy's own dependency sort
    moves nothing afterwards.
    """
    import heapq
    from Utils.mods import bg3_layers as L
    from Utils.mods.bg3_pak_index import (
        INDEX_FILENAME, _looks_like_patch, build_index, collection_mods,
        compute_load_rank, dependents_map, read_manifest, read_rules,
    )

    entries = read_modlist(modlist_path)
    enabled = [e for e in entries if e.enabled and not e.is_separator]
    plan = SortPlan(new_entries=list(entries), modlist_path=modlist_path)
    if not enabled:
        return plan

    profile_dir = modlist_path.parent
    staging = game.get_effective_mod_staging_path()
    index = build_index(staging, [e.name for e in enabled],
                        profile_dir / INDEX_FILENAME, log_fn=log_fn)
    manifest = read_manifest(profile_dir)
    in_collection = collection_mods(index, manifest)
    rank = compute_load_rank(enabled, index, manifest)
    plan.previous_load_order = sorted(rank, key=rank.get)
    modlist_pos = {e.name: i for i, e in enumerate(entries)}

    free = [m for m in rank if m not in in_collection]
    plan.collection_count = len([m for m in rank if m in in_collection])
    free_set = set(free)

    deps = dependents_map(index)
    overrides = L.read_overrides(profile_dir)
    for mod in free:
        category, modio_tags = L.read_categories(staging / mod)
        plan.layers[mod] = L.classify(
            mod, index.get(mod, []), category, modio_tags,
            overrides.get(mod), _looks_like_patch)

    # Hard edges (a -> b: a loads before b).  Dependencies first; then saved
    # decisions (loser before winner), skipped when they'd contradict a
    # dependency chain.
    after: dict[str, set[str]] = {m: set() for m in free}
    before: dict[str, set[str]] = {m: set() for m in free}
    why: dict[tuple[str, str], str] = {}

    def reaches(a: str, b: str) -> bool:
        stack, seen = [a], set()
        while stack:
            cur = stack.pop()
            if cur == b:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(after[cur])
        return False

    for mod in free:
        for d in deps.get(mod, ()):
            if d in free_set and d != mod:
                after[d].add(mod); before[mod].add(d)
                why[(d, mod)] = f"depends on {d}"
    for r in read_rules(profile_dir)["rules"]:
        w, l = r.get("winner"), r.get("loser")
        if w in free_set and l in free_set and w != l and w not in after[l]:
            if reaches(w, l):
                plan.unresolved.append(
                    f"Your decision '{w} wins over {l}' conflicts with a "
                    "dependency and was skipped")
                continue
            after[l].add(w); before[w].add(l)
            why[(l, w)] = (f"you chose: loads after {l}"
                           if r.get("reason") == "load_after"
                           else f"your decision: wins over {l}")

    def key(m: str):
        return (L.LAYER_INDEX[plan.layers[m][0]], rank[m], modlist_pos.get(m, 0), m)

    indeg = {m: len(before[m]) for m in free}
    heap = [key(m) for m in free if indeg[m] == 0]
    heapq.heapify(heap)
    order: list[str] = []
    remaining = set(free)
    while remaining:
        if not heap:
            # Circular constraints: emit the lowest-keyed mod anyway.
            m = min(remaining, key=key)
            plan.unresolved.append(f"{m}: circular dependency or decisions — "
                                   "placed by its layer")
        else:
            m = heapq.heappop(heap)[-1]
            if m not in remaining:
                continue
        remaining.discard(m)
        order.append(m)
        for n in after[m]:
            if n in remaining:
                indeg[n] -= 1
                if indeg[n] == 0:
                    heapq.heappush(heap, key(n))

    # Collection mods keep their place; the sorted mods load after them.
    coll_order = [m for m in sorted(rank, key=rank.get) if m in in_collection]
    plan.load_order = coll_order + order

    # Write back into the free mods' modlist slots (highest priority first).
    slots = [i for i, e in enumerate(entries)
             if e.enabled and not e.is_separator and e.name in free_set]
    by_name = {e.name: e for e in entries}
    new_entries = list(entries)
    for slot, name in zip(slots, reversed(order)):
        new_entries[slot] = by_name[name]
    plan.new_entries = new_entries

    max_layer_so_far = -1
    delayed: dict[str, str] = {}
    for m in order:
        li = L.LAYER_INDEX[plan.layers[m][0]]
        if li < max_layer_so_far and before[m]:
            last = max(before[m], key=order.index)
            delayed[m] = why.get((last, m), "")
        max_layer_so_far = max(max_layer_so_far, li)
    for i, e in enumerate(new_entries):
        if e.name in free_set and modlist_pos.get(e.name) != i:
            layer, reason = plan.layers[e.name]
            text = f"{L.LAYER_LABEL[layer]} ({reason})"
            if delayed.get(e.name):
                text += f"; later than its layer: {delayed[e.name]}"
            plan.moves.append(SortMove(name=e.name,
                                       old_index=modlist_pos[e.name],
                                       new_index=i, reason=text, layer=layer))
    return plan


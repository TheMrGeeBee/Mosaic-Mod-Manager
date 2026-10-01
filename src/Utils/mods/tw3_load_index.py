"""
tw3_load_index.py
TW3 load-order insights: which enabled mods ship the same staged file
(the vanilla-relative path under content/... that TW3's own mods.settings
Priority decides a winner for -- see Utils.mods.tw3_mods_settings), so a
conflict like the real one that started this work (five mods each
replacing game/player/playerWitcher.ws) is visible before launch, not
discovered from a script-compiler error afterward.

Much simpler than BG3's equivalent (Utils.mods.bg3_pak_index): TW3 mods are
loose files already extracted on disk (no pak/archive decompression to
cache), and there is no dependency graph, UUID system, or stats/treasure-
table/UI-template content to parse -- exactly one generic conflict shape
exists here, "same_file" (plus its "identical" -- byte-identical, order
doesn't matter -- special case).

No Qt imports -- the insights view only renders findings and calls
apply_winner / ignore_finding, same contract as BG3's module.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from Utils.mods.modlist import ModEntry, read_modlist, write_modlist
from Utils.mods.tw3_mods_settings import match_manifest, rank_folders
from Utils.mods.tw3_routing import route_path

SEVERITY = {"same_file": 1, "identical": 0}
KIND_LABELS = {
    "same_file": "Ship the same file",
    "identical": "Identical file contents (order does not matter)",
}

_STATE_KEY = "tw3_load_insights"


@dataclass
class Finding:
    kind: str
    mods: list[str]              # highest priority (wins) first
    keys: list[str]               # the conflicting staged relative path(s)
    winner: str | None            # mod whose version currently takes effect
    resolved_by_rule: bool = False
    rule_violated: bool = False
    # A deliberate, persistent "these are mutually-exclusive alternatives,
    # never both enabled on purpose" marking -- see bg3_pak_index.Finding
    # for the full rationale, ported verbatim.
    never_together: bool = False
    # The current winner is already the curator's own intended order.
    intended: bool = False
    intended_by: str = ""         # "collection order"
    note: str = ""

    @property
    def id(self) -> str:
        return f"{self.kind}:" + "|".join(sorted(self.mods))

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.kind, 1)


@dataclass
class Insights:
    findings: list[Finding] = field(default_factory=list)
    load_rank: dict[str, int] = field(default_factory=dict)   # mod -> position
    # Mods a collection manifest orders -- their order can't be changed here.
    collection_mods: set[str] = field(default_factory=set)
    ignored: list[Finding] = field(default_factory=list)
    modlist_path: Path | None = None


@dataclass
class ModScan:
    """One enabled mod's staged content, reduced to what Insights needs.

    *files*: {conflict key ("mods/content/scripts/game/player/
    playerwitcher.ws"): content md5} -- the key is the path TW3 actually
    overrides, i.e. with each mod's own TW3 folder_id segment stripped out
    (mods/<folder_id>/<inner path> -> mods/<inner path>), since that's what
    two DIFFERENT mods, each under their own distinctly-named folder,
    genuinely collide on. Keeping the folder_id in the key would make
    every mod's files trivially unique and never show a real conflict.

    *folder_ids*: the TW3 mods/<id> or dlc/<id> folder names this mod
    would produce on deploy -- needed to look up this mod's Priority via
    tw3_mods_settings.rank_folders, and to match it against a Collection's
    loadOrder via tw3_mods_settings.match_manifest.
    """
    files: dict[str, str] = field(default_factory=dict)
    folder_ids: set[str] = field(default_factory=set)


def _scan_mod(mod_dir: Path) -> ModScan:
    scan = ModScan()
    if not mod_dir.is_dir():
        return scan
    for f in mod_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(mod_dir).as_posix()
        prefix, final_rel = route_path(rel)
        if prefix not in ("mods", "dlc"):
            continue   # bin/ and other root-level files aren't priority conflicts
        parts = final_rel.split("/", 1)
        if len(parts) < 2:
            continue   # a loose file directly in mods/ or dlc/, no folder_id
        folder_id, inner_rel = parts
        if not folder_id:
            continue
        scan.folder_ids.add(folder_id)
        key = f"{prefix}/{inner_rel.lower()}"
        try:
            scan.files[key] = hashlib.md5(f.read_bytes()).hexdigest()
        except OSError:
            continue
    return scan


def build_index(staging: Path, mod_names: list[str]) -> dict[str, ModScan]:
    """{mod_name: ModScan} for every name in *mod_names* (enabled mods).
    Cheap enough (loose files already on disk, no archive decompression)
    to not need a persistent cache, unlike BG3's pak-based index."""
    return {name: _scan_mod(staging / name) for name in mod_names}


def read_manifest(profile_dir: Path) -> "list[dict] | None":
    """A collection profile's curated load order (collection.json
    ``loadOrder``), which deploy's mods.settings writer follows instead of
    modlist order. Same shape/contract as bg3_pak_index.read_manifest."""
    path = profile_dir / "collection.json"
    if not path.is_file():
        return None
    try:
        lo = json.loads(path.read_text(encoding="utf-8")).get("loadOrder")
    except (OSError, ValueError, AttributeError):
        return None
    return lo if isinstance(lo, list) and lo else None


def mod_file_ids(staging: Path, mod_names: list[str]) -> dict[str, int]:
    """{mod_name: Nexus file_id} from each mod's meta.ini (0/absent if
    untracked) -- same source deploy() reads, needed to match a mod
    against a Collection's loadOrder the same precise way
    tw3_mods_settings does (see its module docstring for why folder-id
    string matching alone isn't reliable enough)."""
    from Nexus.nexus_meta import read_meta
    out: dict[str, int] = {}
    for name in mod_names:
        try:
            fid = read_meta(staging / name / "meta.ini").file_id
        except Exception:
            fid = 0
        if fid:
            out[name] = fid
    return out


def _folder_owner_map(index: dict[str, ModScan]) -> dict[str, str]:
    """{folder_id: mod_name} -- the deployed_mods shape
    tw3_mods_settings.match_manifest/rank_folders expect."""
    return {fid: mod for mod, scan in index.items() for fid in scan.folder_ids}


def collection_mods(index: dict[str, ModScan],
                    manifest: "list[dict] | None",
                    file_ids: dict[str, int]) -> set[str]:
    """Mod names whose TW3 folder(s) the collection manifest orders."""
    if not manifest:
        return set()
    deployed = _folder_owner_map(index)
    folder_file_ids = {fid: file_ids[mod] for fid, mod in deployed.items()
                       if mod in file_ids}
    matched, _unmatched = match_manifest(deployed, manifest, folder_file_ids)
    return {deployed[fid] for fid in matched}


def compute_load_rank(index: dict[str, ModScan], modlist_path: Path,
                      manifest: "list[dict] | None",
                      file_ids: dict[str, int]) -> dict[str, int]:
    """Position of each mod in the mods.settings order
    tw3_mods_settings.write_mods_settings would produce, inverted so
    HIGHER = wins (matching bg3_pak_index.compute_load_rank's convention,
    which the rest of this module's winner-picking logic mirrors) --
    tw3_mods_settings itself uses the opposite convention (lowest Priority
    wins, confirmed against the real game engine), so inversion happens
    only here, at the boundary.

    A mod producing more than one TW3 folder (rare -- an author-chosen
    archive wrapper folder holding one leftover ambiguous file alongside
    the mod's real folder, confirmed live) is represented by its BEST
    folder's rank: an approximation, not exact per-file precision, but
    Insights is visibility, not the authoritative Priority calculation --
    that's tw3_mods_settings.write_mods_settings, unaffected by this."""
    deployed = _folder_owner_map(index)
    folder_file_ids = {fid: file_ids[mod] for fid, mod in deployed.items()
                       if mod in file_ids}
    if not deployed:
        return {}
    priority = rank_folders(deployed, modlist_path, manifest, folder_file_ids)
    if not priority:
        return {}
    worst = max(priority.values())
    inverted = {fid: worst - p for fid, p in priority.items()}
    mod_rank: dict[str, int] = {}
    for fid, mod in deployed.items():
        r = inverted.get(fid)
        if r is None:
            continue
        mod_rank[mod] = max(mod_rank.get(mod, -1), r)
    return mod_rank


def _winner(mods: list[str], rank: dict[str, int]) -> "str | None":
    ranked = [m for m in mods if m in rank]
    if len(ranked) != len(mods):
        return None     # someone has no Priority entry at all — unknown
    return max(ranked, key=lambda m: rank[m])


def analyse(enabled: list[ModEntry], index: dict[str, ModScan],
           modlist_path: Path, manifest: "list[dict] | None" = None,
           file_ids: "dict[str, int] | None" = None,
           ) -> tuple[list[Finding], dict[str, int]]:
    """Group every same-path overlap between two or more enabled mods into
    findings. Mirrors bg3_pak_index.analyse's structure, reduced to TW3's
    one generic conflict shape."""
    file_ids = file_ids or {}
    rank = compute_load_rank(index, modlist_path, manifest, file_ids)
    priority = {e.name: i for i, e in enumerate(enabled)}   # 0 = top = wins

    def order(mods) -> list[str]:
        return sorted(set(mods), key=lambda m: priority.get(m, 10**9))

    files: dict[str, dict[str, str]] = defaultdict(dict)   # key -> {mod: hash}
    for mod, scan in index.items():
        if mod not in priority:
            continue
        for key, digest in scan.files.items():
            files[key][mod] = digest

    groups: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
    for key, per_mod in files.items():
        if len(per_mod) < 2:
            continue
        kind = "identical" if len(set(per_mod.values())) == 1 else "same_file"
        groups[(kind, tuple(order(per_mod)))].append(key)

    findings = [Finding(kind=k, mods=list(m), keys=sorted(keys),
                        winner=_winner(list(m), rank))
                for (k, m), keys in groups.items()]

    coll_mods = collection_mods(index, manifest, file_ids) if manifest else set()
    for f in findings:
        if f.winner and coll_mods and all(m in coll_mods for m in f.mods):
            f.intended, f.intended_by = True, "collection order"

    findings.sort(key=lambda f: (f.intended, -f.severity, -len(f.keys), f.mods))
    return findings, rank


# ---------------------------------------------------------------------------
# Rule persistence -- same 3-part shape and helpers as bg3_pak_index, through
# the same Utils.profile.profile_state storage, just a different state key.
# ---------------------------------------------------------------------------

def read_rules(profile_dir: Path) -> dict:
    from Utils.profile.profile_state import read_profile_state
    raw = read_profile_state(profile_dir).get(_STATE_KEY) or {}
    return {"rules": list(raw.get("rules") or []),
            "ignored": list(raw.get("ignored") or []),
            "never_together": [list(g) for g in (raw.get("never_together") or [])]}


def write_rules(profile_dir: Path, data: dict) -> None:
    from Utils.profile.profile_state import _update_key
    _update_key(profile_dir, _STATE_KEY,
                {"rules": data.get("rules", []),
                 "ignored": sorted(set(data.get("ignored", []))),
                 "never_together": data.get("never_together", [])})


def mark_never_together(finding: "Finding", profile_dir: Path) -> None:
    data = read_rules(profile_dir)
    group = sorted(set(finding.mods))
    existing = [sorted(set(g)) for g in data["never_together"]]
    if group not in existing:
        data["never_together"].append(group)
        write_rules(profile_dir, data)


def unmark_never_together(finding: "Finding", profile_dir: Path) -> None:
    data = read_rules(profile_dir)
    group = sorted(set(finding.mods))
    data["never_together"] = [g for g in data["never_together"]
                              if sorted(set(g)) != group]
    write_rules(profile_dir, data)


def clear_decisions(profile_dir: Path) -> None:
    write_rules(profile_dir, {"rules": [], "ignored": [], "never_together": []})


def _apply_never_together_status(findings: "list[Finding]",
                                 groups: "list[list[str]]") -> None:
    group_sets = [frozenset(g) for g in groups]
    for f in findings:
        if frozenset(f.mods) in group_sets:
            f.never_together = True


def _apply_rule_status(findings: list[Finding], rules: list[dict],
                       rank: dict[str, int]) -> None:
    direct = {(r["winner"], r["loser"]) for r in rules
              if r.get("winner") and r.get("loser")}
    beats: dict[str, set[str]] = defaultdict(set)
    for w, l in direct:
        beats[w].add(l)
    pairs: set[tuple[str, str]] = set()
    for start in list(beats):
        seen, stack = set(), list(beats[start])
        while stack:
            m = stack.pop()
            if m in seen or m == start:
                continue
            seen.add(m)
            stack.extend(beats.get(m, ()))
        pairs |= {(start, m) for m in seen}
    for f in findings:
        mine = [(w, l) for (w, l) in pairs if w in f.mods and l in f.mods]
        if not mine:
            continue
        f.resolved_by_rule = any(
            all((w, o) in pairs for o in f.mods if o != w) for w in f.mods)
        f.rule_violated = any(
            w in rank and l in rank and rank[w] < rank[l] for w, l in mine)


def compute_insights(game, profile_dir: Path, log_fn=None) -> Insights:
    modlist_path = profile_dir / "modlist.txt"
    entries = read_modlist(modlist_path)
    enabled = [e for e in entries if e.enabled and not e.is_separator]
    staging = game.get_effective_mod_staging_path()
    index = build_index(staging, [e.name for e in enabled])
    manifest = read_manifest(profile_dir)
    file_ids = mod_file_ids(staging, [e.name for e in enabled])
    findings, rank = analyse(enabled, index, modlist_path, manifest, file_ids)
    state = read_rules(profile_dir)
    _apply_rule_status(findings, state["rules"], rank)
    _apply_never_together_status(findings, state["never_together"])
    ignored_ids = set(state["ignored"])
    return Insights(
        findings=[f for f in findings if f.id not in ignored_ids],
        ignored=[f for f in findings if f.id in ignored_ids],
        load_rank=rank,
        collection_mods=collection_mods(index, manifest, file_ids),
        modlist_path=modlist_path)


def unresolved_count(insights: Insights) -> int:
    """Findings that still need a decision. See
    bg3_pak_index.unresolved_count for the full rationale, ported
    verbatim (TW3 mods are never override-only/outside the load order the
    way a BG3 pak can be, so that exclusion doesn't apply here -- every
    enabled TW3 mod that deploys a mods/ or dlc/ file gets a Priority)."""
    return sum(1 for f in insights.findings
               if f.kind != "identical" and not f.intended
               and not f.never_together
               and (not f.resolved_by_rule or f.rule_violated))


class RuleConflict(Exception):
    """Making this mod win would conflict with the collection's own order."""


def apply_winner(profile_dir: Path, finding: Finding, winner: str,
                 collection: "set[str] | None" = None) -> Path:
    """Record "*winner* beats the others in *finding*" and reorder
    modlist.txt (top = wins) so it loads after all of them. Simpler than
    BG3's equivalent: TW3 has no formal dependency graph, so there is
    nothing to pull along -- only the winner and its direct losers move."""
    losers = [m for m in finding.mods if m != winner]
    if collection and winner in collection and all(l in collection for l in losers):
        raise RuleConflict(
            f"{winner} and {', '.join(losers)} follow your collection's load "
            "order, which deploy uses instead of the mod list — reset or edit "
            "the collection's order to change it.")

    modlist_path = profile_dir / "modlist.txt"
    entries = read_modlist(modlist_path)
    names = [e.name for e in entries]
    if winner in names:
        loser_idx = [names.index(m) for m in losers if m in names]
        w_idx = names.index(winner)
        if loser_idx and w_idx > min(loser_idx):
            entries.insert(min(loser_idx), entries.pop(w_idx))
            write_modlist(modlist_path, entries)

    _save_rules(profile_dir, winner, losers, finding.kind)
    return modlist_path


def _save_rules(profile_dir: Path, winner: str, losers: list[str],
                reason: str) -> None:
    state = read_rules(profile_dir)
    rules = [r for r in state["rules"]
             if not (r.get("winner") in losers and r.get("loser") == winner)
             and not (r.get("winner") == winner and r.get("loser") in losers)]
    rules += [{"winner": winner, "loser": l, "reason": reason} for l in losers]
    state["rules"] = rules
    write_rules(profile_dir, state)


def keep_current_order(profile_dir: Path, finding: Finding) -> str:
    """Accept the finding's current winner as the decision. Nothing moves
    -- for overlaps that already look right in-game."""
    if not finding.winner:
        raise RuleConflict("Nothing is known to win here yet, so there is no "
                           "current order to keep.")
    _save_rules(profile_dir, finding.winner,
                [m for m in finding.mods if m != finding.winner], finding.kind)
    return finding.winner


def ignore_finding(profile_dir: Path, finding: Finding) -> None:
    state = read_rules(profile_dir)
    state["ignored"] = list(set(state["ignored"]) | {finding.id})
    write_rules(profile_dir, state)

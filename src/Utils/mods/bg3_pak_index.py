"""BG3 load-order insights: what each enabled mod's paks actually override.

Mosaic's dependency sort (``Utils.mods.bg3_sort``) only knows what meta.lsx
declares.  Most real ordering problems are invisible to it: two mods defining
the same stats entry, the same treasure table, or overriding the same UI
state — whichever loads *later* in modsettings.lsx wins.  This module reads
the paks' text content (cheap: ~0.6 s for ~600 paks, cached per pak by
size + mtime), groups those overlaps into findings, and tracks the user's
"this mod should win" decisions as per-profile rules.

No Qt imports — the insights view only renders findings and calls
``apply_winner`` / ``ignore_finding``.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from Utils.archives.pak_reader import iter_pak_entries, read_pak_info
from Utils.mods.modlist import ModEntry, read_modlist, write_modlist
from Utils.mods.modsettings import (
    _BUILTIN_FOLDERS,
    _SYSTEM_UUIDS,
    BG3ModInfo,
    _classify_pak_files,
    _is_meta_only_pak,
    _repair_meta_xml,
    load_order_eligible,
    parse_meta_lsx,
    resolve_load_order,
)

INDEX_FILENAME = "bg3_pak_index.json"
_INDEX_VERSION = 6
_STATE_KEY = "bg3_load_insights"

# Files that say nothing about what a mod overrides.
_IGNORED_SUFFIXES = ("meta.lsx", "mod_publish_logo.png", "gui/metadata.lsf",
                     "desktop.ini", "thumbs.db")
_IGNORED_DIRS = ("/.idea/", "/.vscode/", "/.git/")

_STATS_ENTRY_RE = re.compile(r'^\s*new entry\s+"([^"]+)"', re.M)
_STATS_TYPE_RE = re.compile(r'^\s*type\s+"([^"]+)"', re.M)
_TREASURE_RE = re.compile(r'^\s*new treasuretable\s+"([^"]+)"', re.M)
_CAN_MERGE_RE = re.compile(r'^\s*CanMerge\s+1\b', re.M)
# Treasure-table value prefix: the table is merged into, not replaced.
MERGE_PREFIX = "merge:"
# "Compatibility Patch", "Patches_for_X", "Better UI AiO Patch" — but not a
# game-version mention like "(Patch 8)" / "Patch 7-8".
_PATCH_NAME_RE = re.compile(
    r"(?<![a-z])(patch|patches|compat|compatibility)(?![a-z])(?!\s*\d)", re.I)
# Named resources in GUI/Library xaml (item templates, styles…).  When two UI
# mods define the same key, the one loading later replaces the other's — e.g.
# Better Inventory UI's CharacterInventoryTemplate vs ACS's / BCPP's.
# Only real templates/styles count: sizes, colours, brushes and image
# sources are routinely copied between UI mods and don't replace anything
# visible — flagging them made most UI findings noise.
_XAML_KEYED_EL_RE = re.compile(r'<([\w:.]+)\b[^>]*?\bx:Key="([^"]+)"', re.S)
_TEMPLATE_ELEMENTS = {"ControlTemplate", "DataTemplate", "Style",
                      "HierarchicalDataTemplate", "ItemsPanelTemplate"}
_GUI_STATE_RE = re.compile(
    r'<ls:State\s+Name="([^"]+)"[^>]*ModType="Override"', re.I)


# ---------------------------------------------------------------------------
# Per-pak extraction
# ---------------------------------------------------------------------------

def _digest(text: str) -> str:
    """Short, whitespace-insensitive content hash (identical vs. different)."""
    norm = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
    return hashlib.blake2b(norm.encode("utf-8", "ignore"), digest_size=8).hexdigest()


def _split_blocks(text: str, header_re: re.Pattern) -> list[tuple[str, str]]:
    """Split a Larian stats-style text file into (name, block_text) pairs."""
    starts = list(header_re.finditer(text))
    out = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        out.append((m.group(1), text[m.start():end]))
    return out


def _parse_stats(text: str) -> dict[str, str]:
    """{"Type:Name": digest} for every ``new entry`` in a stats .txt file."""
    out: dict[str, str] = {}
    for name, block in _split_blocks(text, _STATS_ENTRY_RE):
        tm = _STATS_TYPE_RE.search(block)
        key = f"{tm.group(1) if tm else '?'}:{name}"
        out[key] = _digest(block)
    return out


def _parse_treasure(text: str) -> dict[str, str]:
    """{name: digest}; tables marked ``CanMerge 1`` get MERGE_PREFIX — the
    game adds their items to the existing table instead of replacing it, so
    two merging mods never conflict."""
    out = {}
    for name, block in _split_blocks(text, _TREASURE_RE):
        prefix = MERGE_PREFIX if _CAN_MERGE_RE.search(block) else ""
        out[name] = prefix + _digest(block)
    return out


def _can_collide(path: str) -> bool:
    """Whether another mod could ship the same path.  Files under a mod's
    own Mods/<Folder>/ or Public/<Folder>/ namespace can't meaningfully
    collide; only base-game folders (Shared, Gustav, Engine, ...) and paths
    outside Mods/Public can.  Keeps the index small (~1% of all paths)."""
    parts = path.replace("\\", "/").lower().split("/")
    if parts[0] == "generated" and len(parts) > 2:
        parts = parts[1:]
    if parts[0] in ("mods", "public") and len(parts) > 2:
        return parts[1] in _BUILTIN_FOLDERS
    return True


def _meta_root(meta_xml: str):
    try:
        return ET.fromstring(meta_xml)
    except ET.ParseError:
        try:
            return ET.fromstring(_repair_meta_xml(meta_xml))
        except ET.ParseError:
            return None


def _parse_dependency_versions(meta_xml: str) -> dict[str, str]:
    """{dependency UUID: the minimum Version64 (or 32-bit Version) the
    meta.lsx Dependencies block asks for}."""
    root = _meta_root(meta_xml)
    out: dict[str, str] = {}
    if root is None:
        return out
    for node in root.iter("node"):
        if node.get("id") != "Dependencies":
            continue
        for child in node.iter("node"):
            if child.get("id") != "ModuleShortDesc":
                continue
            attrs = {a.get("id"): a.get("value") for a in child.iter("attribute")}
            uuid = attrs.get("UUID")
            version = attrs.get("Version64") or attrs.get("Version")
            if uuid and version:
                out[uuid] = version
        break
    return out


def decode_version(value: str | int | None) -> tuple[int, int, int, int]:
    """BG3 packed module version -> (major, minor, revision, build).

    major = v>>55, minor = (v>>47)&0xFF, revision = (v>>31)&0xFFFF,
    build = v&0x7FFFFFFF.  The legacy ``Version`` attribute uses the same
    64-bit layout (see ``modsettings._version64_or_default``); 1 and
    268435456 are historic encodings of 1.0.0.0.
    """
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        return (0, 0, 0, 0)
    if v in (1, 268435456):
        return (1, 0, 0, 0)
    return (v >> 55, (v >> 47) & 0xFF, (v >> 31) & 0xFFFF, v & 0x7FFFFFFF)


def format_version(t: tuple[int, int, int, int]) -> str:
    return ".".join(str(x) for x in t)


def _parse_conflicts(meta_xml: str) -> list[str]:
    """UUIDs listed under meta.lsx's <node id="Conflicts">."""
    try:
        root = ET.fromstring(meta_xml)
    except ET.ParseError:
        try:
            root = ET.fromstring(_repair_meta_xml(meta_xml))
        except ET.ParseError:
            return []
    out = []
    for node in root.iter("node"):
        if node.get("id") != "Conflicts":
            continue
        for child in node.iter("node"):
            if child.get("id") == "ModuleShortDesc":
                for attr in child.iter("attribute"):
                    if attr.get("id") == "UUID" and attr.get("value"):
                        out.append(attr.get("value"))
    return out


_TAGS_RE = re.compile(r'id="Tags"[^>]*?value="([^"]*)"')


def _parse_tags(meta_xml: str) -> list[str]:
    """meta.lsx ModuleInfo ``Tags`` (semicolon-separated), e.g. Library;UI."""
    m = _TAGS_RE.search(meta_xml)
    if not m:
        return []
    return [t.strip() for t in re.split(r"[;,]", m.group(1)) if t.strip()]


def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", "ignore")


def _gui_file_kind(name_lower: str) -> str:
    base = name_lower.rsplit("/", 1)[-1]
    if "controller" in base or base.endswith("_c.xaml"):
        return "Controller"
    return "Keyboard"


def scan_pak(pak: Path) -> dict:
    """Extract everything the insights need from one pak (JSON-serialisable)."""
    info = read_pak_info(pak)
    rec: dict = {"meta": None, "files": [], "stats": {}, "treasure": {},
                 "gui": [], "gui_templates": [], "conflicts": []}

    files = [n for n in info.file_names
             if not n.lower().endswith(_IGNORED_SUFFIXES)
             and not any(d in "/" + n.lower() for d in _IGNORED_DIRS)
             and _can_collide(n)]
    rec["files"] = files

    if info.meta_xml:
        mi = parse_meta_lsx(info.meta_xml)
        if mi is not None:
            ovr, own = _classify_pak_files(info.file_names)
            rec["meta"] = {
                "uuid": mi.uuid, "name": mi.name, "folder": mi.folder,
                "version64": mi.version64, "version": mi.version,
                "md5": mi.md5, "publish_handle": mi.publish_handle,
                "mod_type": mi.mod_type, "dependencies": mi.dependencies,
                "dependency_names": mi.dependency_names,
                "is_override_only": ovr and not own,
                "is_meta_only": _is_meta_only_pak(info.file_names),
                "tags": _parse_tags(info.meta_xml),
                "dependency_versions": _parse_dependency_versions(info.meta_xml),
            }
            rec["conflicts"] = _parse_conflicts(info.meta_xml)

    def want(nl: str) -> bool:
        return (("/stats/generated/data/" in nl and nl.endswith(".txt"))
                or nl.endswith("/stats/generated/treasuretable.txt")
                or ("/gui/statemachines/" in nl and nl.endswith(".xaml"))
                or ("/gui/library/" in nl and nl.endswith(".xaml")))

    for name, data in iter_pak_entries(pak, want):
        if data is None:
            continue
        nl = name.lower()
        text = _decode(data)
        if nl.endswith("treasuretable.txt"):
            rec["treasure"].update(_parse_treasure(text))
        elif nl.endswith(".txt"):
            rec["stats"].update(_parse_stats(text))
        elif "/gui/library/" in nl:
            kind = _gui_file_kind(nl)
            rec["gui_templates"].extend(sorted({
                f"{kind}:{k}" for el, k in _XAML_KEYED_EL_RE.findall(text)
                if el.rsplit(":", 1)[-1] in _TEMPLATE_ELEMENTS}))
        else:
            kind = _gui_file_kind(nl)
            rec["gui"].extend(f"{kind}:{s}" for s in _GUI_STATE_RE.findall(text))
    return rec


# ---------------------------------------------------------------------------
# Cached index
# ---------------------------------------------------------------------------

def _load_index(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == _INDEX_VERSION:
            return data.get("paks", {})
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def build_index(staging: Path, mod_names: list[str], cache_path: Path | None,
                log_fn=None) -> dict[str, list[dict]]:
    """{mod_name: [pak record, ...]} for *mod_names*, reusing cached records
    whose pak size + mtime are unchanged.  Rewrites the cache afterwards."""
    _log = log_fn or (lambda _m: None)
    cached = _load_index(cache_path) if cache_path else {}
    fresh: dict[str, dict] = {}
    out: dict[str, list[dict]] = {}
    rescanned = 0
    for mod in mod_names:
        mod_dir = staging / mod
        if not mod_dir.is_dir():
            continue
        for pak in sorted(mod_dir.rglob("*.pak")):
            try:
                st = pak.stat()
            except OSError:
                continue
            key = str(pak)
            stamp = [st.st_size, st.st_mtime_ns]
            hit = cached.get(key)
            if hit is not None and hit.get("stamp") == stamp:
                rec = hit
            else:
                try:
                    rec = scan_pak(pak)
                except Exception as exc:
                    _log(f"  Could not read {pak.name}: {exc}")
                    continue
                rec["stamp"] = stamp
                rescanned += 1
            rec["rel"] = pak.relative_to(mod_dir).as_posix()
            fresh[key] = rec
            out.setdefault(mod, []).append(rec)
    if cache_path is not None:
        try:
            cache_path.write_text(
                json.dumps({"version": _INDEX_VERSION, "paks": fresh}),
                encoding="utf-8")
        except OSError as exc:
            _log(f"  Could not write {cache_path.name}: {exc}")
    _log(f"  Indexed {len(fresh)} pak(s) ({rescanned} rescanned).")
    return out


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

# How much each kind matters, for sorting the report.
SEVERITY = {"declared_conflict": 3, "known_incompatible": 3,
            "outdated_dependency": 3, "same_module": 3, "author_note": 2,
            "gui_state": 2, "ui_template": 2,
            "stats_override": 2,
            "treasure_table": 2, "variant_group": 2, "same_file": 1,
            "identical": 0}

KIND_LABELS = {
    "declared_conflict": "Declared incompatible (meta.lsx Conflicts)",
    "known_incompatible": "Known incompatible (mod author)",
    "outdated_dependency": "Needs a newer version of a dependency",
    "same_module": "Same module — only one of them is used",
    "author_note": "The mod author says (Nexus page)",
    "variant_group": "Variants of the same mod enabled together",
    "gui_state": "Replace the same UI screen",
    "ui_template": "Replace the same UI templates",
    "stats_override": "Define the same stats entries",
    "treasure_table": "Define the same treasure tables",
    "same_file": "Ship the same file",
    "identical": "Identical definitions (order does not matter)",
}


@dataclass
class Finding:
    kind: str
    mods: list[str]              # highest priority (loads last) first
    keys: list[str]              # e.g. stats entries, UI states, file paths
    winner: str | None           # mod whose version currently takes effect
    resolved_by_rule: bool = False
    rule_violated: bool = False
    # The winner declares a dependency on every loser: it's a patch built on
    # top of them, so the current order is the intended one.
    intended: bool = False
    intended_by: str = ""        # "patch" | "author rule" | "author note"
    # author_note findings: the AuthorSuggestion (bg3_author_notes) behind it.
    suggestion: object = None
    # A mod whose name says it's a patch/compat mod and that overlaps these
    # mods — probably a patch whose author never declared the dependencies.
    # Only a suggestion: the user confirms with accept_patch().
    suggested_patch: str | None = None
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
    depends_on: dict[str, set[str]] = field(default_factory=dict)
    # Mods a collection manifest orders — their order can't be changed here.
    collection_mods: set[str] = field(default_factory=set)
    # meta.lsx UUIDs per mod (for "Copy as known-rule suggestion") and the
    # cached Nexus author notes per mod (detail pane).
    mod_uuids: dict[str, list[str]] = field(default_factory=dict)
    author_notes: dict[str, list[dict]] = field(default_factory=dict)
    ignored: list[Finding] = field(default_factory=list)
    modlist_path: Path | None = None


def _read_group(mod_dir: Path) -> str:
    """info.json "Group" (shared by alternative files of one mod)."""
    p = mod_dir / "info.json"
    if not p.is_file():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        mods = data.get("Mods") or []
        return (mods[0].get("Group") or "") if mods else ""
    except (OSError, ValueError, AttributeError, IndexError):
        return ""


def _to_info(meta: dict, mod: str) -> BG3ModInfo:
    info = BG3ModInfo(
        uuid=meta["uuid"], name=meta["name"], folder=meta["folder"],
        version64=meta["version64"], md5=meta.get("md5", ""),
        publish_handle=meta.get("publish_handle", "0"),
        version=meta.get("version", ""), mod_type=meta.get("mod_type", ""),
        dependencies=list(meta.get("dependencies", [])),
        dependency_names=dict(meta.get("dependency_names", {})),
        source_mod=mod,
    )
    info.is_override_only = bool(meta.get("is_override_only"))
    info.is_meta_only = bool(meta.get("is_meta_only"))
    return info


def read_manifest(profile_dir: Path) -> list[dict] | None:
    """A collection profile's curated load order (collection.json
    ``loadOrder``), which deploy follows instead of modlist order."""
    path = profile_dir / "collection.json"
    if not path.is_file():
        return None
    try:
        lo = json.loads(path.read_text(encoding="utf-8")).get("loadOrder")
    except (OSError, ValueError, AttributeError):
        return None
    return lo if isinstance(lo, list) and lo else None


def collection_mods(index: dict[str, list[dict]],
                    manifest: list[dict] | None) -> set[str]:
    """Mod folders whose paks the collection manifest orders."""
    if not manifest:
        return set()
    uuids = {((e.get("data") or {}).get("uuid") or "").strip().lower()
             for e in manifest}
    uuids.discard("")
    return {mod for mod, recs in index.items()
            for r in recs
            if r.get("meta") and r["meta"]["uuid"].lower() in uuids}


def compute_load_rank(enabled: list[ModEntry],
                      index: dict[str, list[dict]],
                      manifest: list[dict] | None = None) -> dict[str, int]:
    """Position of each mod in the modsettings.lsx order write_modsettings
    would produce (higher = loads later = wins).  Mods with no load-order
    entry (override-only / no meta.lsx) are absent.  With a collection
    *manifest* the order follows it, exactly as deploy does."""
    lowest_first = list(reversed(enabled))
    by_uuid: dict[str, BG3ModInfo] = {}
    for e in lowest_first:
        for rec in index.get(e.name, []):
            meta = rec.get("meta")
            if not meta or meta["uuid"] in _SYSTEM_UUIDS:
                continue
            by_uuid[meta["uuid"]] = _to_info(meta, e.name)
    eligible = load_order_eligible(by_uuid)
    if manifest:
        from Utils.mods.modsettings import _apply_manifest_pak_order
        ordered = _apply_manifest_pak_order(lowest_first, eligible, manifest,
                                            lambda _m: None)
    else:
        ordered = resolve_load_order(lowest_first, eligible)
    rank: dict[str, int] = {}
    for i, info in enumerate(ordered):
        rank[info.source_mod] = max(rank.get(info.source_mod, -1), i)
    return rank


def _winner(mods: list[str], rank: dict[str, int]) -> str | None:
    ranked = [m for m in mods if m in rank]
    if len(ranked) != len(mods):
        return None     # someone loads outside the load order — unknown
    return max(ranked, key=lambda m: rank[m])


def analyse(enabled: list[ModEntry], index: dict[str, list[dict]],
            staging: Path, manifest: list[dict] | None = None,
            known=None, author=None) -> tuple[list[Finding], dict[str, int]]:
    """Group every overlap between two or more enabled mods into findings."""
    rank = compute_load_rank(enabled, index, manifest)
    priority = {e.name: i for i, e in enumerate(enabled)}   # 0 = top = wins

    def order(mods) -> list[str]:
        return sorted(set(mods), key=lambda m: priority.get(m, 10**9))

    # key -> {mod: digest}
    stats: dict[str, dict[str, str]] = defaultdict(dict)
    treasure: dict[str, dict[str, str]] = defaultdict(dict)
    gui: dict[str, set[str]] = defaultdict(set)
    templates: dict[str, set[str]] = defaultdict(set)
    files: dict[str, set[str]] = defaultdict(set)
    uuid_owner: dict[str, str] = {}
    declared: list[tuple[str, str]] = []

    for mod, recs in index.items():
        if mod not in priority:
            continue
        for rec in recs:
            for k, d in rec["stats"].items():
                stats[k][mod] = d
            for k, d in rec["treasure"].items():
                treasure[k][mod] = d
            for s in rec["gui"]:
                gui[s].add(mod)
            for t in rec.get("gui_templates", []):
                templates[t].add(mod)
            for f in rec["files"]:
                files[f.lower()].add(mod)
            meta = rec.get("meta")
            if meta:
                uuid_owner.setdefault(meta["uuid"], mod)
                for c in rec.get("conflicts", []):
                    declared.append((mod, c))

    groups: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)

    def add(kind: str, mods, key: str):
        if len(set(mods)) > 1:
            groups[(kind, tuple(order(mods)))].append(key)

    for table, kind in ((stats, "stats_override"), (treasure, "treasure_table")):
        for key, per_mod in table.items():
            if len(per_mod) < 2:
                continue
            if kind == "treasure_table" and all(
                    d.startswith(MERGE_PREFIX) for d in per_mod.values()):
                continue    # every mod merges into the table — no conflict
            identical = len(set(per_mod.values())) == 1
            add("identical" if identical else kind, per_mod, key)
    for key, mods in gui.items():
        add("gui_state", mods, key)
    for key, mods in templates.items():
        add("ui_template", mods, key)
    for key, mods in files.items():
        add("same_file", mods, key)

    # UI screens follow the same "loads later wins" rule — confirmed in-game:
    # Advanced Character Sheet's inventory (and its Camp Chest button) only
    # showed while ACS loaded after BCPP UW, which replaces the same screen.
    findings = [Finding(kind=k, mods=list(m), keys=sorted(keys),
                        winner=_winner(list(m), rank))
                for (k, m), keys in groups.items()]

    for mod, conflict_uuid in declared:
        other = uuid_owner.get(conflict_uuid)
        if other and other != mod:
            findings.append(Finding(
                kind="declared_conflict", mods=order([mod, other]),
                keys=[conflict_uuid], winner=None,
                note=f"{mod} declares it is incompatible with {other}."))

    by_group: dict[str, list[str]] = defaultdict(list)
    for mod in index:
        if mod in priority:
            g = _read_group(staging / mod)
            if g:
                by_group[g].append(mod)
    for g, mods in by_group.items():
        if len(mods) > 1:
            findings.append(Finding(
                kind="variant_group", mods=order(mods), keys=[g], winner=None,
                note="These share one info.json Group, which usually means "
                     "they are alternative files of the same mod."))

    deps = dependents_map(index)
    known_after = {}
    if known is not None:
        known_after = {(first, then): (reason, source)
                       for first, then, reason, source in known.edges}
    for f in findings:
        if f.winner:
            losers = [m for m in f.mods if m != f.winner]
            if losers and all(l in deps.get(f.winner, set()) for l in losers):
                f.intended, f.intended_by = True, "patch"
            elif losers and all((l, f.winner) in known_after for l in losers):
                f.intended, f.intended_by = True, "author rule"
                reason, source = known_after[(losers[0], f.winner)]
                f.note = (f"{reason[0].upper()}{reason[1:]}."
                          + (f" Source: {source}" if source else ""))
            else:
                against = [(f.winner, l) for l in losers if (f.winner, l) in known_after]
                if against:
                    reason, source = known_after[against[0]]
                    f.note = (f"{reason[0].upper()}{reason[1:]}, but the current "
                              "order does the opposite — Sort Load Order fixes "
                              "it." + (f" Source: {source}" if source else ""))

    if known is not None:
        seen_pairs: set[frozenset] = set()
        for mod, other, note, source in known.incompatible:
            pair = frozenset((mod, other))
            if mod not in priority or other not in priority or pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            findings.append(Finding(
                kind="known_incompatible", mods=order([mod, other]),
                keys=[note or "incompatible"], winner=None,
                note=(f"The author of {mod} says it doesn't work with {other}"
                      + (f": {note}" if note else "") + "."
                      + (f" Source: {source}" if source else ""))))

    findings.extend(_outdated_dependencies(index, priority, order))
    findings.extend(_same_modules(index, priority, order))
    findings.extend(_author_note_findings(author or [], rank, order))

    _suggest_patches(findings, index)

    findings.sort(key=lambda f: (f.intended, -f.severity, -len(f.keys), f.mods))
    return findings, rank


def _author_note_findings(suggestions, rank, order) -> list[Finding]:
    """Findings for the mod authors' own load-order / compatibility notes
    (``bg3_author_notes.suggestions``).  An order note the current load
    order already follows is shown as intended (just information); one it
    doesn't follow needs a decision (Accept makes it a "load after")."""
    out = []
    for sug in suggestions:
        mods = [sug.mod] + [o for o in sug.others if o != sug.mod]
        f = Finding(kind="author_note", mods=order(mods), keys=[sug.sentence],
                    winner=_winner(mods, rank) if all(m in rank for m in mods) else None,
                    note=f"{sug.mod}'s Nexus page: \"{sug.sentence}\""
                         + (f" Source: {sug.url}" if sug.url else ""),
                    suggestion=sug)
        if sug.kind in ("load_after", "load_before") and \
                sug.mod in rank and all(o in rank for o in sug.others):
            if sug.kind == "load_after":
                ok = all(rank[sug.mod] > rank[o] for o in sug.others)
            else:
                ok = all(rank[sug.mod] < rank[o] for o in sug.others)
            if ok:
                f.intended, f.intended_by = True, "author note"
        out.append(f)
    return out


def accept_author_note(profile_dir: Path, finding: Finding,
                       index_deps: dict[str, set[str]] | None = None) -> int:
    """Turn an author_note finding into saved "load after" choices (what the
    author asked for).  Returns how many were saved; incompatibility notes
    have nothing to save and are just acknowledged (ignored)."""
    sug = finding.suggestion
    if sug is None:
        return 0
    if sug.kind == "incompatible":
        ignore_finding(profile_dir, finding)
        return 0
    deps = index_deps or {}
    pairs = ([(sug.mod, o) for o in sug.others] if sug.kind == "load_after"
             else [(o, sug.mod) for o in sug.others])
    for later, earlier in pairs:
        if earlier in _dependents_closure(later, deps):
            raise RuleConflict(f"{earlier} depends on {later}, so it can't load "
                               f"after it as the author note asks.")
    for later, earlier in pairs:
        add_load_after(profile_dir, later, earlier)
    return len(pairs)


def _same_modules(index, priority, order) -> list[Finding]:
    """Enabled mods that are the same module: they ship a .pak with the same
    file name (deploy puts every .pak flat in the Mods folder, so only the
    highest-priority copy is deployed) or the same meta.lsx UUID.  Replacement
    mods do this on purpose — KAVT ships unique_tav.pak with Unique Tav's
    UUID — and so do two installed copies of one mod.  Either way one of them
    silently replaces the other."""
    by_file: dict[str, set[str]] = defaultdict(set)
    by_uuid: dict[str, set[str]] = defaultdict(set)
    uuid_name: dict[str, str] = {}
    for mod, recs in index.items():
        if mod not in priority:
            continue
        for r in recs:
            meta = r.get("meta") or {}
            if meta.get("is_meta_only"):
                continue          # load-order dividers
            rel = (r.get("rel") or "").replace("\\", "/")
            if rel.lower().endswith(".pak"):
                by_file[rel.rsplit("/", 1)[-1].lower()].add(mod)
            if meta.get("uuid") and meta["uuid"] not in _SYSTEM_UUIDS:
                by_uuid[meta["uuid"]].add(mod)
                uuid_name.setdefault(meta["uuid"], meta.get("name", ""))
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for name, mods in by_file.items():
        if len(mods) > 1:
            groups[tuple(order(mods))].append(f"file: {name}")
    for uuid, mods in by_uuid.items():
        if len(mods) > 1:
            groups[tuple(order(mods))].append(
                f"module: {uuid_name.get(uuid) or uuid} ({uuid})")
    out = []
    for mods, keys in groups.items():
        same_file = any(k.startswith("file:") for k in keys)
        winner = mods[0] if same_file else None   # top of the list wins the file
        note = (f"These are the same module, so only one is used"
                + (f": {winner}'s copy is the one deployed, because it's "
                   "higher in your list." if winner else ".")
                + " If one replaces the other (e.g. KAVT replaces Unique Tav), "
                  "disable the one you don't want.")
        out.append(Finding(kind="same_module", mods=list(mods), keys=sorted(keys),
                           winner=winner, note=note))
    return out


def _outdated_dependencies(index, priority, order) -> list[Finding]:
    """Enabled mods whose meta.lsx asks for a newer version of a dependency
    than the one installed (idea from NexusMods.App's BG3 diagnostics)."""
    installed: dict[str, tuple[str, tuple]] = {}
    for mod, recs in index.items():
        if mod not in priority:
            continue
        for r in recs:
            meta = r.get("meta")
            if meta:
                v = meta.get("version64") or meta.get("version")
                installed.setdefault(meta["uuid"], (mod, decode_version(v)))
    out: list[Finding] = []
    for mod, recs in index.items():
        if mod not in priority:
            continue
        for r in recs:
            meta = r.get("meta") or {}
            for dep_uuid, need_raw in (meta.get("dependency_versions") or {}).items():
                if dep_uuid not in installed:
                    continue
                dep_mod, have = installed[dep_uuid]
                need = decode_version(need_raw)
                if dep_mod != mod and have < need:
                    name = meta.get("dependency_names", {}).get(dep_uuid) or dep_mod
                    out.append(Finding(
                        kind="outdated_dependency", mods=order([mod, dep_mod]),
                        keys=[f"{name}: needs {format_version(need)}, "
                              f"installed {format_version(have)}"],
                        winner=None,
                        note=(f"{mod} asks for {name} {format_version(need)} or "
                              f"newer, but {dep_mod} is {format_version(have)}. "
                              "Update it, or it may not work.")))
    return out


def _looks_like_patch(mod: str, recs: list[dict]) -> bool:
    if any((r.get("meta") or {}).get("is_meta_only") for r in recs):
        return False    # load-order dividers carry names like "Compat…"
    names = [mod] + [r["meta"]["name"] for r in recs if r.get("meta")]
    return any(_PATCH_NAME_RE.search(n) for n in names)


def _suggest_patches(findings: list[Finding], index: dict[str, list[dict]]) -> None:
    """Mark findings where exactly one involved mod looks like a patch."""
    for f in findings:
        if f.kind in ("identical", "declared_conflict", "variant_group",
                      "known_incompatible", "outdated_dependency",
                      "same_module", "author_note") \
                or f.intended:
            continue
        patches = [m for m in f.mods if _looks_like_patch(m, index.get(m, []))]
        if len(patches) == 1:
            f.suggested_patch = patches[0]


# ---------------------------------------------------------------------------
# Rules (per profile)
# ---------------------------------------------------------------------------

def read_rules(profile_dir: Path) -> dict:
    from Utils.profile.profile_state import read_profile_state
    raw = read_profile_state(profile_dir).get(_STATE_KEY) or {}
    return {"rules": list(raw.get("rules") or []),
            "ignored": list(raw.get("ignored") or [])}


def write_rules(profile_dir: Path, data: dict) -> None:
    from Utils.profile.profile_state import _update_key
    _update_key(profile_dir, _STATE_KEY,
                {"rules": data.get("rules", []),
                 "ignored": sorted(set(data.get("ignored", [])))})


def _apply_rule_status(findings: list[Finding], rules: list[dict],
                       rank: dict[str, int]) -> None:
    direct = {(r["winner"], r["loser"]) for r in rules
              if r.get("winner") and r.get("loser")}
    # Transitive: A beats B and B beats C means A beats C.
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
        # Decided only when one mod has a rule over every other mod here —
        # a rule covering two of four mods doesn't settle the finding.
        f.resolved_by_rule = any(
            all((w, o) in pairs for o in f.mods if o != w) for w in f.mods)
        f.rule_violated = any(
            w in rank and l in rank and rank[w] < rank[l] for w, l in mine)


# ---------------------------------------------------------------------------
# Entry points used by the view
# ---------------------------------------------------------------------------

def compute_insights(game, profile_dir: Path, log_fn=None) -> Insights:
    modlist_path = profile_dir / "modlist.txt"
    entries = read_modlist(modlist_path)
    enabled = [e for e in entries if e.enabled and not e.is_separator]
    staging = game.get_effective_mod_staging_path()
    index = build_index(staging, [e.name for e in enabled],
                        profile_dir / INDEX_FILENAME, log_fn=log_fn)
    manifest = read_manifest(profile_dir)
    from Utils.mods import bg3_known_rules as K
    known = K.resolve(K.load_rules(log_fn=log_fn), index, {e.name for e in enabled})
    author = author_suggestions(game, index, enabled, staging)
    notes_by_mod = cached_author_notes(game, enabled, staging)
    findings, rank = analyse(enabled, index, staging, manifest, known, author)
    state = read_rules(profile_dir)
    _apply_rule_status(findings, state["rules"], rank)
    ignored_ids = set(state["ignored"])
    return Insights(
        findings=[f for f in findings if f.id not in ignored_ids],
        ignored=[f for f in findings if f.id in ignored_ids],
        load_rank=rank, depends_on=dependents_map(index),
        collection_mods=collection_mods(index, manifest),
        mod_uuids={m: [r["meta"]["uuid"] for r in recs if r.get("meta")]
                   for m, recs in index.items()},
        author_notes=notes_by_mod,
        modlist_path=modlist_path)


def author_suggestions(game, index, enabled, staging) -> list:
    """Suggestions from the *cached* Nexus author notes — never touches the
    network (views refresh the cache; deploy only reads it)."""
    try:
        from Nexus.nexus_author_notes import read_cache
        from Utils.mods.bg3_author_notes import suggestions
        domain = getattr(game, "nexus_game_domain", "") or "baldursgate3"
        return suggestions(index, [e.name for e in enabled], staging,
                           read_cache(domain), domain)
    except Exception:
        return []


def cached_author_notes(game, enabled, staging) -> dict[str, list[dict]]:
    """{mod: its Nexus page's load-order/compatibility sentences} from the
    local cache only."""
    try:
        from Nexus.nexus_author_notes import read_cache
        from Utils.mods.bg3_author_notes import nexus_ids
        domain = getattr(game, "nexus_game_domain", "") or "baldursgate3"
        cache = read_cache(domain)
        out = {}
        for mod, mid in nexus_ids(staging, [e.name for e in enabled]).items():
            entry = cache.get(str(mid)) or {}
            sentences = list(entry.get("sentences", [])) + list(entry.get("requirement_notes", []))
            if sentences:
                out[mod] = sentences
        return out
    except Exception:
        return {}


def refresh_author_notes(game, profile_dir: Path, force: bool = False,
                         api=None, log_fn=None) -> int:
    """Fetch Nexus author notes for the profile's enabled Nexus mods (stale
    or missing ones only, unless *force*).  Returns how many mods have
    notes afterwards.  Network — call from a worker thread."""
    from Nexus.nexus_author_notes import fetch_notes
    from Utils.mods.bg3_author_notes import nexus_ids
    entries = read_modlist(profile_dir / "modlist.txt")
    names = [e.name for e in entries if e.enabled and not e.is_separator]
    ids = nexus_ids(game.get_effective_mod_staging_path(), names)
    domain = getattr(game, "nexus_game_domain", "") or "baldursgate3"
    cache = fetch_notes(domain, list(ids.values()), force=force, api=api,
                        log_fn=log_fn)
    return sum(1 for e in cache.values() if e.get("sentences"))


def known_rule_suggestion(finding: Finding, mod_uuids: dict[str, list[str]]) -> str:
    """A ready-to-paste bg3_known_rules.json entry for an author_note finding."""
    sug = finding.suggestion
    if sug is None:
        return ""
    def ref(m):
        return {"name": m, "match": {"uuids": mod_uuids.get(m, [])[:1] or [],
                                     "names": [m] if not mod_uuids.get(m) else []}}
    entry = {"name": sug.mod, "match": ref(sug.mod)["match"],
             "note": sug.sentence, "source": sug.url}
    key = {"load_after": "after", "load_before": "before",
           "incompatible": "incompatible"}[sug.kind]
    entry[key] = [ref(o) for o in sug.others]
    return json.dumps(entry, ensure_ascii=False, indent=2)


def unresolved_count(insights: Insights) -> int:
    """Findings that still need a decision (not identical, no rule yet, or a
    rule the current order breaks)."""
    return sum(1 for f in insights.findings
               if f.kind != "identical" and not f.intended
               and (not f.resolved_by_rule or f.rule_violated))


class RuleConflict(Exception):
    """Making this mod win would break a declared dependency."""


def _dependents_closure(mod: str, deps: dict[str, set[str]]) -> set[str]:
    """Every mod that depends on *mod*, directly or through other mods."""
    out: set[str] = set()
    stack = [mod]
    while stack:
        cur = stack.pop()
        for m, ds in deps.items():
            if cur in ds and m not in out:
                out.add(m)
                stack.append(m)
    return out


def apply_winner(profile_dir: Path, finding: Finding, winner: str,
                 index_deps: dict[str, set[str]] | None = None,
                 collection: set[str] | None = None) -> Path:
    """Record "*winner* beats the others in *finding*" and reorder
    modlist.txt (top = wins) so it loads after all of them.

    The winner moves directly above the highest-placed loser.  But a mod
    that others depend on is pulled forward to load before its first
    dependent (e.g. Goon's Library depends on Interrupted Music Performance
    Fixer, so the Fixer loads early whatever its own position), so each loser
    is also moved below the winner and everything that depends on it —
    only then does it really load first.  Nothing else moves."""
    losers = [m for m in finding.mods if m != winner]
    if collection and winner in collection and all(l in collection for l in losers):
        raise RuleConflict(
            f"{winner} and {', '.join(losers)} follow your collection's load "
            "order, which deploy uses instead of the mod list — reset or edit "
            "the collection's order to change it.")
    deps = index_deps or {}
    pulls_winner = _dependents_closure(winner, deps)
    for loser in losers:
        if loser in pulls_winner:
            raise RuleConflict(
                f"{loser} depends on {winner}, so {winner} must load "
                f"before it and cannot win.")

    modlist_path = profile_dir / "modlist.txt"
    entries = read_modlist(modlist_path)
    names = [e.name for e in entries]
    changed = False
    if winner in names:
        loser_idx = [names.index(m) for m in losers if m in names]
        w_idx = names.index(winner)
        if loser_idx and w_idx > min(loser_idx):
            entries.insert(min(loser_idx), entries.pop(w_idx))
            changed = True
        # Losers go below the lowest-placed of: the winner and every mod
        # that (transitively) depends on it.
        for loser in losers:
            names = [e.name for e in entries]
            if loser not in names:
                continue
            anchors = [names.index(m) for m in pulls_winner | {winner}
                       if m in names]
            floor = max(anchors)
            l_idx = names.index(loser)
            if l_idx < floor:
                entry = entries.pop(l_idx)
                entries.insert(floor, entry)   # floor shifted up by the pop
                changed = True
    if changed:
        write_modlist(modlist_path, entries)

    _save_rules(profile_dir, winner, losers, finding.kind)
    return modlist_path


def _save_rules(profile_dir: Path, winner: str, losers: list[str],
                reason: str) -> None:
    """Store "*winner* loads after each of *losers*", replacing any earlier
    decision about the same pairs (in either direction)."""
    state = read_rules(profile_dir)
    rules = [r for r in state["rules"]
             if not (r.get("winner") in losers and r.get("loser") == winner)
             and not (r.get("winner") == winner and r.get("loser") in losers)]
    rules += [{"winner": winner, "loser": l, "reason": reason} for l in losers]
    state["rules"] = rules
    write_rules(profile_dir, state)


def keep_current_order(profile_dir: Path, finding: Finding) -> str:
    """Accept the finding's current winner as the decision.  Nothing moves —
    for overlaps that already look right in-game.  Returns the winner."""
    if not finding.winner:
        raise RuleConflict("Nothing is known to win here yet, so there is no "
                           "current order to keep.")
    _save_rules(profile_dir, finding.winner,
                [m for m in finding.mods if m != finding.winner], finding.kind)
    return finding.winner


LOAD_AFTER = "load_after"


def add_load_after(profile_dir: Path, mod: str, after: str) -> None:
    """User choice from Sort Load Order: *mod* always loads after *after*."""
    if mod == after:
        raise RuleConflict("A mod can't load after itself.")
    _save_rules(profile_dir, mod, [after], LOAD_AFTER)


def load_after_of(profile_dir: Path, mod: str) -> list[str]:
    return [r["loser"] for r in read_rules(profile_dir)["rules"]
            if r.get("winner") == mod and r.get("reason") == LOAD_AFTER]


def clear_load_after(profile_dir: Path, mod: str) -> int:
    """Remove every "load after" choice made for *mod*; returns how many."""
    state = read_rules(profile_dir)
    keep = [r for r in state["rules"]
            if not (r.get("winner") == mod and r.get("reason") == LOAD_AFTER)]
    removed = len(state["rules"]) - len(keep)
    state["rules"] = keep
    write_rules(profile_dir, state)
    return removed


def accept_patch(profile_dir: Path, patch: str, findings: list[Finding],
                 index_deps: dict[str, set[str]] | None = None,
                 collection: set[str] | None = None) -> int:
    """Make *patch* win every finding it was suggested for (one click
    instead of one decision per finding).  Returns how many were applied."""
    todo = [f for f in findings if f.suggested_patch == patch]
    for f in todo:
        apply_winner(profile_dir, f, patch, index_deps, collection)
    return len(todo)


def settle_modlist(game, profile_dir: Path) -> int:
    """Run the dependency sort deploy would run, right away.

    "Make it win" moves as few mods as possible, which can leave modlist.txt
    out of step with the real load order (a mod others depend on loads
    earlier than its list position).  The next deploy then renumbers many
    entries and logs a long "reordering" block.  Doing it here keeps the
    list tidy; it never changes the load order itself.  Returns the moves."""
    from Utils.mods.bg3_sort import apply_plan, compute_sort_plan_for_modlist
    plan = compute_sort_plan_for_modlist(game, profile_dir / "modlist.txt")
    if plan.changed:
        apply_plan(plan)
    return len(plan.moves)


def broken_rules(game, profile_dir: Path,
                 entries: list[ModEntry] | None = None) -> list[dict]:
    """Saved decisions the given modlist order (default: the current one)
    breaks — the winner would load before the loser."""
    if entries is None:
        entries = read_modlist(profile_dir / "modlist.txt")
    enabled = [e for e in entries if e.enabled and not e.is_separator]
    rules = read_rules(profile_dir)["rules"]
    if not rules or not enabled:
        return []
    index = build_index(game.get_effective_mod_staging_path(),
                        [e.name for e in enabled], profile_dir / INDEX_FILENAME)
    rank = compute_load_rank(enabled, index, read_manifest(profile_dir))
    return [r for r in rules
            if r.get("winner") in rank and r.get("loser") in rank
            and rank[r["winner"]] < rank[r["loser"]]]


def reapply_rules(game, profile_dir: Path, max_passes: int = 5
                  ) -> tuple[int, list[str]]:
    """Make every saved decision hold again (e.g. after importing a BG3MM
    or volo order).  Returns (decisions re-applied, problems).  A few passes
    are allowed because fixing one decision can move a mod another one
    involves; anything still broken after that is reported, not forced."""
    applied = 0
    problems: list[str] = []
    for _ in range(max_passes):
        broken = broken_rules(game, profile_dir)
        if not broken:
            break
        index = build_index(game.get_effective_mod_staging_path(),
                            [e.name for e in read_modlist(profile_dir / "modlist.txt")
                             if e.enabled and not e.is_separator],
                            profile_dir / INDEX_FILENAME)
        deps = dependents_map(index)
        coll = collection_mods(index, read_manifest(profile_dir))
        for r in broken:
            f = Finding(kind=r.get("reason", "stats_override"),
                        mods=[r["winner"], r["loser"]], keys=[], winner=None)
            try:
                apply_winner(profile_dir, f, r["winner"], deps, coll)
                applied += 1
            except RuleConflict as exc:
                problems.append(str(exc))
    else:
        still = broken_rules(game, profile_dir)
        problems += [f"{r['winner']} could not be made to load after {r['loser']}"
                     for r in still]
    settle_modlist(game, profile_dir)
    return applied, sorted(set(problems))


def ignore_finding(profile_dir: Path, finding: Finding) -> None:
    state = read_rules(profile_dir)
    state["ignored"] = list(set(state["ignored"]) | {finding.id})
    write_rules(profile_dir, state)


def dependents_map(index: dict[str, list[dict]]) -> dict[str, set[str]]:
    """{mod: {mods it depends on}} from the indexed meta.lsx dependencies."""
    owner: dict[str, str] = {}
    for mod, recs in index.items():
        for rec in recs:
            if rec.get("meta"):
                owner.setdefault(rec["meta"]["uuid"], mod)
    out: dict[str, set[str]] = defaultdict(set)
    for mod, recs in index.items():
        for rec in recs:
            for dep in (rec.get("meta") or {}).get("dependencies", []):
                if dep in owner and owner[dep] != mod:
                    out[mod].add(owner[dep])
    return out

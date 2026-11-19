"""
omw_script_order.py
Infer load order between OpenMW ``.omwscripts`` files from the Lua they run.

An .omwscripts file has no masters list, but a script mod can *use* another mod's Lua
interface (``I.ErnPerkFramework``), which only exists once the providing script has
loaded. We read each .omwscripts, scan the Lua it names for the interfaces it provides
(``interfaceName = "X"``) and uses (``I.X``), and move a user to just behind its
provider when the profile has it in front. Everything else keeps its place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

OMWSCRIPTS_EXT = ".omwscripts"

# ``interfaceName = "X"`` / ``interfaceName='X'`` in a script's returned table.
_PROVIDES_RE = re.compile(r"""\binterfaceName\s*=\s*["']([A-Za-z_][\w]*)["']""")
# ``I.X`` and ``interfaces.X`` (the alias most scripts bind openmw.interfaces to), plus
# ``require('openmw.interfaces').X`` and ``require("openmw.interfaces")["X"]``.
_USES_RES = (
    re.compile(r"\b(?:I|interfaces)\.([A-Za-z_]\w*)"),
    re.compile(r"""require\s*\(?\s*["']openmw\.interfaces["']\s*\)?\s*\.\s*([A-Za-z_]\w*)"""),
    re.compile(r"""require\s*\(?\s*["']openmw\.interfaces["']\s*\)?\s*\[\s*["']([A-Za-z_]\w*)["']\s*\]"""),
)
# Lua line comments are stripped before scanning so commented-out calls don't count.
_LUA_COMMENT_RE = re.compile(r"--(?!\[\[).*")


def is_omwscripts(name: str) -> bool:
    return name.lower().endswith(OMWSCRIPTS_EXT)


def parse_omwscripts(path: Path) -> list[str]:
    """Lua paths named by an .omwscripts file (``GLOBAL, PLAYER: scripts/x.lua``).

    Flags before the colon are ignored; blank lines and ``#`` comments are skipped.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        lua = line.split(":", 1)[1].strip()
        if lua:
            out.append(lua.replace("\\", "/"))
    return out


def scan_lua(text: str) -> "tuple[set[str], set[str]]":
    """Return ``(provides, uses)`` interface names found in Lua source."""
    text = _LUA_COMMENT_RE.sub("", text)
    provides = set(_PROVIDES_RE.findall(text))
    uses: set[str] = set()
    for rx in _USES_RES:
        uses.update(rx.findall(text))
    # Calling a script's own interface from inside itself is not a dependency.
    return provides, uses - provides


@dataclass
class ScriptInfo:
    provides: set[str]
    uses: set[str]


@dataclass
class Move:
    name: str       # the script file that moved
    after: str      # the script file it now follows
    because: str    # the interface that links them


def scan_script_file(omwscripts: Path, resolve_lua: Callable[[str], "Path | None"]) -> ScriptInfo:
    """Union of provides/uses over every Lua file an .omwscripts names.

    *resolve_lua* maps a Lua path from the file ("scripts/x/y.lua") to a real path, or
    None when it cannot be found (that Lua is skipped).
    """
    provides: set[str] = set()
    uses: set[str] = set()
    for rel in parse_omwscripts(omwscripts):
        lua = resolve_lua(rel)
        if lua is None:
            continue
        try:
            text = Path(lua).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        p, u = scan_lua(text)
        provides |= p
        uses |= u
    return ScriptInfo(provides, uses - provides)


def order_scripts(
    plugin_names: Iterable[str],
    scan: Callable[[str], "ScriptInfo | None"],
) -> "tuple[list[str], list[Move], list[str]]":
    """Reorder the ``.omwscripts`` entries of *plugin_names* so providers load first.

    Non-script entries never move, and scripts keep their relative order unless a
    dependency forces a change. *scan(name)* returns the ScriptInfo of one script
    file (None = unknown, left alone).

    Returns ``(new_order, moves, cycle_notes)``. A script is only ever moved to just
    after the last provider it depends on, so unrelated entries are not shuffled.
    """
    names = list(plugin_names)
    info: dict[str, ScriptInfo] = {}
    for n in names:
        if is_omwscripts(n):
            s = scan(n)
            if s is not None:
                info[n] = s
    if not any(s.uses for s in info.values()):
        return names, [], []

    providers: dict[str, list[str]] = {}
    for n, s in info.items():
        for iface in s.provides:
            providers.setdefault(iface, []).append(n)

    order = list(names)
    moves: list[Move] = []
    notes: list[str] = []
    # Each pass fixes one violation; a dependency chain needs at most len(scripts)
    # passes, so the bound also stops a dependency cycle from looping.
    for _ in range(len(info) + 1):
        moved = False
        pos = {n: i for i, n in enumerate(order)}
        for n in [x for x in order if x in info]:
            blockers = [
                (p, iface)
                for iface in sorted(info[n].uses)
                for p in providers.get(iface, ())
                if p != n and pos[p] > pos[n]
            ]
            if not blockers:
                continue
            last, iface = max(blockers, key=lambda b: pos[b[0]])
            order.remove(n)
            order.insert(order.index(last) + 1, n)
            moves.append(Move(n, last, iface))
            moved = True
            break
        if not moved:
            return order, moves, notes
    # Still violated after the bound: the scripts depend on each other in a loop.
    pos = {n: i for i, n in enumerate(order)}
    for n, s in info.items():
        for iface in sorted(s.uses):
            for p in providers.get(iface, ()):
                if p != n and pos[p] > pos[n]:
                    notes.append(f"{n} and {p} depend on each other ({iface}); left as is")
    return order, moves, sorted(set(notes))


def lua_resolver(data_dirs: Iterable[Path]) -> Callable[[str], "Path | None"]:
    """Resolve a Lua path against data directories, ignoring case (later dirs win)."""
    dirs = [Path(d) for d in data_dirs]

    def resolve(rel: str) -> "Path | None":
        for d in reversed(dirs):
            cur = d
            for part in rel.split("/"):
                if not part:
                    continue
                if (cur / part).exists():
                    cur = cur / part
                    continue
                try:
                    match = next((c for c in cur.iterdir() if c.name.lower() == part.lower()), None)
                except OSError:
                    match = None
                if match is None:
                    cur = None
                    break
                cur = match
            if cur is not None and cur.is_file():
                return cur
        return None

    return resolve


def make_orderer(data_dirs: Iterable[Path]) -> Callable[[list[str]], "tuple[list[str], list[Move], list[str]]"]:
    """Build the ``script_orderer`` callable used by openmw_cfg.update_openmw_cfg."""
    dirs = [Path(d) for d in data_dirs]
    resolve = lua_resolver(dirs)

    def scan(name: str) -> "ScriptInfo | None":
        for d in reversed(dirs):
            f = d / name
            if f.is_file():
                return scan_script_file(f, resolve)
        return None

    def orderer(names: list[str]):
        return order_scripts(names, scan)

    return orderer

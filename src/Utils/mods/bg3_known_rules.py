"""Known load-order rules for Baldur's Gate 3 — a small curated list.

Some ordering advice exists only on mod pages ("install Better Inventory UI
after Better Containers, BCPP and Better Hotbar 2"), not in any file Mosaic
can read.  ``bg3_known_rules.json`` (next to this module) collects such rules;
it is bundled, and refreshed from Mosaic's own ``main`` branch at most once a
day so a new rule reaches users without a release (the same pattern as the
Nexus requirement filter in ``Nexus/nexus_requirements.py``).

Strength (applied by ``bg3_sort`` / ``bg3_pak_index``): collection order,
dependencies and the user's own decisions beat these rules; these beat the
automatic layers.

No Qt imports.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from Utils.mods.bg3_requirements import normalize_requirement_name

BUNDLED_PATH = Path(__file__).with_name("bg3_known_rules.json")
REMOTE_URL = ("https://raw.githubusercontent.com/TheMrGeeBee/Mosaic-Mod-Manager/"
              "main/src/Utils/mods/bg3_known_rules.json")
_REFRESH_SECONDS = 24 * 3600

_lock = threading.Lock()
_refresh_started = False


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    from Utils.config_paths import get_config_dir
    return get_config_dir() / "bg3_known_rules.json"


def _parse(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("mods"), list):
        return None
    return data


def _read(path: Path) -> dict | None:
    try:
        return _parse(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _refresh_worker(log_fn) -> None:
    try:
        from Utils.gh_cache import fetch_text
        text = fetch_text(REMOTE_URL, accept="text/plain",
                          min_interval=_REFRESH_SECONDS)
    except Exception as exc:
        log_fn(f"Known BG3 rules: refresh failed ({exc}); using local copy.")
        return
    if not text or _parse(text) is None:
        return
    try:
        cache = _cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.is_file() or cache.read_text(encoding="utf-8") != text:
            cache.write_text(text, encoding="utf-8")
    except OSError as exc:
        log_fn(f"Known BG3 rules: could not cache update ({exc}).")


def start_refresh(log_fn=None) -> None:
    """Refresh the cached copy from ``main`` in the background (once per run;
    the fetch itself is throttled to once a day)."""
    global _refresh_started
    with _lock:
        if _refresh_started:
            return
        _refresh_started = True
    threading.Thread(target=_refresh_worker, args=(log_fn or (lambda _m: None),),
                     daemon=True, name="bg3-known-rules").start()


def load_rules(refresh: bool = True, log_fn=None) -> dict:
    """The bundled rule file, or the copy fetched from ``main`` when that
    one has a strictly higher ``version`` — so every change to the rules
    must bump ``version``, and an older ``main`` copy never hides newer
    bundled rules.  Never raises."""
    if refresh:
        start_refresh(log_fn)
    bundled = _read(BUNDLED_PATH) or {"version": 0, "mods": []}
    cached = _read(_cache_path())
    try:
        newer = cached and int(cached.get("version", 0)) > int(bundled.get("version", 0))
    except (TypeError, ValueError):
        newer = False
    return cached if newer else bundled


# ---------------------------------------------------------------------------
# Matching installed mods
# ---------------------------------------------------------------------------

@dataclass
class ResolvedRules:
    layer_pins: dict[str, tuple[str, str]] = field(default_factory=dict)  # mod -> (layer, reason)
    # (loads_first, loads_after, reason, source)
    edges: list[tuple[str, str, str, str]] = field(default_factory=list)
    # (mod, other, note, source)
    incompatible: list[tuple[str, str, str, str]] = field(default_factory=list)


def _matcher(index: dict[str, list[dict]]):
    """Return ``find(match_spec) -> [mod folder, ...]`` over installed mods."""
    by_uuid: dict[str, set[str]] = {}
    names: dict[str, list[str]] = {}
    for mod, recs in index.items():
        keys = {normalize_requirement_name(mod)}
        for r in recs:
            meta = r.get("meta")
            if meta:
                by_uuid.setdefault(meta["uuid"].lower(), set()).add(mod)
                keys.add(normalize_requirement_name(meta["name"]))
        names[mod] = [k for k in keys if k]

    def find(spec: dict) -> list[str]:
        spec = spec or {}
        found: set[str] = set()
        for u in spec.get("uuids", []) or []:
            found |= by_uuid.get(str(u).lower(), set())
        frags = [normalize_requirement_name(n) for n in spec.get("names", []) or []]
        frags = [f for f in frags if f]
        if frags:
            for mod, keys in names.items():
                if any(f in k for f in frags for k in keys):
                    found.add(mod)
        return sorted(found)
    return find


def resolve(rules: dict, index: dict[str, list[dict]],
            enabled: set[str] | None = None) -> ResolvedRules:
    """Map the rule file onto installed (optionally only *enabled*) mods."""
    from Utils.mods.bg3_layers import LAYER_INDEX
    find = _matcher(index)
    live = (lambda m: m in enabled) if enabled is not None else (lambda m: True)
    out = ResolvedRules()
    for entry in rules.get("mods", []):
        if not isinstance(entry, dict):
            continue
        mods = [m for m in find(entry.get("match")) if live(m)]
        if not mods:
            continue
        label = entry.get("name") or mods[0]
        source = entry.get("source", "")
        layer = entry.get("layer")
        if layer in LAYER_INDEX:
            for m in mods:
                out.layer_pins[m] = (layer, "known mod")
        for key, first_is_other in (("after", True), ("before", False)):
            for other in entry.get(key, []) or []:
                if not isinstance(other, dict):
                    continue
                others = [o for o in find(other.get("match")) if live(o)]
                oname = other.get("name") or (others[0] if others else "")
                for m in mods:
                    for o in others:
                        if o == m:
                            continue
                        reason = (f"author rule: {label} loads after {oname}"
                                  if first_is_other else
                                  f"author rule: {label} loads before {oname}")
                        out.edges.append((o, m, reason, source) if first_is_other
                                         else (m, o, reason, source))
        for other in entry.get("incompatible", []) or []:
            if not isinstance(other, dict):
                continue
            note = other.get("note", "")
            for o in [o for o in find(other.get("match")) if live(o)]:
                for m in mods:
                    if o != m:
                        out.incompatible.append((m, o, note, source))
    return out

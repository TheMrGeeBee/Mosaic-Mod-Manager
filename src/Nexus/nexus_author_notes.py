"""Author notes: the load-order / compatibility sentences on a mod's Nexus page.

Advice like "install Better Inventory UI after Better Containers, BCPP and
Better Hotbar 2" or "Not compatible with Better Arrow Icons" usually lives
only in a mod page's description, not in any file.  This module fetches the
descriptions (and requirement notes) of installed Nexus mods, keeps just the
relevant sentences, and caches them per game.

The data is public: Nexus's GraphQL ``legacyModsByDomain`` answers without a
login.  One request covers up to 20 mods.  Only the extracted sentences are
cached (``<config>/nexus_author_notes/<game_domain>.json``), refreshed after
7 days.  Game-agnostic; ``Utils.mods.bg3_author_notes`` turns them into BG3
load-order suggestions.  No Qt imports.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from pathlib import Path

_BATCH = 20
_MAX_AGE = 7 * 24 * 3600
_MAX_SENTENCES = 12
_lock = threading.Lock()

_QUERY = """
query AuthorNotes($ids: [CompositeDomainWithIdInput!]!) {
    legacyModsByDomain(ids: $ids) {
        nodes {
            modId
            name
            updatedAt
            description
            modRequirements { nexusRequirements { nodes { modId modName notes } } }
        }
    }
}
"""

# ---------------------------------------------------------------------------
# Extraction (pure)
# ---------------------------------------------------------------------------

_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"\[/?[a-z*][^\]\n]{0,80}\]|<[^>\n]{1,200}>", re.I)
_SPLIT_RE = re.compile(r"\n+|(?<=[.!?])\s+(?=[A-Z0-9\"'(])")

KIND_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("incompatible", re.compile(
        r"not\s+compatible|incompatib|conflicts?\s+with|doesn'?t\s+work\s+with|"
        r"does\s+not\s+work\s+with|won'?t\s+work\s+with", re.I)),
    ("order", re.compile(
        r"load\s*order|\bload(?:s|ed|ing)?\s+(?:it\s+)?(?:after|before|below|above|"
        r"last|first|lower|higher)|install(?:ed)?\s+(?:\S+\s+){0,6}?(?:after|before)"
        r"|\b(?:lower|higher|below|above)\s+(?:in|on)\s+(?:the\s+)?(?:load\s*order|list)"
        r"|\boverride[sn]?\b.{0,40}\b(?:after|before)\b", re.I)),
    ("requires", re.compile(r"\brequire[sd]?\b|\brequirement\b", re.I)),
    ("compatible", re.compile(r"\bcompatible\s+with\b", re.I)),
)
# Game-patch talk ("Patch #8 ready", "Patch 7", "HF #34") and FAQ questions
# are the main noise in real descriptions.
_NOISE_RE = re.compile(r"\bpatch\s*#?\s*\d|\bhotfix\b|\bHF\s*#?\s*\d", re.I)
_FAQ_Q_RE = re.compile(r"^\s*Q\s*[:(]", re.I)
_FAQ_A_RE = re.compile(r"^\s*A\s*:", re.I)


def plain_text(description: str) -> str:
    """Nexus description (BBCode + HTML line breaks) -> plain text."""
    text = _BR_RE.sub("\n", description or "")
    text = _TAG_RE.sub("", text)
    text = html.unescape(text).replace(" ", " ").replace("﻿", "")
    return re.sub(r"[ \t]+", " ", text)


def extract_sentences(description: str) -> list[dict]:
    """The load-order / compatibility sentences of a description, each as
    ``{"kind": ..., "text": ...}``, strongest kinds first, at most 12."""
    seen: set[str] = set()
    found: list[tuple[int, int, dict]] = []
    order = {k: i for i, (k, _p) in enumerate(KIND_PATTERNS)}
    for pos, raw in enumerate(_SPLIT_RE.split(plain_text(description))):
        s = raw.strip(" -*•–—\t")
        if len(s) < 12 or len(s) > 400 or _FAQ_Q_RE.match(s):
            continue
        for kind, pat in KIND_PATTERNS:
            m = pat.search(s)
            if m:
                # Game-patch talk ("Patch #8 ready") is noise when it comes
                # before the keyword; "requires X (… Patch 7)" is still useful.
                noise = _NOISE_RE.search(s)
                if noise and noise.start() < m.start() and kind != "incompatible" \
                        and not re.search(r"load\s*order|\binstall", s, re.I):
                    break
                # FAQ answers only count when they're about order/conflicts.
                if _FAQ_A_RE.match(s) and kind in ("requires", "compatible"):
                    break
                key = s.casefold()
                if key not in seen:
                    seen.add(key)
                    found.append((order[kind], pos, {"kind": kind, "text": s}))
                break
    found.sort(key=lambda t: (t[0], t[1]))
    return [f[2] for f in found[:_MAX_SENTENCES]]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path(game_domain: str) -> Path:
    from Utils.config_paths import get_config_dir
    safe = re.sub(r"[^a-z0-9_-]", "_", game_domain.lower())
    return get_config_dir() / "nexus_author_notes" / f"{safe}.json"


def read_cache(game_domain: str) -> dict[str, dict]:
    try:
        data = json.loads(_cache_path(game_domain).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(game_domain: str, data: dict) -> None:
    path = _cache_path(game_domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(path)


def stale_ids(game_domain: str, mod_ids: list[int], force: bool = False,
              now: float | None = None) -> list[int]:
    cache = read_cache(game_domain)
    now = time.time() if now is None else now
    out = []
    for mid in dict.fromkeys(int(m) for m in mod_ids if int(m) > 0):
        entry = cache.get(str(mid))
        if force or entry is None or now - float(entry.get("fetched_at", 0)) > _MAX_AGE:
            out.append(mid)
    return out


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _default_session():
    from Nexus.nexus_api import NexusAPI, load_api_key
    try:
        key = load_api_key() or ""
    except Exception:
        key = ""
    return NexusAPI(api_key=key)


def _entry_from_node(node: dict, now: float) -> dict:
    req_nodes = (((node.get("modRequirements") or {})
                  .get("nexusRequirements") or {}).get("nodes") or [])
    requirement_notes = [
        {"kind": "requires",
         "text": f"Requires {r.get('modName') or r.get('modId')}"
                 + (f" — {r['notes'].strip()}" if (r.get("notes") or "").strip() else "")}
        for r in req_nodes]
    return {"fetched_at": now, "updated_at": node.get("updatedAt") or "",
            "name": node.get("name") or "",
            "sentences": extract_sentences(node.get("description") or ""),
            "requirement_notes": requirement_notes}


def fetch_notes(game_domain: str, mod_ids: list[int], force: bool = False,
                api=None, log_fn=None) -> dict[str, dict]:
    """Refresh stale entries for *mod_ids* and return the whole cache for
    *game_domain* ({str(mod_id): entry}).  Never raises; on any failure the
    previous cache is kept."""
    _log = log_fn or (lambda _m: None)
    with _lock:
        todo = stale_ids(game_domain, mod_ids, force)
        if not todo:
            return read_cache(game_domain)
        try:
            from Nexus.nexus_api import GRAPHQL_BASE
            api = api or _default_session()
            session = api._session
            timeout = getattr(api, "_timeout", 30.0)
        except Exception as exc:
            _log(f"Author notes: Nexus client unavailable ({exc}).")
            return read_cache(game_domain)
        cache = read_cache(game_domain)
        now = time.time()
        fetched = 0
        for i in range(0, len(todo), _BATCH):
            batch = todo[i:i + _BATCH]
            try:
                resp = session.post(GRAPHQL_BASE, json={
                    "query": _QUERY,
                    "variables": {"ids": [{"gameDomain": game_domain, "modId": m}
                                          for m in batch]}}, timeout=timeout)
                if not resp.ok:
                    _log(f"Author notes: Nexus answered {resp.status_code}; "
                         "keeping cached notes.")
                    break
                nodes = (((resp.json().get("data") or {})
                          .get("legacyModsByDomain") or {}).get("nodes") or [])
            except Exception as exc:
                _log(f"Author notes: fetch failed ({exc}); keeping cached notes.")
                break
            got = set()
            for node in nodes:
                try:
                    mid = int(node.get("modId", 0))
                except (TypeError, ValueError):
                    continue
                cache[str(mid)] = _entry_from_node(node, now)
                got.add(mid)
                fetched += 1
            for mid in batch:          # hidden/deleted mods: don't ask again today
                if mid not in got:
                    cache[str(mid)] = {"fetched_at": now, "updated_at": "", "name": "",
                                       "sentences": [], "requirement_notes": []}
        try:
            _write_cache(game_domain, cache)
        except OSError as exc:
            _log(f"Author notes: could not write cache ({exc}).")
        if fetched:
            _log(f"Author notes: read {fetched} Nexus mod page(s).")
        return cache

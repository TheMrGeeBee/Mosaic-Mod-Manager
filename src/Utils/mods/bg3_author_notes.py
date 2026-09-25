"""BG3 load-order suggestions from mod authors' notes on Nexus.

``Nexus.nexus_author_notes`` extracts the load-order / compatibility
sentences from each installed mod's page.  This module finds which *other*
installed mods a sentence names, and turns

  * "install Better Inventory UI after Better Container / BCPP / …"
      → load_after(Better Inventory UI, [Better Containers, BCPP UW …])
  * "Not compatible with Better Arrow Icons"
      → incompatible(Better Inventory UI, [Better Arrow Icons])

into suggestions the user accepts with one click (never applied silently).
Real sentences name mods loosely ("Better Container", "BCPP", "Better
Hotbar 2"), so matching uses each mod's core name without brackets, allows a
trailing "s", and accepts an all-caps acronym as a whole word.  No Qt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from Utils.mods.bg3_requirements import normalize_requirement_name as _norm

MIN_NAME_LEN = 8          # normalised characters, for full-name matches
_BRACKETS_RE = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]")
_TRAILING_VERSION_RE = re.compile(r"[\s_-]*v?\d+(?:\.\d+)*\s*$")
_ACRONYM_RE = re.compile(r"\b([A-Z][A-Z0-9]{2,})\b")
_BRACKETED_RE = re.compile(r"[\(\[\{]\s*([A-Z][A-Z0-9]{2,})\s*[\)\]\}]")
# Abbreviations that name a kind of mod, not one mod ("VFX support",
# "Act III"), so they never identify an installed mod on their own.
_GENERIC_ACRONYMS = {"VFX", "SFX", "NPC", "NPCS", "UI", "UW", "SE", "HD", "UHD",
                     "CC", "AIO", "DLC", "FPS", "EA", "II", "III", "IV", "VI",
                     "VII", "VIII", "IX", "XI", "XII", "PAK", "BG3", "LOD", "FX"}
_AFTER_RE = re.compile(r"\b(after|below|lower|last)\b", re.I)
_BEFORE_RE = re.compile(r"\b(before|above|higher|first)\b", re.I)


@dataclass
class AuthorSuggestion:
    kind: str                 # "load_after" | "load_before" | "incompatible"
    mod: str                  # the mod whose page says it
    others: list[str]         # other installed mods the sentence names
    sentence: str
    url: str = ""

    @property
    def id(self) -> str:
        return f"author_note:{self.kind}:{self.mod}|" + "|".join(sorted(self.others))


@dataclass
class _Names:
    cores: set[str] = field(default_factory=set)       # normalised
    acronyms: set[str] = field(default_factory=set)    # exact case


def _core(name: str) -> str:
    name = _BRACKETS_RE.sub("", name or "")
    name = _TRAILING_VERSION_RE.sub("", name.strip())
    return name.strip(" -_")


def mod_names(mod: str, recs: list[dict], nexus_name: str = "") -> _Names:
    """Every way a sentence might refer to this installed mod."""
    raw = [mod, nexus_name] + [r["meta"]["name"] for r in recs if r.get("meta")]
    out = _Names()
    for name in raw:
        if not name:
            continue
        core = _norm(_core(name))
        if len(core) >= MIN_NAME_LEN:
            out.cores.add(core)
            if core.endswith("s"):
                out.cores.add(core[:-1])
        # An abbreviation identifies the mod only when it leads the name
        # ("BCPP UW 6 chars…") or is bracketed ("… Sheet (ACS)"); a trailing
        # "- MCM" just means the mod works with MCM.
        first = _ACRONYM_RE.match(name.strip())
        candidates = ([first.group(1)] if first else []) + _BRACKETED_RE.findall(name)
        for acr in candidates:
            if acr not in _GENERIC_ACRONYMS and not acr.isdigit():
                out.acronyms.add(acr)
    return out


def named_mods(sentence: str, names: dict[str, _Names], exclude: str) -> list[str]:
    """Installed mods (other than *exclude*) that *sentence* names."""
    flat = _norm(sentence)
    hits = []
    for mod, n in names.items():
        if mod == exclude:
            continue
        if any(c in flat for c in n.cores) or any(
                re.search(rf"\b{re.escape(a)}\b", sentence) for a in n.acronyms):
            hits.append(mod)
    return sorted(hits)


def _read_nexus_meta(mod_dir: Path) -> tuple[int, str]:
    """(Nexus mod id, Nexus name) from meta.ini; (0, "") if not a Nexus mod."""
    try:
        text = (mod_dir / "meta.ini").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0, ""
    values = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            values[k.strip().lower()] = v.strip()
    try:
        mid = int(values.get("modid") or 0)
    except ValueError:
        mid = 0
    return mid, values.get("nexusname", "")


def nexus_ids(staging: Path, mods: list[str]) -> dict[str, int]:
    out = {}
    for mod in mods:
        mid, _name = _read_nexus_meta(staging / mod)
        if mid > 0:
            out[mod] = mid
    return out


def suggestions(index: dict[str, list[dict]], enabled: list[str], staging: Path,
                notes: dict[str, dict], game_domain: str = "baldursgate3"
                ) -> list[AuthorSuggestion]:
    """Turn cached author notes (``nexus_author_notes`` cache: {str(mod_id):
    entry}) into suggestions about the user's other enabled mods."""
    # Load-order divider packs (meta.lsx only) aren't real mods.
    live = [m for m in enabled if m in index and not all(
        (r.get("meta") or {}).get("is_meta_only") for r in index[m] if r.get("meta"))
        and any(r.get("meta") for r in index[m])]
    ids: dict[str, int] = {}
    nexus_names: dict[str, str] = {}
    for mod in live:
        mid, nexus_name = _read_nexus_meta(staging / mod)
        if mid > 0:
            ids[mod] = mid
        nexus_names[mod] = nexus_name
    page_count: dict[int, int] = {}
    for mid in ids.values():
        page_count[mid] = page_count.get(mid, 0) + 1
    names: dict[str, _Names] = {}
    for mod in live:
        # Files from one Nexus page all carry the page's name (Better
        # Inventory UI and its Addon), so it can't tell them apart there.
        shared = page_count.get(ids.get(mod, 0), 0) > 1
        names[mod] = mod_names(mod, index.get(mod, []),
                               "" if shared else nexus_names.get(mod, ""))

    same_page: dict[int, list[str]] = {}
    for mod, mid in ids.items():
        same_page.setdefault(mid, []).append(mod)

    out: list[AuthorSuggestion] = []
    seen: set[str] = set()
    for mod, mid in ids.items():
        entry = notes.get(str(mid)) or {}
        url = f"https://www.nexusmods.com/{game_domain}/mods/{mid}"
        siblings = [m for m in same_page.get(mid, []) if m != mod]
        for s in entry.get("sentences", []):
            kind, text = s.get("kind"), s.get("text", "")
            if kind not in ("order", "incompatible"):
                continue
            named = named_mods(text, names, exclude=mod)
            # Several installed files from one Nexus page (Better Inventory
            # UI + its Addon): a sentence naming one of them is about that one.
            if any(sib in named for sib in siblings):
                continue
            others = [o for o in named if o not in siblings]
            if not others:
                continue
            if kind == "incompatible":
                sug = AuthorSuggestion("incompatible", mod, others, text, url)
            else:
                after, before = _AFTER_RE.search(text), _BEFORE_RE.search(text)
                if after and not before:
                    kind_out = "load_after"
                elif before and not after:
                    kind_out = "load_before"
                else:
                    continue          # direction unclear: stays a plain note
                word = after or before
                # "MCM higher in your load order" / "make sure MCM is higher
                # than EasyCheat": the mod named just before the direction
                # word is its subject.  If that's another mod (not this one),
                # the direction flips for us.  "below MCM", "after Community
                # Library" and "install Better Inventory UI after them" don't.
                lead = text[max(0, word.start() - 40):word.start()]
                lead_self = named_mods(lead, {mod: names[mod]}, exclude="") if mod in names else []
                if not lead_self and named_mods(lead, {o: names[o] for o in others}, exclude=mod):
                    kind_out = "load_before" if kind_out == "load_after" else "load_after"
                sug = AuthorSuggestion(kind_out, mod, others, text, url)
            if sug.id not in seen:
                seen.add(sug.id)
                out.append(sug)
    return out

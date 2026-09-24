"""Load-order layers for Baldur's Gate 3's built-in "Sort Load Order".

Every mod that gets a modsettings.lsx entry is assigned one layer.  The
sorter (``Utils.mods.bg3_sort.compute_layered_plan``) orders layers first to
last, keeps the current order inside a layer, and lets collection order,
dependencies and the user's Load Order Insights decisions override layers.

The built-in table is plain data on purpose: add a known mod's meta.lsx UUID
to ``KNOWN_MODS`` to pin it.  A user can also move any single mod to another
layer from the sort preview; that choice is saved per profile.

No Qt imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_STATE_KEY = "bg3_sort_layers"


@dataclass(frozen=True)
class Layer:
    id: str
    label: str


# Load order, first (lowest priority) to last (wins conflicts).
LAYERS: tuple[Layer, ...] = (
    Layer("frameworks", "Frameworks & UI"),
    Layer("libraries", "Libraries & Resources"),
    Layer("gameplay", "Gameplay"),
    Layer("story", "Story & Companions"),
    Layer("items", "Items & Equipment"),
    Layer("visuals", "Visuals & Customisation"),
    Layer("misc", "Miscellaneous"),
    Layer("patches", "Patches"),
    Layer("late", "Late loaders"),
)
LAYER_INDEX = {layer.id: i for i, layer in enumerate(LAYERS)}
LAYER_LABEL = {layer.id: layer.label for layer in LAYERS}

# Well-known mods, by meta.lsx UUID (lowercase).  Checked before any
# heuristic, after the user's own per-mod choice.
KNOWN_MODS: dict[str, str] = {
    # UI / script frameworks almost everything else builds on
    "26922ba9-6018-5252-075d-7ff2ba6ed879": "frameworks",   # ImpUI (and P8 Fork)
    "755a8a72-407f-4f0d-9a33-274ac0f0b53d": "frameworks",   # Mod Configuration Menu
    # Libraries
    "396c5966-09b0-40a1-af3f-93a5e9ce71c0": "libraries",    # Community Library
    "f97b43be-7398-4ea5-8fe2-be7eb3d4b5ca": "libraries",    # VolitionCabinet
    "5d1bd6cb-6361-45ef-b20c-d997acfba822": "libraries",    # AahzLib
    "96bc14a7-733e-4bea-859d-3695d745efc1": "libraries",    # MazzleLib
    "ad95bd1c-1a80-45fb-b73b-9eea1f8e58d0": "libraries",    # MazzleDocs
    "2cba831e-bc5e-4ed5-b831-b5c4580b02b0": "libraries",    # Tag Framework
    "07fbc2f1-f359-4b9d-b243-fe28bd783e4c": "libraries",    # Goon's Library
    "fd03819b-cec2-c351-1680-81f1f1e52c76": "libraries",    # Vlad's Codex - VFX Library
    "0dd5b581-c210-4956-ab96-7682fb519de5": "libraries",    # Vlad's Grimoire - VFX Library
    "dd19db12-96c0-4bca-9ef6-e8d733801d23": "libraries",    # VFX Library SHV
    "e6333436-9cf0-4464-aaf2-39246292575e": "libraries",    # AV Item Shipment Framework
    "65e55feb-aada-4fec-821f-7d913e9b4d82": "libraries",    # DART Framework
    "4fa17abe-993c-4e7e-ab2a-e7370b166ac9": "libraries",    # Character Preset Framework
    # Must load after the mods they adjust
    "67fbbd53-7c7d-4cfa-9409-6d737b4d92a9": "late",         # Compatibility Framework
    "7b8366bd-abc1-4f9f-ba9d-585549b4a750": "late",         # Appearance Edit Enhanced
    "60de8215-7e9f-4cb6-9763-05aa6aecf257": "late",         # Appearance Edit Origins
}

# Nexus category (meta.ini ``categoryname``, casefolded) -> layer.
NEXUS_CATEGORIES: dict[str, str] = {
    "user interface": "frameworks",
    "resources": "libraries",
    "gameplay": "gameplay",
    "utilities": "gameplay",
    "classes": "gameplay",
    "races": "gameplay",
    "spells": "gameplay",
    "companions": "story",
    "quests": "story",
    "equipment": "items",
    "armor": "items",
    "armour": "items",
    "weapons": "items",
    "clothing": "items",
    "accessories": "items",
    "visuals": "visuals",
    "character customisation": "visuals",
    "character customization": "visuals",
    "audio": "visuals",
    "animations": "visuals",
    "dice": "visuals",
    "photo mode": "visuals",
    "miscellaneous": "misc",
}

# mod.io installs have no Nexus category but carry tags (meta.ini
# ``modiotags``).  Checked in this order, so "UI" beats "Quality of Life".
MODIO_TAGS: tuple[tuple[str, str], ...] = (
    ("ui", "frameworks"),
    ("user interface", "frameworks"),
    ("library", "libraries"),
    ("framework", "libraries"),
    ("classes", "gameplay"),
    ("races", "gameplay"),
    ("spells", "gameplay"),
    ("gameplay", "gameplay"),
    ("companions", "story"),
    ("story", "story"),
    ("quests", "story"),
    ("weapons", "items"),
    ("armour", "items"),
    ("armor", "items"),
    ("clothing", "items"),
    ("equipment", "items"),
    ("customisation", "visuals"),
    ("customization", "visuals"),
    ("visuals", "visuals"),
    ("dice", "visuals"),
    ("audio", "visuals"),
    ("quality of life", "gameplay"),
    ("utility", "gameplay"),
)

_LIBRARY_TAGS = {"library", "api", "core", "framework"}
_LIBRARY_NAME_RE = re.compile(r"(?<![a-z])(lib|library|framework)(?![a-z])", re.I)


def read_categories(mod_dir: Path) -> tuple[str, list[str]]:
    """(Nexus ``categoryname``, mod.io ``modiotags``) from meta.ini."""
    try:
        text = (mod_dir / "meta.ini").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "", []
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip().lower()] = value.strip()
    tags = [t.strip() for t in values.get("modiotags", "").split(",") if t.strip()]
    return values.get("categoryname", ""), tags


def classify(mod: str, recs: list[dict], category: str,
             modio_tags: list[str] | None = None,
             override: str | None = None,
             looks_like_patch=None) -> tuple[str, str]:
    """(layer id, reason) for one mod folder.

    *recs* are the mod's pak records from ``bg3_pak_index.build_index``.
    Being depended on is deliberately not a library signal: content packs
    (Dragonborn assets, colour packs) and big overhauls are depended on too,
    and dependencies already make their dependents load after them.
    """
    if override in LAYER_INDEX:
        return override, "your choice"
    metas = [r["meta"] for r in recs if r.get("meta")]
    for m in metas:
        known = KNOWN_MODS.get(m["uuid"].lower())
        if known:
            return known, "known mod"
    if any(m.get("mod_type", "").lower() == "patch" for m in metas):
        return "patches", "meta.lsx says it is a patch"
    if looks_like_patch is not None and looks_like_patch(mod, recs):
        return "patches", "named as a patch / compatibility mod"
    tags = {t.strip().lower() for m in metas for t in m.get("tags", [])}
    if tags & _LIBRARY_TAGS:
        return "libraries", f"tagged {', '.join(sorted(tags & _LIBRARY_TAGS))}"
    if any(_LIBRARY_NAME_RE.search(m["name"]) for m in metas):
        return "libraries", "named as a library / framework"
    layer = NEXUS_CATEGORIES.get(category.casefold())
    if layer:
        return layer, f"Nexus category: {category}"
    if category:
        return "misc", f"Nexus category: {category}"
    folded = {t.casefold() for t in (modio_tags or [])}
    for tag, layer in MODIO_TAGS:
        if tag in folded:
            return layer, f"mod.io tag: {tag}"
    return "misc", "no category"


# ---------------------------------------------------------------------------
# Per-profile overrides
# ---------------------------------------------------------------------------

def read_overrides(profile_dir: Path) -> dict[str, str]:
    from Utils.profile.profile_state import read_profile_state
    raw = read_profile_state(profile_dir).get(_STATE_KEY) or {}
    return {m: l for m, l in raw.items() if l in LAYER_INDEX}


def set_override(profile_dir: Path, mod: str, layer: str | None) -> None:
    """Pin *mod* to *layer* for this profile (None removes the pin)."""
    from Utils.profile.profile_state import _update_key
    data = read_overrides(profile_dir)
    if layer is None:
        data.pop(mod, None)
    elif layer in LAYER_INDEX:
        data[mod] = layer
    _update_key(profile_dir, _STATE_KEY, dict(sorted(data.items())))

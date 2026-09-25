"""Load-order layers for Baldur's Gate 3's built-in "Sort Load Order".

Every mod that gets a modsettings.lsx entry is assigned one layer.  The
sorter (``Utils.mods.bg3_sort.compute_layered_plan``) orders layers first to
last, keeps the current order inside a layer, and lets collection order,
dependencies and the user's Load Order Insights decisions override layers.

Known mods are pinned to a layer in ``bg3_known_rules.json`` (see
``Utils.mods.bg3_known_rules``).  A user can also move any single mod to
another layer from the sort preview; that choice is saved per profile.

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

# Well-known mods (ImpUI, Mod Configuration Menu, Compatibility Framework…)
# are pinned to layers in ``bg3_known_rules.json`` — one list for layer pins,
# author load-order rules and known incompatibilities.

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
             looks_like_patch=None,
             known_layer: str | None = None) -> tuple[str, str]:
    """(layer id, reason) for one mod folder.

    *recs* are the mod's pak records from ``bg3_pak_index.build_index``.
    Being depended on is deliberately not a library signal: content packs
    (Dragonborn assets, colour packs) and big overhauls are depended on too,
    and dependencies already make their dependents load after them.
    """
    if override in LAYER_INDEX:
        return override, "your choice"
    if known_layer in LAYER_INDEX:
        return known_layer, "known mod"
    metas = [r["meta"] for r in recs if r.get("meta")]
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

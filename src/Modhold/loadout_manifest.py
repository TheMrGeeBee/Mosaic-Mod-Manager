"""
loadout_manifest.py
Parse a Modhold loadout (``GET /api/v1/loadouts/{id}``, format ``modhold-loadout``)
into plain objects, and plan an install from it. Qt-free.

Real loadouts are messy, and this model copes with what was seen live:

* ``position`` is not unique (several items can share 45, 54, 55, ...), so items
  are ordered by ``(position, index in the payload)`` and carry their own unique
  ``order_index``; nothing is ever keyed by ``position``.
* About half the items are not hosted on Modhold (``mod.id`` null, ``files`` empty,
  only a link): they cannot be downloaded and are listed for the user instead.
* Items can be optional, and some are ``locked: "beta"`` (needs the right access).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from Modhold.nexus_links import NexusRef, parse_nexus_url

FORMAT = "modhold-loadout"
SUPPORTED_VERSION = 1


class LoadoutFormatError(ValueError):
    """The payload is not a loadout this version of Mosaic understands."""


@dataclass(frozen=True)
class LoadoutFile:
    id: int
    name: str
    version: str
    size: int
    sha256: str


@dataclass
class LoadoutItem:
    order_index: int                 # unique, position in the payload
    position: int                    # as published; may repeat
    optional: bool
    note: str
    mod_id: Optional[int]            # Modhold mod id, None when hosted elsewhere
    name: str
    version: str
    author: str
    page: str                        # the mod's page (Modhold, or its original site)
    locked: str                      # "" or "beta"
    files: list[LoadoutFile] = field(default_factory=list)
    external_url: str = ""           # where an unhosted item has to be fetched
    nexus_ref: Optional[NexusRef] = None   # set when that link is a Nexus mod page

    @property
    def hosted(self) -> bool:
        """Downloadable from Modhold: it has a Modhold mod and at least one file."""
        return bool(self.mod_id) and bool(self.files)

    @property
    def link(self) -> str:
        """The best page for the user to open for this item."""
        return self.external_url or self.page


@dataclass
class Loadout:
    id: int
    name: str
    summary: str
    by: str
    game_slug: str
    game_name: str
    updated: str
    page: str
    signed_in: bool
    items: list[LoadoutItem]
    game_version: str = ""           # the game version it was built for ("" = not stated)
    works_on: list[str] = field(default_factory=list)   # other versions known to work


@dataclass
class LoadoutPlan:
    """What an install will do with a chosen set of optional items."""
    installable: list[LoadoutItem]           # hosted on Modhold, chosen: to be downloaded
    external: list[LoadoutItem]              # chosen, but not downloadable here: listed for the user
    skipped_optional: list[LoadoutItem]      # optional items the user left out
    gated: list[LoadoutItem]                 # installable items that need beta access
    nexus: list[LoadoutItem] = field(default_factory=list)   # fetched from Nexus instead

    def ordered(self) -> list[LoadoutItem]:
        """Everything that will be downloaded (Modhold and Nexus), in loadout order."""
        return sorted(self.installable + self.nexus,
                      key=lambda it: (it.position, it.order_index))

    @property
    def file_count(self) -> int:
        return sum(len(i.files) for i in self.installable)

    @property
    def download_bytes(self) -> int:
        return sum(f.size for i in self.installable for f in i.files)


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_file(raw: dict) -> Optional[LoadoutFile]:
    fid = _int(raw.get("id"))
    if fid <= 0:
        return None
    return LoadoutFile(id=fid, name=str(raw.get("name") or ""),
                       version=str(raw.get("version") or ""),
                       size=_int(raw.get("size")),
                       sha256=str(raw.get("sha256") or "").lower())


def _parse_item(index: int, raw: dict) -> LoadoutItem:
    mod = raw.get("mod") or {}
    mod_id = _int(mod.get("id")) or None
    files = [f for f in (_parse_file(x) for x in (raw.get("files") or [])) if f is not None]
    page = str(mod.get("page") or "")
    nexus = str(raw.get("nexus") or "")
    hosted = bool(mod_id) and bool(files)
    # An unhosted item's link is its page; a hosted one never needs it.
    external_url = "" if hosted else (nexus or page)
    return LoadoutItem(
        order_index=index,
        position=_int(raw.get("position"), default=index),
        optional=bool(raw.get("optional")),
        note=str(raw.get("note") or "").strip(),
        mod_id=mod_id,
        name=str(mod.get("name") or ""),
        version=str(mod.get("version") or ""),
        author=str(mod.get("author") or mod.get("author_handle") or ""),
        page=page,
        locked="beta" if raw.get("locked") == "beta" else "",
        files=files,
        external_url=external_url,
        nexus_ref=None if hosted else parse_nexus_url(external_url),
    )


def _version_text(value) -> str:
    """A game version as the author wrote it, or "" for null/missing/not a string."""
    return value.strip() if isinstance(value, str) else ""


def _version_list(value) -> list[str]:
    """``works_on``: the strings in it, in order; anything that is not a list gives []."""
    if not isinstance(value, list):
        return []
    return [v for v in (_version_text(x) for x in value) if v]


def parse_loadout(payload: dict) -> Loadout:
    """Parse a ``modhold-loadout`` payload. Raises :class:`LoadoutFormatError` for
    anything that is not one, or is a newer format than this Mosaic understands."""
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise LoadoutFormatError("This is not a Modhold loadout.")
    version = _int(payload.get("version"), default=1)
    if version > SUPPORTED_VERSION:
        raise LoadoutFormatError(
            f"This loadout uses format version {version}; this Mosaic understands "
            f"version {SUPPORTED_VERSION}. Update Mosaic to install it.")
    game = payload.get("game") or {}
    raw_items = [x for x in (payload.get("items") or []) if isinstance(x, dict)]
    items = [_parse_item(i, raw) for i, raw in enumerate(raw_items)]
    items.sort(key=lambda it: (it.position, it.order_index))
    return Loadout(
        id=_int(payload.get("id")), name=str(payload.get("name") or ""),
        summary=str(payload.get("summary") or ""), by=str(payload.get("by") or ""),
        game_slug=str(game.get("slug") or ""), game_name=str(game.get("name") or ""),
        updated=str(payload.get("updated") or ""), page=str(payload.get("page") or ""),
        signed_in=bool(payload.get("signed_in")), items=items,
        game_version=_version_text(payload.get("game_version")),
        works_on=_version_list(payload.get("works_on")))


def plan_install(loadout: Loadout,
                 include_optional: "set[int] | None" = None,
                 nexus_items: "set[int] | None" = None) -> LoadoutPlan:
    """Split the loadout into what gets downloaded, what the user must fetch
    themselves, and what was left out.

    *include_optional* is the set of ``order_index`` values of optional items the
    user ticked; ``None`` means every optional item. Required items are always in.
    An item hosted twice (same Modhold mod and file) is only installed once.

    *nexus_items* is the set of ``order_index`` values of unhosted items that have a
    Nexus link and should be downloaded from Nexus; ``None`` (the default) fetches
    nothing from Nexus, so those items stay on the user's list."""
    installable: list[LoadoutItem] = []
    nexus: list[LoadoutItem] = []
    external: list[LoadoutItem] = []
    skipped: list[LoadoutItem] = []
    seen_files: set[int] = set()
    seen_external: set[str] = set()
    for item in loadout.items:
        if item.optional and include_optional is not None \
                and item.order_index not in include_optional:
            skipped.append(item)
            continue
        if item.hosted:
            fresh = [f for f in item.files if f.id not in seen_files]
            if not fresh:
                continue                              # a repeat of an item already planned
            seen_files.update(f.id for f in fresh)
            if len(fresh) != len(item.files):
                item = LoadoutItem(**{**item.__dict__, "files": fresh})
            installable.append(item)
        elif (item.nexus_ref is not None and nexus_items is not None
              and item.order_index in nexus_items):
            key = f"nexus:{item.nexus_ref.domain}/{item.nexus_ref.mod_id}/{item.nexus_ref.file_id}"
            if key in seen_external:
                continue
            seen_external.add(key)
            nexus.append(item)
        else:
            key = item.link or item.name
            if key in seen_external:
                continue
            seen_external.add(key)
            external.append(item)
    gated = [i for i in installable if i.locked]
    return LoadoutPlan(installable, external, skipped, gated, nexus)

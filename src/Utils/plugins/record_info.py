"""
record_info.py
RecordInfo -- the record identity + stats shown in the NIF Viewer/Character
tab's info card, generalized across every record type Mosaic parses (not
just ARMO -- see armor_record_details.py for ARMO specifically, which
uniquely needs ARMA indirection, and item_record_details.py for every other
handled type, which reference their own mesh directly via their own MODL
subrecord).

value/weight are "int | None"/"float | None" rather than always-populated:
None means "this record type has no such concept" (e.g. a Static prop has
no inventory value), shown as a dash by the UI -- not the same thing as a
real value of 0.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from Utils.plugins.esp_records import Record

_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def humanize(suffix: str) -> str:
    """CamelCase/PascalCase -> "Title Case With Spaces"."""
    words = _WORD_RE.findall(suffix)
    return " ".join(w.capitalize() for w in words) if words else suffix


def classify_by_keyword_prefix(keyword_labels: list, prefix: str) -> "str | None":
    """First keyword EditorID matching ^<prefix>(.+)$ (case-insensitive),
    humanized -- e.g. prefix "WeapType" + keyword "WeapTypeWarAxe" ->
    "War Axe". None if no keyword matches."""
    pattern = re.compile(rf"^{re.escape(prefix)}(.+)$", re.IGNORECASE)
    for eid in keyword_labels:
        m = pattern.match(eid)
        if m:
            return humanize(m.group(1))
    return None


_DASH = "—"


def format_slots(slots: "frozenset[int]", slot_labels: "dict[int, str] | None") -> str:
    """Sorted "32 (Body), 34 (Forearms)" style text; a dash if there are no
    slots at all (there almost always are, for a record that has any)."""
    if not slots:
        return _DASH
    labels = slot_labels or {}
    parts = []
    for s in sorted(slots):
        name = labels.get(s)
        parts.append(f"{s} ({name})" if name else str(s))
    return ", ".join(parts)


def read_edid_full(rec: Record, localized: bool, strings: dict) -> "tuple[str, str]":
    """(editor_id, name) -- name is "" if FULL is absent or (when localized)
    its string id isn't in *strings*."""
    edid_raw = rec.sub("EDID")
    editor_id = edid_raw.rstrip(b"\x00").decode("utf-8", errors="replace") if edid_raw else ""
    full_raw = rec.sub("FULL")
    name = ""
    if full_raw:
        if localized:
            if len(full_raw) >= 4:
                sid = struct.unpack_from("<I", full_raw, 0)[0]
                name = strings.get(sid, "")
        else:
            name = full_raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    return editor_id, name


@dataclass
class RecordInfo:
    sig: str                           # "ARMO", "WEAP", "STAT", ...
    base_type_label: str               # "Armor (ARMO)", "Weapon (WEAP)", ...
    plugin: str                        # defining plugin filename (the resolved owner, not
                                        # necessarily whichever plugin's copy last parsed it)
    local_formid: int                  # this record's own local id (low 24 bits)
    editor_id: str
    name: str                          # "" if unresolved
    value: "int | None"                # None: this type has no value concept
    weight: "float | None"             # None: this type has no weight concept
    keyword_labels: list = field(default_factory=list)   # resolved keyword EditorIDs
    enabled: bool = True
    display_formid: str = ""           # precomputed "062A1B3C" / "FE0021A3" style string
    extra_fields: list = field(default_factory=list)      # [(label, text), ...] type-specific rows

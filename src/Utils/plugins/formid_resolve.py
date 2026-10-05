"""
formid_resolve.py
FormID identity resolution and display formatting for Bethesda plugins.

Two separate, deliberately small problems:

  resolve():        cross-plugin IDENTITY — given a raw FormID read from one
                     plugin's subrecord (e.g. a KWDA entry, an ARMO's ARMA
                     link), which (plugin, local id) it actually means. The
                     high byte of a FormID indexes the REFERENCING plugin's
                     own MAST list (plugin_parser.read_masters) — not the
                     global load order — this is the standard xEdit/
                     libloadorder "direct master list" rule.

  build_display_order()/display_formid(): DISPLAY formatting — the human-
                     readable Base Form ID string for a record's OWN FormID,
                     matching what xEdit/the in-game console would show.
                     Regular (non-ESL) plugins use a 2-hex global load-order
                     position; ESL-flagged plugins (.esl, or .esp with the
                     ESL header bit set) use a separate "FE" + 3-hex subset
                     position addressing scheme instead, since light plugins
                     don't consume one of the 254 regular load-order slots.

display_formid() never needs resolve() — a record's own Base Form ID only
depends on which plugin is currently being scanned (always "self"), not on
any cross-plugin reference.
"""
from __future__ import annotations

from typing import Callable

GlobalKey = tuple  # (defining plugin filename.lower(), local id: int)


def resolve(raw_formid: int, masters: list, self_name: str) -> GlobalKey:
    """Cross-plugin identity for a FormID found inside some plugin's own
    subrecord data. *masters* is that plugin's own MAST list (in order,
    from plugin_parser.read_masters); *self_name* is that plugin's own
    filename. High byte == len(masters) means "defined in this file itself"."""
    hi, low = raw_formid >> 24, raw_formid & 0xFFFFFF
    if 0 <= hi < len(masters):
        return (masters[hi].lower(), low)
    return (self_name.lower(), low)


def build_display_order(
    active_plugins: list, is_esl: "Callable[[str], bool]"
) -> "tuple[dict, dict]":
    """(full_position, esl_position) — two separate 0-indexed load-order maps
    built once per catalog build. *active_plugins* is every enabled plugin's
    filename, in load order (lowest priority first, matching
    armor_records.active_plugin_paths' own convention). A plugin appears in
    exactly one of the two returned maps."""
    full_position: dict = {}
    esl_position: dict = {}
    for name in active_plugins:
        key = name.lower()
        if is_esl(name):
            if key not in esl_position:
                esl_position[key] = len(esl_position)
        else:
            if key not in full_position:
                full_position[key] = len(full_position)
    return full_position, esl_position


def display_formid(plugin: str, local_id: int, full_position: dict, esl_position: dict) -> str:
    """The Base Form ID string matching xEdit/the in-game console: FE + 3-hex
    ESL-subset position + 3-hex (local_id & 0xFFF) for an ESL-flagged plugin;
    otherwise 2-hex full-plugin load-order position + 6-hex local_id. Falls
    back to a plain 6-hex local id (no plugin prefix) if the plugin isn't in
    either map (shouldn't happen for an enabled plugin, but never crash the
    panel over a stale/rebuilt index)."""
    key = plugin.lower()
    if key in esl_position:
        return f"FE{esl_position[key]:03X}{local_id & 0xFFF:03X}"
    if key in full_position:
        return f"{full_position[key]:02X}{local_id & 0xFFFFFF:06X}"
    return f"{local_id & 0xFFFFFF:06X}"

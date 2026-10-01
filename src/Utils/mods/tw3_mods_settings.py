"""
tw3_mods_settings.py
Write The Witcher 3's native mods.settings INI (Documents/The Witcher 3/
mods.settings). The game engine itself reads this file to resolve a
same-relative-path conflict between mods: one [<modFolderId>] section per
mod with Enabled/Priority/VK keys, and the mod with the LOWEST Priority
number wins.

That direction was confirmed the hard way, live, against the real game --
not just inferred from Vortex's open-source game-witcher3 extension
(iniParser.ts/priorityManager.ts), which documents the file's shape but not
which direction wins. A real 6-way conflict on game/player/playerWitcher.ws
(a Collection's mod0000_MergedFiles Script Merger output plus 5 source
mods) was written with mod0000_MergedFiles at the highest Priority (41, the
intended winner under a "highest wins" assumption) and verified correct via
direct file read -- yet two separate live game launches both showed
modGearLevelScaling (Priority 11, the LOWEST of the six) as the actual
winner. 11 is the minimum across the whole conflict group
(11/12/18/22/33/41); "lowest wins" is the only theory consistent with that
result, and was re-verified against all six values, not just two.

Two priority sources, in order:
  1. A Collection's own loadOrder manifest (profile's collection.json),
     matched to deployed mods PRIMARILY by Nexus file_id (loadOrder[].fileId
     vs. each installed mod's own meta.ini fileid -- confirmed against a
     real Collection that every loadOrder entry carries fileId, and it's a
     clean numeric match independent of archive folder-naming quirks),
     falling back to case-insensitive folder-id string matching only when
     a file_id isn't available (e.g. a non-Nexus source). The manifest's
     ascending data.prefix/index already encodes the curator's own intended
     winner correctly *in relative order* -- lower prefix = curator's
     intended winner, exactly matching "lowest Priority wins" with zero
     inversion needed, which is itself good evidence prefix and Priority
     share the same "lower is stronger" convention. The literal prefix
     number is used only to order matched entries relative to each other,
     not written verbatim (see _rank's docstring for why).

     File-id matching matters in practice, not just in theory: a real
     Collection's loadOrder listed id "modHideQuests" for a mod whose
     actual archive folder is "ModHideQuest 5.00 - Je1992" -- not even a
     prefix match (singular vs. plural). String-matching alone would have
     misfiled a genuine Collection mod as "extra" and let it win priority
     it was never meant to have; file_id matching gets it right.
  2. Any deployed mod NOT covered by either match (no Collection installed,
     or extra mods added on top of one) ranks from the profile's own
     modlist.txt (top of file = highest Mosaic priority, per
     Utils.mods.modlist's own documented convention) and wins over every
     Collection-sourced entry -- a mod the user deliberately added beyond a
     curated set represents a deliberate choice that shouldn't be silently
     defeated by curated content.

DLC-prefixed folders are never part of this (mirrors Vortex's own
iniParser.ts, which skips any name starting with "dlc") -- callers must
exclude them from *deployed_mods* themselves.
"""

from __future__ import annotations

import configparser
from pathlib import Path


def write_mods_settings(settings_path: Path, modlist_path: Path,
                        deployed_mods: dict[str, str], log_fn=None,
                        manifest_load_order: "list[dict] | None" = None,
                        file_ids: "dict[str, int] | None" = None) -> int:
    """Write *settings_path* ranking every entry in *deployed_mods*
    ({tw3 mod-folder id: Mosaic mod display name}, mods/ entries only).
    *file_ids* ({tw3 mod-folder id: Nexus file_id}, 0/absent for a
    non-Nexus or unidentified mod) is the preferred match key against the
    Collection manifest's own loadOrder -- see module docstring for why.

    Returns the number of mods written. Never raises -- a failure here must
    not fail the surrounding deploy, same contract as modsettings.py's
    write_modsettings() for BG3."""
    _log = log_fn or (lambda _m: None)
    try:
        if not deployed_mods:
            return 0
        ranked = _rank(deployed_mods, modlist_path, manifest_load_order,
                       file_ids or {}, _log)
        cp = configparser.ConfigParser()
        cp.optionxform = str  # preserve folder-id casing in section names
        for folder_id, priority in ranked.items():
            cp.add_section(folder_id)
            cp.set(folder_id, "Enabled", "1")
            cp.set(folder_id, "Priority", str(priority))
            cp.set(folder_id, "VK", folder_id)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        # No spaces around "=" and real Windows CRLF line endings -- matches
        # the strict Windows-INI format TW3's own tools expect. Confirmed
        # live: Script Merger rejected a `space_around_delimiters=True` file
        # with "Unrecognized setting ... Enabled = 1" -- its parser splits
        # on the first "=" without trimming, so "Enabled " (trailing space,
        # from the space before "=") never matches the literal key it wants.
        with open(settings_path, "w", encoding="utf-8", newline="\r\n") as fh:
            cp.write(fh, space_around_delimiters=False)
        return len(ranked)
    except Exception as exc:
        _log(f"  WARN: could not write mods.settings: {exc}")
        return 0


def _order_unmatched_by_modlist(folder_ids: list[str],
                               deployed_mods: dict[str, str],
                               modlist_path: Path) -> list[str]:
    """Return *folder_ids* ordered winner-first: top of modlist.txt first,
    then any folder whose mod_name isn't in modlist.txt at all (shouldn't
    normally happen), in original order -- nothing is silently dropped."""
    if not folder_ids:
        return []
    name_to_folders: dict[str, list[str]] = {}
    for folder_id in folder_ids:
        name_to_folders.setdefault(deployed_mods[folder_id], []).append(folder_id)

    from Utils.mods.modlist import read_modlist
    entries = read_modlist(modlist_path) if modlist_path.is_file() else []
    ordered_names = [e.name for e in entries if e.enabled and not e.is_separator]

    ordered: list[str] = []
    seen: set[str] = set()
    for name in ordered_names:
        for folder_id in name_to_folders.get(name, []):
            if folder_id not in seen:
                ordered.append(folder_id)
                seen.add(folder_id)
    for folder_id in folder_ids:
        if folder_id not in seen:
            ordered.append(folder_id)
            seen.add(folder_id)
    return ordered


def _rank(deployed_mods: dict[str, str], modlist_path: Path,
         manifest_load_order: "list[dict] | None",
         file_ids: dict[str, int], log_fn=None) -> dict[str, int]:
    """Return {folder_id: Priority} for every entry in *deployed_mods*,
    lowest Priority = wins (see module docstring).

    Final values are a fresh, consecutive 0..N-1 sequence in "most winning
    first" order, not the Collection manifest's raw prefix numbers written
    verbatim. Two reasons: (1) an extra/unmatched mod must win over every
    Collection mod, and a real Collection's own prefix values can start at
    0, so placing an unmatched group strictly below the manifest's minimum
    would require negative Priority integers -- whether TW3's parser
    accepts those at all is unverified, so it's avoided entirely; (2)
    resequencing to guaranteed-unique integers makes the old "two folders
    claim the same manifest priority" duplicate-value failure mode (see the
    matching logic below) structurally impossible rather than something
    that has to be separately guarded against."""
    _log = log_fn or (lambda _m: None)
    by_fileid: dict[int, int] = {}
    by_id: dict[str, int] = {}
    for entry in (manifest_load_order or []):
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if not isinstance(data, dict):
            data = {}
        raw = data.get("prefix", entry.get("index"))
        try:
            manifest_rank = int(raw)
        except (TypeError, ValueError):
            continue
        entry_fid = entry.get("fileId")
        if isinstance(entry_fid, int) and entry_fid:
            by_fileid[entry_fid] = manifest_rank
        entry_id = str(entry.get("id") or "").strip().lower()
        if entry_id:
            by_id[entry_id] = manifest_rank

    # matched: folder_id -> manifest_rank, used only to ORDER matched
    # entries relative to each other below -- never written verbatim.
    matched: dict[str, int] = {}
    unmatched: list[str] = []
    # A single Mosaic mod can legitimately produce more than one top-level
    # TW3 folder (its real mod folder, plus e.g. a leftover wrapper folder
    # holding one ambiguous loose file -- confirmed live: "Hide Quest in
    # Quest Menu" produced both "modHideQuests" and "ModHideQuest 5.00 -
    # Je1992", both tracing to the same Nexus file_id). Each Collection
    # manifest entry must only ever be claimed by ONE folder_id.
    #
    # Two passes, not one combined loop: an exact id-string match is
    # precise (the manifest is naming that exact folder), while a
    # file_id match is a looser fallback that can also hit an incidental
    # wrapper folder sharing the same underlying mod. Doing both in a
    # single pass made the result depend on iteration order -- whichever
    # folder_id happened to come first could wrongly claim the manifest
    # entry via file_id before the real folder's id-match was ever tried,
    # confirmed live (the wrapper folder claimed it, leaving the actual
    # content folder unmatched). Running all id-string matches first,
    # unconditionally, before any file_id matching starts, makes the
    # correct folder win regardless of dict/filemap order.
    used_ranks: set[int] = set()
    remaining: list[str] = []
    for folder_id in deployed_mods:
        r = by_id.get(folder_id.lower())
        if r is not None and r not in used_ranks:
            matched[folder_id] = r
            used_ranks.add(r)
        else:
            remaining.append(folder_id)
    for folder_id in remaining:
        fid = file_ids.get(folder_id) or 0
        r = by_fileid.get(fid) if fid else None
        if r is not None and r not in used_ranks:
            matched[folder_id] = r
            used_ranks.add(r)
        else:
            unmatched.append(folder_id)

    if (by_fileid or by_id) and unmatched:
        # A deployed mod the Collection's own loadOrder doesn't cover by
        # either file_id or exact folder id falls back to modlist.txt and
        # wins over every Collection-sourced mod (see module docstring) --
        # correct for a genuinely extra mod, but surfaced here in case it's
        # actually a Collection mod Mosaic couldn't identify (e.g. no
        # tracked Nexus file_id, such as a manually-installed or off-site
        # source) rather than silently trusting the default.
        for folder_id in unmatched:
            _log(f"  Note: '{deployed_mods[folder_id]}' ({folder_id}) "
                 "doesn't match the Collection's load order by file ID or "
                 "folder name -- ranked from modlist.txt instead, which "
                 "may not match the curator's intended priority.")

    ordered_unmatched = _order_unmatched_by_modlist(unmatched, deployed_mods,
                                                     modlist_path)

    final: dict[str, int] = {}
    rank = 0
    for folder_id in ordered_unmatched:
        final[folder_id] = rank
        rank += 1
    # Lower manifest prefix = curator's intended winner (see module
    # docstring) -> ascending by manifest_rank assigns lower final Priority
    # to lower-prefix entries, preserving that relative order exactly.
    for folder_id in sorted(matched, key=lambda f: matched[f]):
        final[folder_id] = rank
        rank += 1
    return final

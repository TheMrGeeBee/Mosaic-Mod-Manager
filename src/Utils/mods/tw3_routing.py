"""
tw3_routing.py
Decides whether a staged TW3 file deploys under mods/, dlc/, or the game
root, from the staged archive's own folder layout.

Lives here (not in Games/The Witcher 3/witcher_3.py, where it originated)
because that file's folder name has a space in it, which makes it
unimportable as a normal Python module elsewhere -- witcher_3.py's own
deploy() imports _route_path from here, and so does
Utils.mods.tw3_load_index (which needs the exact same routing deploy()
uses to know which staged files would actually collide at the same
in-game path, prospectively, before any deploy has happened).
"""

from __future__ import annotations

_SKIP_SEGMENTS = frozenset({"mods", "dlc", "dlcs"})

# Top-level game-root folders that are NOT prefixed — they should be deployed
# directly at the game root with their own folder name preserved.
# When one of these is found (possibly buried under an archive wrapper like
# "Full/" or "Lite/"), everything from that segment onward is kept and
# deployed to the game root ("").
_ROOT_SEGMENTS = frozenset({"bin"})


def route_path(staged_rel: str) -> tuple[str, str]:
    """Return (dest_prefix, final_rel) for a staged filemap path.

    Scans directory segments (not the filename) looking for:
      - A segment in _SKIP_SEGMENTS  → skip it and look deeper
      - A segment in _ROOT_SEGMENTS  → deploy path-from-here at game root
      - A segment starting with "mod" → deploy under mods/
      - A segment starting with "dlc" → deploy under dlc/

    All other segments (archive wrappers like "Full/", "Lite/", version
    folders, etc.) are silently skipped so that the correct inner structure
    is found regardless of how many wrapper folders the archive contains.

    Returns:
      dest_prefix — game-root-relative destination directory (empty = root)
      final_rel   — staged_rel starting from the qualifying segment, so the
                    modname folder lands directly inside mods/ or dlc/

    Examples:
      "modFoo/content/x.xml"                      → ("mods", "modFoo/content/x.xml")
      "TrueFires_v1.01/modFoo/content/x.xml"      → ("mods", "modFoo/content/x.xml")
      "mods/modFoo/content/x.xml"                 → ("mods", "modFoo/content/x.xml")
      "Full/mods/modFoo/content/x.xml"            → ("mods", "modFoo/content/x.xml")
      "dlcFoo/content/x.xml"                      → ("dlc",  "dlcFoo/content/x.xml")
      "Full/DLC/dlcFoo/content/x.xml"             → ("dlc",  "dlcFoo/content/x.xml")
      "bin/x64/d3d11.dll"                         → ("",     "bin/x64/d3d11.dll")
      "Full/bin/config/r4game/user_config.xml"    → ("",     "bin/config/r4game/user_config.xml")

    A wrapper folder whose own NAME happens to start with "mod"/"dlc" (an
    author-chosen archive name, e.g. "ModHideQuest 5.00 - Je1992") is NOT
    mistaken for the real mod folder: if the segment right after it is
    itself a recognised container (mods/dlc/dlcs), that confirms the
    current segment is just a wrapper around a properly-nested archive, so
    scanning continues deeper instead of stopping here. Confirmed live: a
    real archive shaped "ModHideQuest 5.00 - Je1992/Mods/modHideQuests/
    content/..." was deployed doubly-nested (mods/ModHideQuest 5.00 -
    Je1992/Mods/modHideQuests/content/...) before this check existed --
    not just a cosmetic issue, since TW3's own mod loader only scans one
    level deep under mods/, so the mod's content was never actually read
    by the game at all.
    """
    norm     = staged_rel.replace("\\", "/")
    segments = norm.split("/")

    # Scan every segment except the last (filename)
    for i, seg in enumerate(segments[:-1]):
        low = seg.lower()
        if low in _SKIP_SEGMENTS:
            continue          # known container — look deeper
        if low in _ROOT_SEGMENTS:
            return "", "/".join(segments[i:])   # e.g. bin/... at game root
        if low.startswith("mod") or low.startswith("dlc"):
            nxt = segments[i + 1].lower() if i + 1 < len(segments) else ""
            if nxt in _SKIP_SEGMENTS or nxt in _ROOT_SEGMENTS:
                continue      # archive wrapper that merely starts with mod/dlc
            prefix = "mods" if low.startswith("mod") else "dlc"
            return prefix, "/".join(segments[i:])

    # No recognised folder found — deploy to game root as-is
    return "", norm

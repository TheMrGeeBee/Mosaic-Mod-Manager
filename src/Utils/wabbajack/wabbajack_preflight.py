"""Checks that run before a Wabbajack modlist install starts -- before any
profile is created or anything is downloaded -- so a modlist that can't be
installed is refused up front instead of half-installing.

Pure logic: the app gathers the inputs and shows the result. Results use
``collection_preflight``'s ``Check`` record so the existing preflight overlay
can display them unchanged.

Blocking: a game Mosaic can't install Wabbajack modlists for (or a modlist
for a different game than the active one), directives Mosaic can't build
yet, archives from download sources it doesn't recognise, and clearly
insufficient disk space. Everything else that may need the user (manual
downloads, Nexus without Premium, LoversLab without a login) is a warning.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from Utils.collections.collection_preflight import Check, check_disk_space

from .downloaders.game_file_source import resolve_game_file
from .wabbajack_bsa import support_problem
from .wabbajack_directives import UNSUPPORTED_DIRECTIVE_TYPES
from .wabbajack_manifest import (
    CreateBSADirective,
    GameFileSourceState,
    LoversLabState,
    ManualState,
    ModList,
    NexusState,
    TransformedTextureDirective,
    UnknownDirective,
    UnknownState,
    mosaic_game_for,
)

FIX_LOVERSLAB_LOGIN = "loverslab-login"

_DIRECTIVE_LABELS = {
    TransformedTextureDirective: "converted texture",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _archives_come(n: int) -> str:
    return f"{_plural(n, 'archive')} {'comes' if n == 1 else 'come'}"


def _plural_label(n: int, label: str) -> str:
    """``"2 unrecognised steps (X)"``: pluralise the noun before any
    parenthesised detail."""
    noun, sep, detail = label.partition(" (")
    return f"{_plural(n, noun)}{sep}{detail}"


def check_game(modlist: ModList, active_game_name: str) -> "list[Check]":
    target = mosaic_game_for(modlist.game_type)
    if target is None:
        return [Check("game", False, "Game not supported yet",
                      f"This modlist is for {modlist.game_type or 'an unknown game'}, and "
                      "Mosaic can't install Wabbajack modlists for that game yet.")]
    if active_game_name != target:
        return [Check("game", False, "Modlist is for a different game",
                      f"This modlist is for {target}. Switch to {target} in Mosaic, "
                      "then import it again.")]
    return [Check("game", True, f"Modlist is for {target}")]


def check_directives(modlist: ModList) -> "list[Check]":
    counts: Counter = Counter()
    for d in modlist.directives:
        if isinstance(d, UnknownDirective):
            counts[f"unrecognised step ({d.type_name.split(',')[0] or 'no type'})"] += 1
        elif isinstance(d, CreateBSADirective):
            problem = support_problem(d)
            if problem is not None:
                counts[f"unbuildable archive ({problem})"] += 1
        elif isinstance(d, UNSUPPORTED_DIRECTIVE_TYPES):
            counts[_DIRECTIVE_LABELS[type(d)]] += 1
    if not counts:
        return [Check("directives", True, "Mosaic can build every file in this modlist")]
    parts = "; ".join(_plural_label(n, label) for label, n in counts.most_common())
    return [Check("directives", False, "Modlist needs features Mosaic can't install yet",
                  f"{parts}. Installing it now would leave the setup incomplete.")]


def _is_loverslab_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host == "loverslab.com" or host.endswith(".loverslab.com")


def check_sources(modlist: ModList, *, nexus_premium: bool,
                  loverslab_logged_in: bool) -> "list[Check]":
    by_kind: Counter = Counter(type(a.state) for a in modlist.archives)
    checks: "list[Check]" = []
    if by_kind[UnknownState]:
        names = sorted({a.state.type_name.split(",")[0] for a in modlist.unsupported_archives})
        checks.append(Check(
            "sources", False, "Some downloads come from sources Mosaic doesn't recognise",
            f"{_plural(by_kind[UnknownState], 'archive')} ({', '.join(names)})."))
    if by_kind[ManualState]:
        checks.append(Check(
            "manual", False, "Some files must be downloaded in your browser",
            f"{_plural(by_kind[ManualState], 'archive')} can only be downloaded from a "
            "web page. During the install Mosaic shows each page; download the file "
            "in your browser and Mosaic picks it up from your downloads. Files "
            "already there are used straight away.",
            blocking=False))
    if by_kind[NexusState] and not nexus_premium:
        checks.append(Check(
            "nexus-premium", False, "Nexus downloads need Premium to run automatically",
            f"{_archives_come(by_kind[NexusState])} from Nexus Mods. Without "
            "Nexus Premium each one needs a click on the Nexus website.",
            blocking=False))
    ll = by_kind[LoversLabState] + sum(
        1 for a in modlist.archives
        if isinstance(a.state, ManualState) and _is_loverslab_url(a.state.url))
    if ll and not loverslab_logged_in:
        checks.append(Check(
            "loverslab", False, "LoversLab files",
            f"{_archives_come(ll)} from LoversLab. You can download them in your "
            "browser during the install, or log in to LoversLab here so Mosaic "
            "can try them automatically.",
            blocking=False, fix=FIX_LOVERSLAB_LOGIN))
    if not checks:
        checks.append(Check("sources", True, "Mosaic can download every archive"))
    return checks


def _game_files_mosaic_builds_from(modlist: ModList) -> "set[str]":
    """Hashes of game-file archives that feed files Mosaic actually installs
    (``mods/`` and ``profiles/``). Real case (Wasteland Genesis AE): all 21
    game files that differed on the user's install only fed the list's
    ``Game Root\\`` folder (the curator's own game files copied over the game,
    which Mosaic skips), so they must not block."""
    used: "set[str]" = set()
    for d in modlist.directives:
        ahp = getattr(d, "archive_hash_path", None) or []
        top = (getattr(d, "to", "") or "").replace("\\", "/").split("/", 1)[0].lower()
        if ahp and top in ("mods", "profiles"):
            used.add(ahp[0])
    return used


def check_game_version(modlist: ModList, game_root: "str | Path | None") -> "list[Check]":
    """A warning (never a block) when the game's exe reports a different
    version than the game files the modlist was built from: Mosaic can still
    build it, but its plugins may need that version to run. Switch with
    Wizard ▸ Game version (Steam)."""
    if not game_root:
        return []
    exes = [a for a in modlist.archives if isinstance(a.state, GameFileSourceState)
            and a.state.game_version and "/" not in a.state.game_file.replace("\\", "/")
            and a.state.game_file.lower().endswith(".exe")]
    if not exes:
        return []
    from Utils.wizard_support.pe_version import format_version, read_file_version
    target = exes[0].state.game_version
    path = resolve_game_file(game_root, exes[0].state.game_file)
    version = read_file_version(path) if path is not None else None
    if version is None:
        return []
    have = format_version(version)
    if have == target:
        return [Check("game-runtime", True, f"Your game is version {have}, as the modlist expects")]
    return [Check("game-runtime", False, "Different game version",
                  f"This modlist was built for game version {target}; yours is {have}. Mosaic "
                  "can install it, but its mods may need that version to run. Switch first "
                  "with Wizard ▸ Game version (Steam) if one is available for it.",
                  blocking=False)]


def check_game_files(modlist: ModList, game_root: "str | Path | None") -> "list[Check]":
    """Game files that files Mosaic installs are built from must be there and
    be the build the modlist was made from (sizes compared here, instantly;
    hashes during the install). Real case (2026-10-08): Wasteland Genesis AE,
    built from 1.11.221 game files -- a mismatch must show before a 168 GB
    download, not after. Game files that only feed parts Mosaic skips (e.g.
    ``Game Root\\``) are left out."""
    used = _game_files_mosaic_builds_from(modlist)
    wanted = [a for a in modlist.archives
              if isinstance(a.state, GameFileSourceState) and a.hash in used]
    if not wanted or not game_root:
        return []
    missing, differ = [], []
    for a in wanted:
        path = resolve_game_file(game_root, a.state.game_file)
        if path is None:
            missing.append(a)
            continue
        try:
            if a.size and path.stat().st_size != a.size:
                differ.append(a)
        except OSError:
            missing.append(a)
    if not missing and not differ:
        return [Check("game-files", True, "Game files the modlist uses are present")]

    def names(archives):
        shown = ", ".join(a.state.game_file for a in archives[:5])
        return shown + (f" and {len(archives) - 5} more" if len(archives) > 5 else "")

    versions = sorted({a.state.game_version for a in missing + differ if a.state.game_version})
    built = (f"This modlist was built from game version {', '.join(versions)}. "
             if versions else "")
    checks = []
    if differ:
        checks.append(Check(
            "game-version", False, "Your game version doesn't match this modlist",
            f"{built}{len(differ)} of the {len(wanted)} game files it builds from are different "
            f"in your game folder ({names(differ)}). Switch the game to that version (Wizard ▸ "
            "Game version (Steam)), or choose a modlist made for yours."))
    if missing:
        checks.append(Check(
            "game-files", False, "Files missing from your game folder",
            f"{names(missing)}. Install the DLC or Creation Club content they come "
            f"from.{(' ' + built.strip()) if built and not differ else ''}"))
    return checks


def run_preflight(modlist: ModList, *, active_game_name: str,
                  staging_root: "str | Path | None", cache_dir: "str | Path | None",
                  nexus_premium: bool, loverslab_logged_in: bool,
                  game_root: "str | Path | None" = None,
                  free_fn: "Callable[[Path], int] | None" = None) -> "list[Check]":
    """Every preflight check for ``modlist``, in display order."""
    return [
        *check_game(modlist, active_game_name),
        *check_game_files(modlist, game_root),
        *check_game_version(modlist, game_root),
        *check_directives(modlist),
        *check_sources(modlist, nexus_premium=nexus_premium,
                       loverslab_logged_in=loverslab_logged_in),
        *check_disk_space(staging_root, cache_dir, modlist.total_install_size,
                          modlist.total_archive_size, free_fn=free_fn, noun="modlist"),
    ]

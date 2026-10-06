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
            "manual", False, "Some files must be downloaded by hand",
            f"{_plural(by_kind[ManualState], 'archive')} can only be downloaded from a "
            "web page; Mosaic will open each one for you during the install.",
            blocking=False))
    if by_kind[NexusState] and not nexus_premium:
        checks.append(Check(
            "nexus-premium", False, "Nexus downloads need Premium to run automatically",
            f"{_archives_come(by_kind[NexusState])} from Nexus Mods. Without "
            "Nexus Premium each one needs a click on the Nexus website.",
            blocking=False))
    if by_kind[LoversLabState] and not loverslab_logged_in:
        checks.append(Check(
            "loverslab", False, "Log in to LoversLab",
            f"{_archives_come(by_kind[LoversLabState])} from LoversLab, which "
            "needs you to be logged in.",
            blocking=False, fix=FIX_LOVERSLAB_LOGIN))
    if not checks:
        checks.append(Check("sources", True, "Mosaic can download every archive"))
    return checks


def check_game_files(modlist: ModList, game_root: "str | Path | None") -> "list[Check]":
    """Files the modlist takes from the game install must exist there. (Their
    hashes are checked during the install, which is when they're read.)"""
    wanted = [a.state for a in modlist.archives if isinstance(a.state, GameFileSourceState)]
    if not wanted or not game_root:
        return []
    missing = [s for s in wanted if resolve_game_file(game_root, s.game_file) is None]
    if not missing:
        return [Check("game-files", True, "Game files the modlist uses are present")]
    names = ", ".join(s.game_file for s in missing[:5])
    more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
    versions = sorted({s.game_version for s in missing if s.game_version})
    expects = f" The modlist was built from game version {', '.join(versions)}." if versions else ""
    return [Check("game-files", False, "Files missing from your game folder",
                  f"{names}{more}. Install the DLC or Creation Club content they come "
                  f"from.{expects}")]


def run_preflight(modlist: ModList, *, active_game_name: str,
                  staging_root: "str | Path | None", cache_dir: "str | Path | None",
                  nexus_premium: bool, loverslab_logged_in: bool,
                  game_root: "str | Path | None" = None,
                  free_fn: "Callable[[Path], int] | None" = None) -> "list[Check]":
    """Every preflight check for ``modlist``, in display order."""
    return [
        *check_game(modlist, active_game_name),
        *check_game_files(modlist, game_root),
        *check_directives(modlist),
        *check_sources(modlist, nexus_premium=nexus_premium,
                       loverslab_logged_in=loverslab_logged_in),
        *check_disk_space(staging_root, cache_dir, modlist.total_install_size,
                          modlist.total_archive_size, free_fn=free_fn, noun="modlist"),
    ]

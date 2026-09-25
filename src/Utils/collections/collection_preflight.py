"""
collection_preflight.py
Checks that run BEFORE a collection install starts (before any profile is created
or anything is downloaded), so a 2,000-mod install isn't built on a base that can
never work.

Pure logic — no Qt, no network. The app (``gui_qt/app.py``) gathers the inputs,
shows the result and drives the fix; this module only decides.

Today this covers Skyrim Special Edition's runtime problem: Bethesda changes
``SkyrimSE.exe`` whenever Creation Club content changes, Steam serves the newest
build (1.7.104), but SKSE64 — which almost every collection needs — supports only
1.6.1170. A collection that needs the older runtime lists a runtime-swap mod (SRS,
Nexus 189855) that patches the game. Mosaic applies those patches itself (see
``Utils.modding_tools.skyrim_runtime``) and therefore never installs that mod.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from Utils.modding_tools import skyrim_runtime as sr
from Utils.wizard_support.pe_version import Version, format_version

FIX_RUNTIME_SWAP = "runtime-swap"


@dataclass(frozen=True)
class Check:
    """One line of the preflight report."""
    key: str
    ok: bool
    title: str
    detail: str = ""
    blocking: bool = True           # only meaningful when not ok
    fix: str | None = None          # a FIX_* id Mosaic can perform, or None


def blocking_failures(checks: Iterable[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and c.blocking]


def warnings(checks: Iterable[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and not c.blocking]


def fixable(checks: Iterable[Check]) -> list[Check]:
    return [c for c in blocking_failures(checks) if c.fix]


# ---- the runtime-swap mod ------------------------------------------------------

def is_skyrim_se(game) -> bool:
    return str(getattr(game, "steam_id", "")) == str(sr.STEAM_APP_ID)


def _mod_id(mod) -> int:
    try:
        return int(getattr(mod, "mod_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def find_runtime_swap_mod(mods: Iterable) -> object | None:
    """The collection's runtime-swap mod (a NexusCollectionMod), if it lists one."""
    for mod in mods:
        if _mod_id(mod) in sr.RUNTIME_SWAP_MOD_IDS:
            return mod
    return None


def without_runtime_swap_mods(mods: Iterable) -> tuple[list, list]:
    """(mods to install, runtime-swap mods left out). Mosaic applies those patches
    itself, so installing the mod would only add a version.dll hook that fights
    the deploy."""
    kept, dropped = [], []
    for mod in mods:
        (dropped if _mod_id(mod) in sr.RUNTIME_SWAP_MOD_IDS else kept).append(mod)
    return kept, dropped


def manifest_game_versions(manifest: dict | None) -> list[str]:
    """``info.gameVersions`` from a collection manifest, e.g. ['1.7.104.0']."""
    info = (manifest or {}).get("info") if isinstance(manifest, dict) else None
    versions = (info or {}).get("gameVersions") if isinstance(info, dict) else None
    return [str(v) for v in versions] if isinstance(versions, list) else []


def find_swap_archive(mod, search_dirs: Iterable["str | Path"]) -> Path | None:
    """The swap mod's archive if a complete copy is on disk: matched exactly by its
    ``.fileid`` sidecar (Mosaic's cache), or — for a manual download with no
    sidecar — by size, mod id in the name and the collection's md5. Truncated
    downloads are reported as not found."""
    from Nexus.nexus_download import _find_cached_archive
    for directory in search_dirs:
        try:
            found, complete = _find_cached_archive(
                Path(directory), str(getattr(mod, "mod_name", "") or ""),
                int(getattr(mod, "size_bytes", 0) or 0), _mod_id(mod),
                int(getattr(mod, "file_id", 0) or 0), str(getattr(mod, "md5", "") or ""))
        except OSError:
            continue
        if found is not None and complete:
            return Path(found)
    return None


# ---- the checks ----------------------------------------------------------------

def check_skyrim_runtime(
    game_root: "str | Path",
    state_dir: "str | Path",
    transition: "sr.Transition | None",
    *,
    game_running: bool,
    collection_versions: list[str] | None = None,
) -> list[Check]:
    """Is Skyrim at the runtime this collection's runtime-swap mod targets?

    *transition* is None until the swap archive has been found/fetched — then the
    only honest answer is "not verified yet", which the fix resolves by fetching
    it and asking again.
    """
    built_for = (collection_versions or [None])[0]
    if transition is None:
        return [Check(
            "skyrim-runtime", False, "Skyrim game version not verified yet",
            "This collection needs an older Skyrim runtime than Steam's current one "
            "(SKSE64 doesn't support the newest). Mosaic will fetch the collection's "
            "runtime patch and switch the game if it has to."
            + (f" (The collection was built for {built_for}.)" if built_for else ""),
            fix=FIX_RUNTIME_SWAP)]

    a = sr.assess(game_root, state_dir, transition,
                  hpatchz=sr.find_hpatchz() or "available-on-demand",
                  game_running=game_running)
    src, dst = format_version(transition.source), format_version(transition.target)
    if a.state == "target":
        return [Check("skyrim-runtime", True, f"Skyrim is {dst}",
                      "Matches the runtime this collection's SKSE needs.")]
    if a.state == "source":
        checks = [Check(
            "skyrim-runtime", False, f"Skyrim is {src}, this collection needs {dst}",
            "SKSE64 and the collection's plugins target the older runtime. Mosaic can "
            "switch the game in a few seconds: your files are backed up and every "
            "patched file is checked against its expected hash. Steam updates undo it; "
            "the check simply runs again next time.",
            fix=FIX_RUNTIME_SWAP)]
        if game_running:
            checks.append(Check("skyrim-running", False, "Skyrim is running",
                                "Close the game before switching its version."))
        return checks
    shown = format_version(a.version) if a.version else "unreadable"
    return [Check(
        "skyrim-runtime", False, f"Unsupported Skyrim version ({shown})",
        f"This collection can switch {src} to {dst}, but SkyrimSE.exe is {shown}. "
        "In Steam use Properties > Installed Files > Verify integrity of game files "
        "(that restores the current build), then try again.")]


def check_disk_space(
    staging_root: "str | Path | None",
    cache_dir: "str | Path | None",
    install_size: int,
    archives_size: int,
    *,
    free_fn: Callable[[Path], int] | None = None,
) -> list[Check]:
    """Free space for the extracted mods (block) and archives on top (warn).

    Sizes come from the collection (installed size / sum of archive sizes). The
    filesystem may compress, so only "clearly doesn't fit" blocks.
    """
    if install_size <= 0:
        return []
    free = free_fn or _free_bytes
    root = Path(staging_root) if staging_root else None
    if root is None:
        return []
    have = free(root)
    gib = 1 << 30
    if have < install_size:
        return [Check("disk-space", False, "Not enough free disk space",
                      f"The collection needs about {install_size / gib:.0f} GB for its mods "
                      f"and {have / gib:.0f} GB is free where they are staged.")]
    same_fs = cache_dir is not None and _same_fs(root, Path(cache_dir))
    need_both = install_size + (archives_size if same_fs else 0)
    if archives_size and have < need_both:
        return [Check("disk-space", False, "Disk space is tight",
                      f"Mods (about {install_size / gib:.0f} GB) plus their downloaded "
                      f"archives (about {archives_size / gib:.0f} GB) may not fit in the "
                      f"{have / gib:.0f} GB free. It may still work if the filesystem "
                      "compresses; turn on clearing archives after install to be safe.",
                      blocking=False)]
    return [Check("disk-space", True, "Enough free disk space",
                  f"{have / gib:.0f} GB free for about {install_size / gib:.0f} GB of mods.")]


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _same_fs(a: Path, b: Path) -> bool:
    def probe(p: Path) -> Path:
        while not p.exists() and p != p.parent:
            p = p.parent
        return p
    try:
        return probe(a).stat().st_dev == probe(b).stat().st_dev
    except OSError:
        return False


def run_preflight(
    *,
    game,
    mods: Iterable,
    manifest: dict | None,
    game_root: "str | Path | None",
    state_dir: "str | Path",
    transition: "sr.Transition | None",
    game_running: bool,
    staging_root: "str | Path | None" = None,
    cache_dir: "str | Path | None" = None,
    install_size: int = 0,
    free_fn: Callable[[Path], int] | None = None,
) -> list[Check]:
    """All checks for installing *mods* into *game*. Empty list = nothing to say."""
    mods = list(mods)
    checks: list[Check] = []
    if is_skyrim_se(game) and game_root and find_runtime_swap_mod(mods) is not None:
        checks += check_skyrim_runtime(
            game_root, state_dir, transition, game_running=game_running,
            collection_versions=manifest_game_versions(manifest))
    archives = sum(int(getattr(m, "size_bytes", 0) or 0) for m in mods)
    checks += check_disk_space(staging_root, cache_dir, install_size, archives, free_fn=free_fn)
    return checks

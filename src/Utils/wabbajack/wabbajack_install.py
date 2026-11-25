"""Installs a parsed Wabbajack modlist into a Mosaic profile.

Toolkit-neutral, modelled on ``Utils.collections.collection_install``: the Qt
layer passes a :class:`WabbajackInstallCallbacks` whose fields are plain
``Signal.emit``s and runs :func:`run_wabbajack_install` on a worker thread.
The two ``request_*`` callbacks block the worker while the UI asks the user
something, like Collections' ``resolve_fomod``.

Steps:

1. **Classify** every directive by destination (:func:`classify_directives`).
   ``mods/<Name>/...`` builds into Mosaic's staging folder ``<Name>``.
   ``profiles/<Profile>/...`` builds into a scratch folder for step 4 --
   only the chosen profile is used (see :func:`profile_choices`), and MO2's
   timestamped backups in it are skipped. ``TEMP_BSA_FILES/<TempID>/...`` builds into scratch as the input
   of a rebuilt BSA/BA2. Anything else (MO2's own files, a stock-game copy, ...) isn't
   placed and is listed in the report; how those map onto Mosaic is still
   open (the plan's "profile-file translation" risk).
2. **Download** every archive a placed directive reads from. Automatic
   sources run in parallel, smallest first; anything with no automatic
   downloader, or whose automatic download failed, is then offered to the
   user one at a time through ``request_manual_download``. Every archive is
   checked against ``Archive.hash`` before use.
3. **Build** each placed directive (:func:`wabbajack_directives.apply_directive`),
   packing ``CreateBSA`` archives last, from their already-built inputs.
4. **Profile**: the curator's ``modlist.txt`` (filtered to mods that were
   actually built) becomes the profile's -- and mods it doesn't list at all
   (another profile's variants) were never downloaded or built, and ``plugins.txt``/``loadorder.txt``
   are copied as-is. There's deliberately no LOOT sort: a Wabbajack load
   order is hand-tuned and reproducing it exactly is the point. The game's
   own INIs (Skyrim.ini, SkyrimPrefs.ini, ...) go into the profile's
   ``ini files`` folder, with MO2's ``__MO_Saves`` save path set back to the
   game's, and the profile is switched to its own INIs. Other profile files
   are kept in ``<profile>/wabbajack_profile_files`` and listed in the report
   rather than applied.
5. **Provenance**: the profile records which modlist it came from, each
   built mod's ``meta.ini`` gets a ``[Wabbajack]`` section, and the mod index
   is rebuilt so the app sees the new mods without a manual refresh.
"""
from __future__ import annotations

import configparser
import re
import shutil
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from Utils.app_log import noop_log as _noop
from Utils.downloads.download_scheduler import order_by_size, run_smallest_first
from Utils.mods.mo2_separator_meta import scan_separator_colors, scan_separator_nesting
from Utils.mods.modlist import (
    ModEntry,
    mo2_to_natural_order,
    read_modlist,
    write_modlist,
)
from Utils.plugins.plugins import invalidate_plugins_cache
from Utils.profile.profile_state import (
    read_separator_colors,
    write_separator_colors,
    write_separator_nesting,
    write_wabbajack_modlist_info,
)

from . import wabbajack_file
from .downloaders import resolve_downloader
from .downloaders.game_file_source import fetch_game_file, runtime_swap_originals
from .downloaders.nexus_source import download_nexus
from .wabbajack_directives import apply_directive
from .wabbajack_hash import cached_hash_file, flush_hash_cache, hashes_match
from .wabbajack_manifest import (
    Archive,
    CreateBSADirective,
    Directive,
    FromArchiveDirective,
    GameFileSourceState,
    InlineFileDirective,
    ModList,
    NexusState,
    PatchedFromArchiveDirective,
    RemappedInlineFileDirective,
    mosaic_game_for,
)
from .wabbajack_vfs import ArchiveIndex

PROFILE_FILES_DIR = "wabbajack_profile_files"
# Wabbajack builds the loose files that go into a rebuilt BSA/BA2 under
# TEMP_BSA_FILES/<TempID>/ before its CreateBSA directive packs them.
_BSA_TEMP_DIR = "temp_bsa_files"
_LOAD_ORDER_FILES = ("plugins.txt", "loadorder.txt")
# Folders a list ships next to mods/ for the user to copy into the game folder
# by hand, per its README -- installed as a root-folder mod of this name
# instead. Waters of Life's holds FOSE, updated Ogg Vorbis libraries and the
# Fallout Anniversary Patcher. Keyed by the folder name, lower-cased.
_MANUAL_ROOT_FOLDERS = {
    "__files requiring manual install": "Files Requiring Manual Install",
}
# Never installed from those folders: Proton brings its own DXVK, and a list's
# d3d9.dll in the game folder overrides it (Nuclear Sunset's Linux guide: "you
# will crash any time you leave an interior").
_PROTON_SUPPLIED = frozenset({"d3d9.dll", "dxvk.conf"})
# MO2 points the game's save folder at its own __MO_Saves\ when a profile keeps
# its own saves; without MO2 that folder doesn't exist and the saves vanish.
_MO2_SAVE_PATH = re.compile(
    r"(?im)^([ \t]*sLocalSavePath[ \t]*=[ \t]*)__MO_Saves[\\/]?[ \t]*(\r?)$")
# Games whose mods aren't laid out under Data/, so the Root Builder -> Data/
# relocation (Bethesda layout) must not run.
_NO_DATA_FOLDER_GAMES = frozenset({"Cyberpunk 2077",
                                   "Vampire: The Masquerade - Bloodlines"})
# Games where a Root Builder mod's Root/ folder holds two different
# destinations, so the mod has to be split (see split_root_builder_mods).
_SPLIT_ROOT_BUILDER_GAMES = frozenset({"Vampire: The Masquerade - Bloodlines"})
# Games whose MO2 plugin puts a mod's files deeper than Mosaic's handler
# does: the folder MO2 uses, relative to the one Mosaic deploys into. Dragon
# Age's MO2 data folder is packages/core/override; Mosaic deploys into the
# Dragon Age Documents folder (it also places AddIns/ and Offers/).
_MO2_DATA_SUBDIR = {"Dragon Age Origins": "packages/core/override"}
# Suffix for the sibling mod that carries a split mod's game-root files.
ROOT_MOD_SUFFIX = " - Root"
# Wabbajack keeps a private copy of the game under this folder so its own
# installs never touch the real one. Mosaic deploys over the real install, so
# the copy is skipped -- on the first VtMB list all 4620 of its files that came
# from a downloaded archive hashed identical to a clean install.
_STOCK_GAME_DIR = "StockGame"


@dataclass
class WabbajackInstallCallbacks:
    on_status: Callable[[str], None] = _noop
    on_log: Callable[[str], None] = _noop
    on_download_start: Callable[[str, str, int], None] = _noop    # hash, name, size
    on_download_progress: Callable[[str, int, int], None] = _noop  # hash, cur, total
    on_download_finish: Callable[[str, bool], None] = _noop        # hash, ok
    on_build_progress: Callable[[int, int], None] = _noop          # done, total
    # How many archives are being built at once, and why (wabbajack_workers).
    on_workers: Callable[[int, str], None] = _noop                 # count, reason
    # Once known: how many archives (and bytes) this run actually needs --
    # fewer than the modlist's when resuming an install.
    on_archives_needed: Callable[[int, int], None] = _noop         # count, bytes
    # Blocking: ask the user to fetch an archive by hand (with why the
    # automatic download didn't work); returns its path, or None to skip it.
    # Unset means "can't ask" -- the archive just fails.
    request_manual_download: "Callable[[Archive, str], Path | None] | None" = None
    # Blocking: show the LoversLab login form; True once logged in.
    request_loverslab_login: "Callable[[], bool] | None" = None


@dataclass
class WabbajackInstallControl:
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass
class WabbajackInstallReport:
    installed_mods: "list[str]" = field(default_factory=list)
    failed_archives: "list[tuple[str, str]]" = field(default_factory=list)    # name, why
    failed_directives: "list[tuple[str, str]]" = field(default_factory=list)  # to, why
    unplaced_files: "list[str]" = field(default_factory=list)
    held_profile_files: "list[str]" = field(default_factory=list)
    # The game's own INIs, put into the profile's "ini files" folder.
    applied_profile_inis: "list[str]" = field(default_factory=list)
    # Mods only other profiles use: not downloaded or built.
    skipped_mods: "list[str]" = field(default_factory=list)
    # MO2 Root Builder mods converted to root-folder mods.
    root_builder_mods: "list[str]" = field(default_factory=list)
    # The modlist's MO2 profiles this run installed, one Mosaic profile each.
    profiles_used: "list[str]" = field(default_factory=list)
    cancelled: bool = False
    # The list's own game folder Mosaic built (or reused) for these profiles.
    stock_game: str = ""

    @property
    def profile_used(self) -> str:
        return self.profiles_used[0] if self.profiles_used else ""

    @property
    def ok(self) -> bool:
        return not (self.cancelled or self.failed_archives or self.failed_directives)


# ---------------------------------------------------------------------------
# Step 1: classify
# ---------------------------------------------------------------------------


@dataclass
class InstallPlan:
    mod_directives: "list[Directive]" = field(default_factory=list)      # to = <Name>/<rel>
    profile_directives: "list[Directive]" = field(default_factory=list)  # to = <Profile>/<rel>
    bsa_inputs: "list[Directive]" = field(default_factory=list)          # to = <TempID>/<rel>
    # The list's own game folder (Stock Game), when it's being built: to = <rel>.
    stock_directives: "list[Directive]" = field(default_factory=list)
    profile_names: "list[str]" = field(default_factory=list)
    unplaced: "list[str]" = field(default_factory=list)
    # Mods made from a list's game-folder files (_MANUAL_ROOT_FOLDERS):
    # installed as root-folder mods, enabled, whatever the profile lists.
    extra_root_mods: "list[str]" = field(default_factory=list)

    @property
    def profile_name(self) -> str:
        return self.profile_names[0] if self.profile_names else ""

    def archive_hashes(self) -> "set[str]":
        return {d.archive_hash_path[0]
                for d in (self.mod_directives + self.profile_directives + self.bsa_inputs
                          + self.stock_directives)
                if isinstance(d, (FromArchiveDirective, PatchedFromArchiveDirective))
                and d.archive_hash_path}


class ProfileChoiceRequired(ValueError):
    """The modlist ships several MO2 profiles and none was chosen."""


def _norm_to(d) -> "list[str]":
    """``To`` split into path parts, with empty parts dropped (``a\\\\b``,
    ``/a``). An empty part would otherwise make the joined path absolute
    and escape the destination folder."""
    return [p for p in (getattr(d, "to", "") or "").replace("\\", "/").split("/") if p]


def _unsafe(parts: "list[str]") -> bool:
    return any(p in (".", "..") for p in parts)


def profile_choices(modlist: ModList) -> "list[str]":
    """The modlist's MO2 profiles a user can install, sorted. A profile
    folder counts if it has a ``modlist.txt``; when none does, every
    profile folder counts. More than one means the user must choose (e.g.
    Horizon's "Horizon - GOG" and "Horizon - Steam" differ in mods and
    plugins), so :func:`classify_directives` never picks on its own."""
    folders: "dict[str, bool]" = {}
    for d in modlist.directives:
        parts = _norm_to(d)
        if len(parts) >= 3 and parts[0].lower() == "profiles":
            has = folders.get(parts[1], False)
            folders[parts[1]] = has or (len(parts) == 3 and parts[2].lower() == "modlist.txt")
    with_modlist = sorted(n for n, has in folders.items() if has)
    return with_modlist or sorted(folders)


# MO2 keeps timestamped copies like loadorder.txt.2025_12_17_21_16_42 in
# profile folders; they're history, not part of the setup.
_PROFILE_BACKUP_RE = re.compile(r"\.\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}$")


def classify_directives(modlist: ModList,
                        profiles: "str | Sequence[str] | None" = None, *,
                        stock_dir: "str | None" = None,
                        root_dirs: "dict[str, str] | None" = None) -> InstallPlan:
    """Split the modlist's directives by where they land (see module doc).
    Wabbajack writes ``To`` with Windows separators; they're normalised.
    ``profiles`` names the MO2 profile(s) to install -- one name, or several
    when the modlist ships variants the user wants side by side (the VtMB
    list "Fashionista - Steam" has 17 that share one pool of 405 mods). It
    may be omitted only when :func:`profile_choices` offers at most one,
    otherwise :class:`ProfileChoiceRequired` is raised. A kept profile
    directive's ``to`` starts with its profile name, so one run can build
    several profiles' files into separate scratch folders.

    ``stock_dir`` names the list's own game folder (see
    ``wabbajack_stock_game.stock_game_dir``) when it is to be built as a game
    copy; its directives then go to ``stock_directives`` instead of
    ``unplaced``. ``root_dirs`` maps more top-level folders holding game-folder
    files (see ``wabbajack_stock_game.partial_stock_dir``) to the name of the
    root-folder mod they become, like ``_MANUAL_ROOT_FOLDERS``."""
    choices = profile_choices(modlist)
    if profiles is None:
        if len(choices) > 1:
            raise ProfileChoiceRequired(
                f"choose one of the modlist's profiles: {', '.join(choices)}")
        wanted = [choices[0]] if choices else []
    else:
        wanted = [profiles] if isinstance(profiles, str) else list(profiles)
        unknown = [p for p in wanted if p not in choices]
        if unknown:
            raise ValueError(f"{unknown[0]!r} isn't one of the modlist's profiles")

    plan = InstallPlan(profile_names=wanted)
    keep = set(wanted)
    stock = stock_dir.lower() if stock_dir else None
    root_folders = {**_MANUAL_ROOT_FOLDERS,
                    **{k.lower(): v for k, v in (root_dirs or {}).items()}}
    for d in modlist.directives:
        parts = _norm_to(d)
        to = "/".join(parts)
        head = parts[0].lower() if parts else ""
        if _unsafe(parts):
            plan.unplaced.append(to)  # never built: it would leave its folder
        elif head == "mods" and len(parts) >= 3:
            plan.mod_directives.append(replace(d, to="/".join(parts[1:])))
        elif (head == "profiles" and len(parts) >= 3 and parts[1] in keep
              and not _PROFILE_BACKUP_RE.search(parts[-1])):
            plan.profile_directives.append(replace(d, to="/".join(parts[1:])))
        elif head == _BSA_TEMP_DIR and len(parts) >= 3:
            plan.bsa_inputs.append(replace(d, to="/".join(parts[1:])))
        elif stock is not None and head == stock and len(parts) >= 2:
            plan.stock_directives.append(replace(d, to="/".join(parts[1:])))
        elif (head in root_folders and len(parts) == 2
              and parts[1].lower() in _PROTON_SUPPLIED):
            plan.unplaced.append(to)
        elif head in root_folders and len(parts) >= 2:
            name = root_folders[head]
            plan.mod_directives.append(replace(d, to=f"{name}/" + "/".join(parts[1:])))
            if name not in plan.extra_root_mods:
                plan.extra_root_mods.append(name)
        else:
            plan.unplaced.append(to)
    return plan


def _is_separator_folder(name: str) -> bool:
    """True for an MO2 ``mods/<Name>_separator`` folder -- a heading's
    ``meta.ini``, not a mod."""
    return name.lower().endswith("_separator")


def limit_to_profile_mods(plan: InstallPlan, listed: "set[str]") -> "list[str]":
    """Keep only the mods the chosen profile's ``modlist.txt`` lists
    (enabled or disabled), dropping every other ``mods/<Name>`` directive and
    the ``TEMP_BSA_FILES`` inputs that only their archives used -- so their
    archives aren't downloaded either. A list with GOG and Steam profiles
    ships both stores' variants of some mods (Horizon: two Unofficial
    Fallout 4 Patch builds providing the same plugin); only the chosen
    profile's belong in the install. Names match case-insensitively.
    Returns the skipped mod names, sorted, not counting MO2 separator
    folders (``*_separator``): *listed* carries those too (see
    ``_profile_mod_names``) because their ``meta.ini`` holds the separator's
    colour, and an unlisted one belongs to another profile, so neither kind
    is a mod the user asked for and left out."""
    wanted = {n.lower() for n in listed} | {n.lower() for n in plan.extra_root_mods}
    kept, skipped = [], set()
    for d in plan.mod_directives:
        mod = d.to.split("/", 1)[0]
        if mod.lower() in wanted:
            kept.append(d)
        elif not _is_separator_folder(mod):
            # Another profile's separator folder: dropped like its mods, but
            # not reported as skipped, since it isn't a mod.
            skipped.add(mod)
    plan.mod_directives = kept
    temp_ids = {d.temp_id.lower() for d in kept + plan.stock_directives
                if isinstance(d, CreateBSADirective)}
    plan.bsa_inputs = [d for d in plan.bsa_inputs
                       if d.to.split("/", 1)[0].lower() in temp_ids]
    return sorted(skipped)


def apply_root_builder_layout(plan: InstallPlan, *,
                              data_folder: "bool | str" = True) -> "list[str]":
    """Convert MO2 Root Builder mods into Mosaic root-folder mods.

    Root Builder puts files that belong in the game's install folder under
    ``<Mod>/Root/`` (real case, Horizon 1.4.4: F4SE's ``f4se_loader.exe``
    and DLLs, a launcher ``.bat``, a downgrader). Mosaic's equivalent is a
    whole-mod ``rootFolder=true`` flag (deployed verbatim from the game root),
    so in such a mod ``Root/x`` becomes ``x`` and every other file moves
    under ``Data/`` (``meta.ini`` stays put; a path already under ``Data/``
    is kept). Returns the converted mod names, sorted; the caller flags
    them in ``meta.ini``. ``data_folder`` names that folder -- ``True`` is
    ``Data`` -- for a game whose mods deploy into a subfolder of the game
    (Stardew's ``Mods``); ``False`` (games whose mods deploy to the game
    folder itself, like Cyberpunk 2077) means only ``Root/x`` -> ``x`` happens
    and other files stay put."""
    data_dir = "Data" if data_folder is True else (data_folder or "")
    by_mod: "dict[str, list[str]]" = {}
    for d in plan.mod_directives:
        mod, _, rest = d.to.partition("/")
        by_mod.setdefault(mod, []).append(rest)
    root_mods = {mod for mod, rests in by_mod.items()
                 if any(r.split("/", 1)[0].lower() == "root" and "/" in r for r in rests)}
    if not root_mods:
        return []

    def relocate(to: str) -> str:
        mod, _, rest = to.partition("/")
        if mod not in root_mods:
            return to
        head, _, tail = rest.partition("/")
        if head.lower() == "root" and tail:
            return f"{mod}/{tail}"
        if (not data_dir or rest.lower() == "meta.ini"
                or rest.lower().startswith(data_dir.lower() + "/")):
            return to
        return f"{mod}/{data_dir}/{rest}"

    plan.mod_directives = [replace(d, to=relocate(d.to)) for d in plan.mod_directives]
    return sorted(root_mods)


def split_root_builder_mods(plan: InstallPlan, *, overlay_dirs: "set[str]"
                           ) -> "tuple[list[str], list[str], int]":
    """Split Root Builder mods whose ``Root/`` folder holds two destinations.

    Vampire: The Masquerade - Bloodlines is the case this exists for. Its
    Root Builder mods put ``Root/Vampire/x`` -- files that patch the game's
    vanilla data folder -- next to ``Root/Bin/…``, ``Root/Loader.exe`` and
    ReShade payloads, which really do belong in the install root.

    ``overlay_dirs`` is the handler's ``mod_folder_strip_prefixes``: those
    prefixes are its own declaration of "this folder means the game's data",
    so ``Root/<one of them>/x`` folds into the mod as ``x``. Mosaic never
    writes the vanilla folder, and a profile's ``-game`` folder overlays it on
    the engine's search path, so the files still reach the game. Where the mod
    already ships its own ``x`` that copy wins and the overlay one is dropped
    -- what the engine does too, the data folder beating the vanilla folder.
    325 of this modlist's 676 such pairs genuinely differ, so the choice
    matters, and dropping is what keeps the two from racing to the same
    staged path.

    The rest of ``Root/`` moves to a sibling mod ``"<Mod> - Root"``, because
    Mosaic's ``rootFolder`` flag is per mod (``filemap.py`` routes a flagged
    mod's whole tree into the separate game-root namespace, unstripped) and a
    mod cannot be both. Leaving those files in the parent would strand them in
    the ``-game`` folder, where the Loader and ReShade are never read.

    Returns ``(root_mods, parents, dropped)``: the generated root mods, the
    mod each one was split off (same order), and how many overlay files the
    mod's own copy displaced.
    """
    own: "dict[str, set[str]]" = {}
    for d in plan.mod_directives:
        mod, _, rest = d.to.partition("/")
        if rest.split("/", 1)[0].lower() != "root":
            own.setdefault(mod, set()).add(rest.lower())

    overlay = {d.lower() for d in overlay_dirs}
    kept: "list[Directive]" = []
    parent_of: "dict[str, str]" = {}
    dropped = 0
    for d in plan.mod_directives:
        mod, _, rest = d.to.partition("/")
        head, _, tail = rest.partition("/")
        if head.lower() != "root" or not tail:
            kept.append(d)
            continue
        sub, _, inner = tail.partition("/")
        if sub.lower() in overlay and inner:
            if inner.lower() in own.get(mod, ()):
                dropped += 1
                continue
            kept.append(replace(d, to=f"{mod}/{inner}"))
        else:
            root = mod + ROOT_MOD_SUFFIX
            parent_of[root] = mod
            kept.append(replace(d, to=f"{root}/{tail}"))
    plan.mod_directives = kept
    roots = sorted(parent_of)
    return roots, [parent_of[r] for r in roots], dropped


@dataclass
class RootBuilderLayout:
    kind: str                       # "split" | "no_data" | "data"
    converted: "list[str]"          # split: the generated root mods; else the converted mods
    root_parents: "dict[str, str]"  # split only: root mod -> the mod it came from
    folded: int = 0                 # split only: overlay files the mod's own copy replaced


def layout_root_builder_mods(plan: InstallPlan, modlist: ModList, game) -> RootBuilderLayout:
    """Lay the plan's MO2 Root Builder mods out the way Mosaic installs them,
    for the modlist's game. Shared by the install and the install check, which
    must agree on where every file went."""
    target_game = mosaic_game_for(modlist.game_type)
    if target_game in _SPLIT_ROOT_BUILDER_GAMES:
        # Root/ holds two destinations here, so the mods are split in two.
        overlay_dirs = set(getattr(game, "mod_folder_strip_prefixes", None) or ())
        roots, parents, folded = split_root_builder_mods(plan, overlay_dirs=overlay_dirs)
        return RootBuilderLayout("split", roots, dict(zip(roots, parents)), folded)
    _prefix_mo2_data_subdir(plan, target_game)
    data_dir = "" if target_game in _NO_DATA_FOLDER_GAMES else _mod_data_subdir(game)
    if not data_dir:
        # Everything deploys to the game folder already, so these mods need no
        # root-folder flag; only their Root/ files must move up a level.
        return RootBuilderLayout("no_data", apply_root_builder_layout(plan, data_folder=False), {})
    return RootBuilderLayout("data", apply_root_builder_layout(plan, data_folder=data_dir), {})


def _mod_data_subdir(game) -> str:
    """The folder, relative to the game folder, Mosaic deploys this game's
    mods into ("Data" for Bethesda games, "Mods" for Stardew); "" when that is
    the game folder itself or lies outside it (a Documents folder). A Root
    Builder mod's other files go under it. Defaults to "Data"."""
    try:
        data, root = game.get_mod_data_path(), game.get_game_path()
    except Exception:
        return "Data"
    if data is None or root is None:
        return "Data"
    try:
        rel = Path(data).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return ""                       # outside the game folder
    return rel.as_posix() if str(rel) != "." else ""


def _prefix_mo2_data_subdir(plan: InstallPlan, target_game: "str | None") -> None:
    """Move every mod file under the game's ``_MO2_DATA_SUBDIR`` folder, so it
    lands where MO2 would have put it. ``meta.ini`` stays put."""
    sub = _MO2_DATA_SUBDIR.get(target_game or "")
    if not sub:
        return

    def moved(to: str) -> str:
        mod, _, rest = to.partition("/")
        if (mod in plan.extra_root_mods or rest.lower() == "meta.ini"
                or rest.lower().startswith(sub.lower() + "/")):
            return to
        return f"{mod}/{sub}/{rest}"

    plan.mod_directives = [replace(d, to=moved(d.to)) for d in plan.mod_directives]


def _profile_mod_names(plan: InstallPlan, wabbajack_path) -> "set[str] | None":
    """The union of the chosen profiles' ``modlist.txt`` entry names, read
    straight from the ``.wabbajack`` before anything is downloaded. ``None``
    when none of them ships an inline ``modlist.txt`` (nothing to filter by);
    a profile that is missing one simply contributes nothing.

    Separator entries are included on purpose. MO2 keeps a separator's colour
    and nesting in ``mods/<Name>_separator/meta.ini``, so leaving them out of
    the filter drops those directives and the install lands with every
    separator uncoloured -- which is exactly what the first real VtMB run
    produced. They are inline files of a few hundred bytes, so keeping them
    costs no download."""
    entries = [d for d in plan.profile_directives
               if Path(d.to).name.lower() == "modlist.txt"
               and isinstance(d, (InlineFileDirective, RemappedInlineFileDirective))]
    if not entries:
        return None
    names: "set[str]" = set()
    found = False
    with tempfile.TemporaryDirectory() as td:
        for i, entry in enumerate(entries):
            dest = Path(td) / f"modlist{i}.txt"
            if not wabbajack_file.extract_inline_file(wabbajack_path, entry.source_data_id, dest):
                continue
            found = True
            names |= {e.name for e in read_modlist(dest)}
    return names if found else None


_ARCHIVE_MAGICS = (b"BSA\x00", b"BTDX")


def _already_built(directive: Directive, staging: Path) -> bool:
    """True if a previous run already produced this mod file. Plain files
    must match the directive's size (an interrupted write is shorter); a
    rebuilt BSA/BA2 isn't byte-identical, so it only has to look like one
    (archives are written to a ``.part`` file and renamed, so a file under
    the final name was completed)."""
    dest = staging / directive.to
    try:
        if isinstance(directive, CreateBSADirective):
            if not dest.is_file():
                return False
            with open(dest, "rb") as fh:
                return fh.read(4) in _ARCHIVE_MAGICS
        if isinstance(directive, (FromArchiveDirective, PatchedFromArchiveDirective,
                                  InlineFileDirective)):
            return dest.is_file() and dest.stat().st_size == directive.size
    except OSError:
        pass
    return False


def prebuilt_mod_count(modlist: ModList, staging: Path) -> "tuple[int, int]":
    """``(already_present, total)`` mod folders this modlist would build.

    Cheap on purpose — a directory-existence test per mod, no hashing — so the
    UI can decide whether to offer a resume before an install starts. It
    answers "is there anything here worth reusing", not "is it all intact";
    :func:`drop_built_directives` still checks each file's size, and a resume
    rebuilds whatever doesn't match.
    """
    names: "set[str]" = set()
    for d in modlist.directives:
        parts = _norm_to(d)
        if len(parts) >= 3 and parts[0].lower() == "mods":
            names.add(parts[1])
    if not names or not staging.is_dir():
        return 0, len(names)
    return sum(1 for n in names if (staging / n).is_dir()), len(names)


def drop_built_directives(plan: InstallPlan, staging: Path) -> "list[Directive]":
    """Remove from ``plan`` the mod files (and rebuilt archives) that already
    exist in ``staging`` and return them. The loose files that only fed a
    rebuilt archive are dropped with it: they are never needed again."""
    built = [d for d in plan.mod_directives if _already_built(d, staging)]
    if not built:
        return []
    gone = {id(d) for d in built}
    plan.mod_directives = [d for d in plan.mod_directives if id(d) not in gone]
    done_temp = {d.temp_id.lower() for d in built if isinstance(d, CreateBSADirective)}
    pending_temp = {d.temp_id.lower() for d in plan.mod_directives + plan.stock_directives
                    if isinstance(d, CreateBSADirective)}
    plan.bsa_inputs = [d for d in plan.bsa_inputs
                       if d.to.split("/", 1)[0].lower() not in done_temp - pending_temp]
    return built


# ---------------------------------------------------------------------------
# Step 2: downloads
# ---------------------------------------------------------------------------


def _verified(path: "Path | None", archive: Archive) -> bool:
    # cached_hash_file: a download already checked on an earlier run isn't
    # read again unless it changed (size or modification time).
    return (path is not None and path.is_file()
            and hashes_match(archive.hash, cached_hash_file(path)))


def _download_name(archive: Archive) -> str:
    """The archive's file name with any directory part removed, so a
    crafted ``Name`` can't place the download outside the download folder."""
    name = Path((archive.name or "").replace("\\", "/")).name
    return name if name not in ("", ".", "..") else archive.hash.replace("/", "_")


def _cached(archive: Archive, download_dir: Path) -> "Path | None":
    # Deliberately not gated on ``archive.name``: a nameless archive is
    # downloaded under its hash by _download_name, so requiring a name here
    # meant those few never counted as cached and were re-fetched every run.
    # _download_name already sanitises the path, and _verified hashes the
    # file, so the name itself guards nothing.
    candidate = download_dir / _download_name(archive)
    if (candidate.is_file()
            and (not archive.size or candidate.stat().st_size == archive.size)
            and _verified(candidate, archive)):
        return candidate
    return None


def _adopt_into_cache(path: Path, archive: Archive, download_dir: Path, log) -> Path:
    """Move an already-verified manual download into the game's download
    cache and return its new path (or the original, if it can't be moved).

    A browser download lands in the user's Downloads folder, where nothing
    keeps it: the next run has to re-hash every candidate there to find it
    again (slow -- a modlist can need 77 of them), and emptying Downloads
    loses archives the modlist still needs. Moving it under the name
    :func:`_download_name` expects means :func:`_cached` finds it directly on
    the next run, with no hashing at all.

    The caller must have verified *path* against ``archive`` first: only a
    complete, hash-matching file is moved, so a half-finished download is
    never taken out of the user's Downloads folder.
    """
    try:
        if path.parent.resolve() == download_dir.resolve():
            return path                      # already cached
    except OSError:
        return path
    dest = download_dir / _download_name(archive)
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # Something already holds the name. If it is this archive the
            # cache is fine as it stands; if not, keep both rather than
            # overwriting a file another archive may be using.
            if _verified(dest, archive):
                log(f"Wabbajack: {dest.name} is already in the download cache")
                return dest
            dest = download_dir / f"{archive.hash.replace('/', '_')}_{dest.name}"
        shutil.move(str(path), str(dest))
    except OSError as exc:
        # Never fail an install over this: the verified original still works.
        log(f"Wabbajack: couldn't move {path.name} into the download cache ({exc}); "
            "using it where it is")
        return path
    if not dest.is_file() or (archive.size and dest.stat().st_size != archive.size):
        log(f"Wabbajack: {path.name} didn't survive the move into the download cache; "
            "using it where it is")
        return path if path.is_file() else dest
    log(f"Wabbajack: moved {dest.name} into the download cache")
    return dest


def _download_automatic(archive: Archive, download_dir: Path, *, nexus_downloader,
                        game_root, originals=None, cb: WabbajackInstallCallbacks,
                        cancel: threading.Event) -> "tuple[Path | None, str]":
    """``(verified path, "")`` or ``(None, why)``. Never asks the user
    anything except the LoversLab login (one retry after a successful one)."""
    def progress(cur, total):
        cb.on_download_progress(archive.hash, cur, total or archive.size)

    state = archive.state
    if isinstance(state, GameFileSourceState):
        # Used in place and already hash-checked; never unlinked below.
        result = fetch_game_file(state, game_root, archive.hash, originals)
        return (result.file_path, "") if result.success else (None, result.error)
    if isinstance(state, NexusState):
        if nexus_downloader is None:
            return None, "not logged in to Nexus Mods"
        def fetch():
            return download_nexus(state, nexus_downloader, download_dir,
                                  progress_cb=progress, cancel=cancel,
                                  log=cb.on_log)
    else:
        fn = resolve_downloader(state)
        if fn is None:
            return None, "no automatic download for this source"
        def fetch():
            return fn(state, download_dir / _download_name(archive), progress_cb=progress,
                      cancel=cancel)

    result = fetch()
    if (not result.success and result.needs_auth and not cancel.is_set()
            and cb.request_loverslab_login is not None and cb.request_loverslab_login()):
        result = fetch()
    if not result.success:
        return None, result.error
    if not _verified(result.file_path, archive):
        if result.file_path is not None:
            result.file_path.unlink(missing_ok=True)
        return None, "downloaded file doesn't match the modlist's hash"
    return result.file_path, ""


def _download_all(archives: "list[Archive]", download_dir: Path, *, nexus_downloader,
                  game_root, originals=None, max_workers: int, cb: WabbajackInstallCallbacks, cancel: threading.Event,
                  report: WabbajackInstallReport,
                  manual_folders: "list[Path] | None" = None,
                  pre_resolved: "dict[str, Path] | None" = None) -> "dict[str, Path]":
    have: "dict[str, Path]" = {}
    need_manual: "list[tuple[Archive, str]]" = []
    lock = threading.Lock()

    def work(archive: Archive) -> None:
        if cancel.is_set():
            return
        # Reported for cache hits too, so progress counts every archive.
        cb.on_download_start(archive.hash, archive.name, archive.size)
        path = _cached(archive, download_dir)
        why = ""
        if path is None:
            path, why = _download_automatic(
                archive, download_dir, nexus_downloader=nexus_downloader,
                game_root=game_root, originals=originals, cb=cb, cancel=cancel)
        cb.on_download_finish(archive.hash, path is not None)
        with lock:
            if path is not None:
                have[archive.hash] = path
            elif not cancel.is_set():
                need_manual.append((archive, why))

    run_smallest_first(order_by_size(archives, size_key=lambda a: a.size),
                       work, max_workers, stop=cancel)

    # A file from the user's own game install can't be downloaded: if it's
    # missing or a different version there's nothing to ask for (real case:
    # 35 prompts for 1.11.221 game files on another game version).
    game_files = [(a, why) for a, why in need_manual if isinstance(a.state, GameFileSourceState)]
    for archive, why in game_files:
        report.failed_archives.append((archive.name, why))
    if game_files:
        cb.on_log(f"Wabbajack: {len(game_files)} file(s) from your game folder don't match "
                  "the modlist; it needs a different game version")
    need_manual = [(a, why) for a, why in need_manual
                   if not isinstance(a.state, GameFileSourceState)]

    # Files the user already downloaded in their browser (a LoversLab or
    # MediaFire page, say) are picked up from their download folders first.
    if need_manual:
        from .manual_downloads import download_folders, find_downloaded
        folders = download_folders() if manual_folders is None else manual_folders
        known = pre_resolved or {}
        still: "list[tuple[Archive, str]]" = []
        for archive, why in need_manual:
            if cancel.is_set():
                still.append((archive, why))
                continue
            # The preflight queue already located and hashed this one, so take
            # its answer instead of re-hashing every candidate in the download
            # folders -- but re-verify, since the file may have been moved or
            # replaced between preflight and here.
            found = known.get(archive.hash)
            if found is not None:
                found = Path(found)
                found = found if _verified(found, archive) else None
            if found is None:
                found = find_downloaded(archive, folders)
            if found is not None:
                cb.on_log(f"Wabbajack: using {found} for {archive.name} (found in your downloads)")
                have[archive.hash] = _adopt_into_cache(found, archive, download_dir, cb.on_log)
                # work() already reported this one as finished-unsuccessfully,
                # since it has no automatic downloader. Report the real outcome
                # so its bytes reach the progress total; the overlay counts
                # archives per hash, so this doesn't double-count the archive.
                cb.on_download_start(archive.hash, archive.name, archive.size)
                cb.on_download_finish(archive.hash, True)
            else:
                still.append((archive, why))
        need_manual = still

    # Manual fallbacks run one at a time: each one blocks on the user.
    for archive, why in need_manual:
        if cancel.is_set():
            break
        path = None
        if cb.request_manual_download is not None:
            cb.on_log(f"Wabbajack: {archive.name} needs a manual download ({why})")
            path = cb.request_manual_download(archive, why)
        if path is not None and _verified(Path(path), archive):
            have[archive.hash] = _adopt_into_cache(
                Path(path), archive, download_dir, cb.on_log)
            # As above: re-report so a hand-downloaded archive's bytes count.
            cb.on_download_start(archive.hash, archive.name, archive.size)
            cb.on_download_finish(archive.hash, True)
        else:
            report.failed_archives.append(
                (archive.name, why if path is None else
                 "the chosen file doesn't match the modlist's hash"))
    return have


# ---------------------------------------------------------------------------
# Steps 4-5: profile + provenance
# ---------------------------------------------------------------------------


def _install_workers_setting() -> str:
    """Settings > Extraction > Install workers: "auto", "light" or a count."""
    try:
        from Utils.ui_config import load_extraction_settings
        return str(load_extraction_settings().get("install_workers", "auto"))
    except Exception:
        return "auto"


def _run_batches(batches, work, controller, index, cancel, cb, log) -> None:
    """Run ``work(root, batch)`` for every batch, at most ``controller.limit``
    at a time, re-reading the limit as the build goes. A worker that raises
    stops new batches and re-raises once the running ones have finished."""
    from collections import deque
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    pending = deque(batches)
    running: set = set()
    shown = None
    with ThreadPoolExecutor(max_workers=controller.max_limit,
                            thread_name_prefix="wabbajack-build") as pool:
        try:
            while pending or running:
                if (controller.limit, controller.reason) != shown:
                    shown = (controller.limit, controller.reason)
                    log(f"Wabbajack: building {controller.limit} archive(s) at once "
                        f"({controller.reason})")
                    cb.on_workers(controller.limit, controller.reason)
                index.concurrency = max(1, controller.limit)
                while pending and len(running) < controller.limit and not cancel.is_set():
                    root, batch = pending.popleft()
                    running.add(pool.submit(work, root, batch))
                if not running:
                    break
                finished, _ = wait(running, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in finished:
                    running.discard(f)
                    f.result()
                controller.tick()
        except BaseException:
            pending.clear()
            raise


def _root_archive(directive) -> str:
    """The downloaded archive a directive reads from, or "" for one that
    reads none (inline files, archives being packed)."""
    chain = getattr(directive, "archive_hash_path", None)
    return chain[0] if chain else ""


def _write_profile(profile_dir: Path, scratch: Path, built_mods: "set[str]",
                   report: WabbajackInstallReport, log,
                   root_parents: "dict[str, str] | None" = None,
                   staging: "Path | None" = None,
                   ini_targets: "list[str] | None" = None,
                   ini_subdir: "str | None" = None,
                   enabled_extra: "set[str] | None" = None) -> None:
    curated = scratch / "modlist.txt"
    entries: "list[ModEntry]" = []
    if curated.is_file():
        # MO2 files a separator *after* the mods it heads; Mosaic puts it
        # before them. Copying the curated list verbatim shifts every group
        # one heading down, so re-anchor before anything else touches it.
        entries = [e for e in mo2_to_natural_order(read_modlist(curated))
                   if e.is_separator or e.name in built_mods]
    # A mod split by split_root_builder_mods isn't in the curated modlist, so
    # it goes straight below its parent and inherits the parent's state --
    # the two halves are one mod and must be enabled or disabled together.
    children: "dict[str, list[str]]" = {}
    for child, parent in (root_parents or {}).items():
        if child in built_mods:
            children.setdefault(parent, []).append(child)
    if children:
        placed: "list[ModEntry]" = []
        for e in entries:
            placed.append(e)
            for child in sorted(children.pop(e.name, ())):
                placed.append(ModEntry(name=child, enabled=e.enabled, locked=e.locked))
        entries = placed
    listed = {e.name for e in entries}
    # Without a curated modlist every built mod is part of the setup; with
    # one, a built mod it doesn't list (not expected after
    # limit_to_profile_mods) is added disabled, as MO2 would show it.
    entries += [ModEntry(name=n, enabled=(not curated.is_file()
                                          or n in (enabled_extra or ())), locked=False)
                for n in sorted(built_mods - listed)]
    write_modlist(profile_dir / "modlist.txt", entries)

    # MO2 keeps a separator's colour in mods/<Name>_separator/meta.ini, which
    # reaches staging like any other mod directive (3 path parts). Mosaic keeps
    # colour in profile_state instead, so without this the imported list is
    # uncoloured and the curator's grouping is invisible.
    if staging is not None:
        try:
            colors = scan_separator_colors(staging)
        except Exception as exc:                       # never fail an install over cosmetics
            log(f"Wabbajack: could not read separator colours: {exc}")
            colors = {}
        if colors:
            merged = dict(read_separator_colors(profile_dir))
            merged.update(colors)
            write_separator_colors(profile_dir, merged)
            log(f"Wabbajack: imported {len(colors)} separator colour(s)")
        # Mosaic has no nesting, so this is stored and not shown: it exists so
        # that writing the profile back out doesn't flatten the curator's
        # grouping. Same reasoning as the colours -- never fail over it.
        try:
            nesting = scan_separator_nesting(staging)
        except Exception as exc:
            log(f"Wabbajack: could not read separator nesting: {exc}")
            nesting = {}
        if nesting:
            write_separator_nesting(profile_dir, nesting)
            log(f"Wabbajack: kept nesting for {len(nesting)} separator(s)")

    for name in _LOAD_ORDER_FILES:
        src = scratch / name
        if src.is_file():
            shutil.copyfile(src, profile_dir / name)
            invalidate_plugins_cache(profile_dir / name)

    if ini_targets and ini_subdir:
        applied = _apply_profile_inis(scratch, profile_dir / ini_subdir, ini_targets, log)
        if applied:
            from Utils.profile.profile_state import merge_profile_settings
            merge_profile_settings(profile_dir, {"profile_ini_files": True})
            for name in applied:
                if name not in report.applied_profile_inis:
                    report.applied_profile_inis.append(name)

    held = [p for p in scratch.rglob("*") if p.is_file()
            and p.relative_to(scratch).as_posix().lower() not in ("modlist.txt", *_LOAD_ORDER_FILES)]
    if held:
        dest = profile_dir / PROFILE_FILES_DIR
        for p in held:
            rel = p.relative_to(scratch)
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest / rel))
            report.held_profile_files.append(rel.as_posix())
        log(f"Wabbajack: kept {len(held)} profile file(s) in {dest} (not applied)")


def _profile_ini_targets(game) -> "tuple[list[str] | None, str | None]":
    """The game's own INI names and the profile subfolder they go in, for a
    game that keeps per-profile INIs; (None, None) otherwise."""
    subdir = getattr(game, "_PROFILE_INI_SUBDIR", None)
    if not subdir or not hasattr(game, "set_profile_ini_files"):
        return None, None
    from Utils.collections.collection_ini_tweaks import GAME_INI_TARGETS
    targets = GAME_INI_TARGETS.get(getattr(game, "name", "") or "")
    return (list(targets), subdir) if targets else (None, None)


def _apply_profile_inis(scratch: Path, ini_dir: Path, targets: "list[str]",
                        log) -> "list[str]":
    """Move the game's own INIs from an MO2 profile into the profile's
    ``ini files`` folder, named as the game names them (MO2 lower-cases them;
    the deploy step links them into My Games by name). ``__MO_Saves`` is set
    back to the game's ``Saves\\``. Returns the names applied."""
    from Games.Bethesda.bethesda_ini import mark_installed_inis

    wanted = {t.lower(): t for t in targets}
    applied: "list[str]" = []
    for src in sorted(scratch.iterdir()):
        name = wanted.get(src.name.lower())
        if name is None or not src.is_file():
            continue
        # latin-1 maps every byte, so the file comes back byte for byte
        text = src.read_bytes().decode("latin-1")
        text, fixed = _MO2_SAVE_PATH.subn(lambda m: f"{m.group(1)}Saves\\{m.group(2)}", text)
        ini_dir.mkdir(parents=True, exist_ok=True)
        (ini_dir / name).write_bytes(text.encode("latin-1"))
        src.unlink()
        applied.append(name)
        if fixed:
            log(f"Wabbajack: {name}: save folder set back from MO2's __MO_Saves to Saves")
    if applied:
        mark_installed_inis(ini_dir, applied)
        log(f"Wabbajack: applied the modlist's {', '.join(applied)} as the profile's own INIs")
    return applied


def _stamp_mod_meta(mod_dir: Path, modlist: ModList, *, root_folder: bool = False) -> None:
    """Record the modlist in ``meta.ini``; ``root_folder`` also sets MO2's
    ``[General] rootFolder=true`` (read by ``Nexus.nexus_meta``), which
    deploys the mod from the game root."""
    meta = mod_dir / "meta.ini"
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # MO2 keys are case-sensitive
    if meta.is_file():
        try:
            cp.read(meta, encoding="utf-8")
        except configparser.Error:
            return  # don't clobber a meta.ini we can't parse
    if not cp.has_section("Wabbajack"):
        cp.add_section("Wabbajack")
    cp["Wabbajack"]["modlist"] = modlist.name
    cp["Wabbajack"]["modlistVersion"] = modlist.version
    if root_folder:
        if not cp.has_section("General"):
            cp.add_section("General")
        cp["General"]["rootFolder"] = "true"
    with open(meta, "w", encoding="utf-8") as fh:
        cp.write(fh)


def _rebuild_index(game, profile_dirs: "Path | list[Path]", log) -> None:
    """Same index rebuild the Collection installer runs after installing
    (collection_install.py, "Updating mod index").

    With several profiles sharing one staging folder the index is identical
    for all of them -- it is a function of staging, the strip prefixes and
    the root-flagged set -- so it is built once and copied. ``copy2`` keeps
    the mtime the index's own validity check is keyed on."""
    dirs = [Path(profile_dirs)] if isinstance(profile_dirs, (str, Path)) else list(profile_dirs)
    if not dirs:
        return
    profile_dir = dirs[0]
    try:
        from Nexus.nexus_meta import collect_root_flagged_mods
        from Utils.deploy.deploy import load_per_mod_strip_prefixes
        from Utils.filemap import rebuild_mod_index
        staging = game.get_effective_mod_staging_path()
        try:
            index_dir = game.get_effective_filemap_path().parent
        except Exception:
            index_dir = profile_dir
        rf_mods: "set[str]" = set()
        for d in dirs:
            try:
                rf_mods |= set(collect_root_flagged_mods(
                    d / "modlist.txt", staging, log_fn=log) or ())
            except Exception:
                pass
        rebuild_mod_index(
            index_dir / "modindex.bin", staging,
            strip_prefixes=set(getattr(game, "mod_folder_strip_prefixes", None) or ()) or None,
            per_mod_strip_prefixes=load_per_mod_strip_prefixes(profile_dir),
            allowed_extensions=set(getattr(game, "mod_install_extensions", None) or ()) or None,
            root_folder_mods=set(rf_mods or ()) or None,
            normalize_folder_case=getattr(game, "normalize_folder_case", True))
        built = index_dir / "modindex.bin"
        for d in dirs[1:]:
            if d != index_dir and built.is_file():
                shutil.copy2(built, d / "modindex.bin")
    except Exception as exc:
        log(f"Wabbajack: mod index rebuild skipped: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _install_stock_game(work: Path, dest: Path, modlist: ModList, game,
                        report: WabbajackInstallReport, log) -> None:
    """Move the finished game folder into place as a game copy."""
    from Utils import game_copy as gc

    from .wabbajack_stock_game import copy_label
    info = gc.CopyInfo(
        game=getattr(game, "name", ""), build=copy_label(modlist),
        version=gc._exe_version(work, gc.version_exe(game)),
        source=f"Wabbajack modlist {modlist.name} {modlist.version}".strip(),
        method="wabbajack",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        gc.install_built_copy(work, dest, info)
    except (gc.GameCopyError, OSError) as exc:
        report.failed_directives.append((str(dest), str(exc)))
        log(f"Wabbajack: couldn't install the modlist's game copy: {exc}")
        return
    report.stock_game = str(dest)
    log(f"Wabbajack: the modlist's game copy is ready at {dest}"
        + (f" (game version {info.version})" if info.version else ""))


def run_wabbajack_install(*, wabbajack_path: "str | Path", modlist: ModList, game,
                          profile_dir: "Path | None" = None,
                          download_dir: Path, nexus_downloader=None,
                          profile_name: "str | None" = None,
                          profile_dirs: "dict[str, Path] | None" = None,
                          substitutions: "dict[str, str] | None" = None,
                          max_downloads: int = 3,
                          callbacks: "WabbajackInstallCallbacks | None" = None,
                          control: "WabbajackInstallControl | None" = None,
                          manual_folders: "list[Path] | None" = None,
                          manual_resolved: "dict[str, Path] | None" = None,
                          resume: bool = False,
                          stock_game_dest: "Path | None" = None,
                          ) -> WabbajackInstallReport:
    """Install ``modlist`` into one or more Mosaic profiles (see the module doc).

    Either pass ``profile_dir`` with ``profile_name`` -- the MO2 profile to
    install, required when :func:`profile_choices` offers more than one -- or
    ``profile_dirs``, an ordered ``{MO2 profile: Mosaic profile dir}`` map that
    installs several of the modlist's profiles in one run. The mods are
    downloaded and built once into the shared staging folder and each profile
    gets its own ``modlist.txt``; the caller is what makes them share, by
    pointing every profile's ``mods`` at one folder (see
    ``profile_state.link_shared_mods``).

    ``substitutions`` comes from ``wabbajack_directives.path_substitutions``.
    ``manual_resolved`` is ``{archive hash: path}`` for manual archives the
    preflight queue already found, so the install doesn't re-hash the download
    folders looking for them again. Each one is re-verified before use.

    ``resume`` installs into a profile that already holds an earlier run:
    files that are already built are kept, and only what's missing is
    downloaded and built.

    ``stock_game_dest``: build the list's own game folder (its ``Stock Game``)
    there as a game copy and make every installed profile use it (see
    ``wabbajack_stock_game``). ``None`` skips that folder, as before."""
    cb = callbacks or WabbajackInstallCallbacks()
    cancel = (control or WabbajackInstallControl()).cancel
    log = cb.on_log
    report = WabbajackInstallReport()
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    if not profile_dirs and profile_dir is None:
        raise ValueError("pass profile_dir or profile_dirs")

    cb.on_status("Reading modlist…")
    pairs = (list(profile_dirs.items()) if profile_dirs
             else [(profile_name or "", Path(profile_dir))])
    # A single unnamed target means "whatever profile this list has" -- a
    # modlist with no profiles folder, or one the caller didn't name.
    unnamed = len(pairs) == 1 and not pairs[0][0]
    from .wabbajack_stock_game import copy_label, game_stock_dir, partial_stock_dirs
    stock_dir = game_stock_dir(modlist, game)
    plan = classify_directives(modlist, None if unnamed else [k for k, _ in pairs],
                               stock_dir=stock_dir if stock_game_dest is not None else None,
                               root_dirs=partial_stock_dirs(modlist, game))
    # MO2 profile -> the Mosaic profile that gets its modlist.txt.
    targets: "dict[str, Path]" = ({plan.profile_name: Path(pairs[0][1])} if unnamed
                                  else {k: Path(v) for k, v in pairs})
    profile_dir = next(iter(targets.values()))

    game.set_active_profile_dir(profile_dir)
    game.load_paths()
    staging = Path(game.get_effective_mod_staging_path())
    # Game-file archives are read from the Steam install, never from a game copy
    # (a "Stock Game" folder the list may itself be building).
    game_root = (game.get_steam_install_path() if hasattr(game, "get_steam_install_path")
                 else game.get_game_path() if hasattr(game, "get_game_path") else None)

    report.unplaced_files = list(plan.unplaced)
    report.profiles_used = list(plan.profile_names)
    if plan.unplaced:
        groups = Counter(u.split("/", 1)[0] if "/" in u else "(top level)"
                         for u in plan.unplaced)
        log(f"Wabbajack: {len(plan.unplaced)} file(s) outside mods/ and the chosen "
            "profile are not installed: "
            + ", ".join(f"{name} ({n})" for name, n in groups.most_common()))
        if stock_dir and groups.get(stock_dir):
            log(f"Wabbajack: {stock_dir}/ is the modlist's own copy of the game "
                "files; Mosaic deploys over your real install instead, so those are "
                "skipped on purpose")
    profile_mods = _profile_mod_names(plan, wabbajack_path)
    if profile_mods is not None:
        report.skipped_mods = limit_to_profile_mods(plan, profile_mods)
        if report.skipped_mods:
            log(f"Wabbajack: skipping {len(report.skipped_mods)} mod(s) the "
                f"{plan.profile_name!r} profile doesn't use: {', '.join(report.skipped_mods)}")
    layout = layout_root_builder_mods(plan, modlist, game)
    root_parents = layout.root_parents
    if layout.kind == "split":
        report.root_builder_mods = layout.converted
        for name in layout.converted:
            log(f"Wabbajack: {root_parents[name]} uses Root Builder; its game-root files "
                f"are installed as {name!r}, a root-folder mod")
        if layout.folded:
            log(f"Wabbajack: {layout.folded} Root/ file(s) the mod also ships itself were "
                "dropped; the mod's own copy is the one the game loads")
    elif layout.kind == "no_data":
        for name in layout.converted:
            log(f"Wabbajack: {name} uses Root Builder; moved its Root/ files to the mod's top level")
    else:
        report.root_builder_mods = layout.converted
        for name in layout.converted:
            log(f"Wabbajack: {name} uses Root Builder; installing it as a root-folder mod "
                "(Root/ files go to the game folder, the rest to Data/)")
    for name in plan.extra_root_mods:
        if name not in report.root_builder_mods:
            report.root_builder_mods = [*report.root_builder_mods, name]
        log(f"Wabbajack: the list's files for the game folder are installed as "
            f"{name!r}, a root-folder mod")
    dxvk = [u for u in plan.unplaced
            if "/" in u and u.rsplit("/", 1)[1].lower() in _PROTON_SUPPLIED]
    if dxvk:
        log(f"Wabbajack: not installing {', '.join(dxvk)} -- Proton brings its own DXVK")

    stock_reused = False
    if plan.stock_directives:
        from Utils import game_copy as gc
        existing = gc.read_marker(stock_game_dest)
        if existing is not None and existing.build == copy_label(modlist):
            # Built by an earlier run of this list version: keep it.
            stock_reused = True
            plan.stock_directives = []
            log(f"Wabbajack: the modlist's game copy is already built at {stock_game_dest}")
        else:
            log(f"Wabbajack: building the modlist's own game folder ({stock_dir}/, "
                f"{len(plan.stock_directives)} file(s)) as a game copy at {stock_game_dest}; "
                "your Steam install is not changed")

    prebuilt = drop_built_directives(plan, staging) if resume else []
    if prebuilt:
        log(f"Wabbajack: {len(prebuilt)} file(s) are already built; keeping them")
    needed = plan.archive_hashes()
    archives = [a for a in modlist.archives if a.hash in needed]
    cb.on_archives_needed(len(archives), sum(a.size for a in archives))
    cb.on_status(f"Downloading {len(archives)} archive(s)…")
    have = _download_all(archives, download_dir, nexus_downloader=nexus_downloader,
                         game_root=game_root,
                         originals=runtime_swap_originals(getattr(game, "name", "")),
                         max_workers=max_downloads, cb=cb, cancel=cancel, report=report,
                         manual_folders=manual_folders, pre_resolved=manual_resolved)
    flush_hash_cache()
    if cancel.is_set():
        report.cancelled = True
        return report

    work_root = profile_dir / ".wabbajack_work"
    profile_scratch = work_root / "profile"
    index = ArchiveIndex(work_root / "vfs")
    for h, path in have.items():
        index.add_archive(h, path)

    bsa_root = work_root / "bsa"
    stock_work = None
    if plan.stock_directives:
        from Utils import game_copy as gc
        stock_work = gc.partial_dir(stock_game_dest)
        shutil.rmtree(stock_work, ignore_errors=True)      # a stale, interrupted attempt
        stock_work.mkdir(parents=True)
    # Archives are packed last, once every file that goes into them exists.
    archives = [d for d in plan.mod_directives if isinstance(d, CreateBSADirective)]
    stock_archives = [d for d in plan.stock_directives if isinstance(d, CreateBSADirective)]
    files = ([(d, staging) for d in plan.mod_directives if not isinstance(d, CreateBSADirective)]
             + [(d, stock_work) for d in plan.stock_directives
                if not isinstance(d, CreateBSADirective)]
             + [(d, profile_scratch) for d in plan.profile_directives]
             + [(d, bsa_root) for d in plan.bsa_inputs])
    # Downloaded archives are built in parallel, each by one worker: every file
    # from one archive is built together, its 7z/rar extracts only the members
    # the install uses, and its scratch files are deleted once its last file is
    # in place. How many run at once is up to the Install workers setting
    # (wabbajack_workers). Directives that read no archive go in batches, first;
    # archives are packed last, once every file that goes into them exists.
    index.plan(getattr(d, "archive_hash_path", None) for d, _ in files)
    by_root: "dict[str, list]" = {}
    loose: list = []
    for job in files:
        root = _root_archive(job[0])
        (by_root.setdefault(root, []) if root else loose).append(job)
    # Biggest archives first, so no long one is left running alone at the end.
    batches = ([("", loose[i:i + 500]) for i in range(0, len(loose), 500)]
               + sorted(by_root.items(),
                        key=lambda kv: -sum(getattr(d, "size", 0) or 0 for d, _ in kv[1])))
    packing = ([(d, staging) for d in archives]
               + [(d, stock_work) for d in stock_archives])
    total = len(files) + len(packing)
    from .wabbajack_workers import WorkerController
    controller = WorkerController(_install_workers_setting(), disk_paths=(staging, work_root))
    index.low_priority = controller.low_priority
    stock_failed = 0
    built_mods: "set[str]" = {n for n in (d.to.split("/", 1)[0] for d in prebuilt)
                              if not _is_separator_folder(n)}
    state_lock = threading.Lock()
    finished = [0]
    cb.on_status("Building files…")

    def build_one(directive, dest_root) -> None:
        nonlocal stock_failed
        # Wabbajack rewrites a profile's modlist.txt when it compiles the
        # list but keeps the original file's Size/Hash (real case: both
        # Horizon 1.4.4 profiles, while all 51 other inline files match),
        # so that one file can't be hash-checked.
        stale_hash = (dest_root is profile_scratch
                      and Path(directive.to).name.lower() == "modlist.txt")
        result = apply_directive(directive, dest_root=dest_root,
                                 wabbajack_path=wabbajack_path, archive_index=index,
                                 substitutions=substitutions, bsa_temp_root=bsa_root,
                                 verify_hash=not stale_hash)
        with state_lock:
            if result.note:
                log(f"Wabbajack: {result.note}")
            if result.success:
                if dest_root is staging:
                    name = directive.to.split("/", 1)[0]
                    # A separator folder holds nothing but the meta.ini that
                    # carries its colour. It is a heading, not a mod: keeping
                    # it out of built_mods keeps it out of installed_mods and
                    # stops _stamp_mod_meta rewriting MO2's own file.
                    if not _is_separator_folder(name):
                        built_mods.add(name)
            else:
                to = (f"{stock_dir}/{directive.to}" if dest_root is stock_work
                      else directive.to)
                report.failed_directives.append((to, result.error))
                if dest_root is stock_work:
                    stock_failed += 1
            finished[0] += 1
            n = finished[0]
        if result.success:
            controller.record(getattr(directive, "size", 0) or 0)
        cb.on_build_progress(n, total)

    def build_batch(root: str, batch: list) -> None:
        for directive, dest_root in batch:
            if cancel.is_set():
                return
            build_one(directive, dest_root)
        if root:
            index.release(root)

    try:
        _run_batches(batches, build_batch, controller, index, cancel, cb, log)
        for directive, dest_root in packing:
            if cancel.is_set():
                break
            build_one(directive, dest_root)
        if cancel.is_set():
            report.cancelled = True
            return report

        if stock_work is not None:
            if stock_failed:
                log(f"Wabbajack: {stock_failed} file(s) of the modlist's game folder failed, "
                    "so its game copy wasn't installed")
            else:
                _install_stock_game(stock_work, Path(stock_game_dest), modlist, game,
                                    report, log)
        elif stock_reused:
            report.stock_game = str(stock_game_dest)
        if report.stock_game:
            from Utils.profile.profile_state import merge_profile_settings
            for dest in targets.values():
                merge_profile_settings(dest, {"game_path": report.stock_game})
            log(f"Wabbajack: {len(targets)} profile(s) will run on the game copy at "
                f"{report.stock_game}")

        cb.on_status("Writing profile…" if len(targets) == 1
                     else f"Writing {len(targets)} profiles…")
        ini_targets, ini_subdir = _profile_ini_targets(game)
        for mo2_profile, dest in targets.items():
            _write_profile(dest, profile_scratch / mo2_profile, built_mods, report, log,
                           root_parents=root_parents, staging=staging,
                           ini_targets=ini_targets, ini_subdir=ini_subdir,
                           enabled_extra=set(plan.extra_root_mods))
    finally:
        index.close()
        wabbajack_file.close_open_containers()
        shutil.rmtree(work_root, ignore_errors=True)
        if stock_work is not None:
            shutil.rmtree(stock_work, ignore_errors=True)   # gone if it was installed

    report.installed_mods = sorted(built_mods)
    for name in report.installed_mods:
        try:
            _stamp_mod_meta(staging / name, modlist,
                            root_folder=name in report.root_builder_mods)
        except OSError as exc:
            log(f"Wabbajack: couldn't stamp {name}/meta.ini: {exc}")
    for mo2_profile, dest in targets.items():
        # mo2_profile is what makes Repair work when one modlist produced
        # several Mosaic profiles: without it they're indistinguishable.
        write_wabbajack_modlist_info(dest, {
            "name": modlist.name, "author": modlist.author, "version": modlist.version,
            "game_type": modlist.game_type, "source_file": str(wabbajack_path),
            "mo2_profile": mo2_profile,
        })
    _rebuild_index(game, list(targets.values()), log)
    cb.on_status("Done")
    return report

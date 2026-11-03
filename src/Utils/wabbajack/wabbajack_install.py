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
   order is hand-tuned and reproducing it exactly is the point. Other
   profile files (INIs) are kept in ``<profile>/wabbajack_profile_files`` and
   listed in the report rather than applied.
5. **Provenance**: the profile records which modlist it came from, each
   built mod's ``meta.ini`` gets a ``[Wabbajack]`` section, and the mod index
   is rebuilt so the app sees the new mods without a manual refresh.
"""
from __future__ import annotations

import configparser
import re
import shutil
import threading
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Sequence

from Utils.downloads.download_scheduler import order_by_size, run_smallest_first
from Utils.mods.mo2_separator_meta import scan_separator_colors
from Utils.mods.modlist import ModEntry, read_modlist, write_modlist
from Utils.plugins.plugins import invalidate_plugins_cache
from Utils.profile.profile_state import (read_separator_colors,
                                         write_separator_colors,
                                         write_wabbajack_modlist_info)

from .downloaders import resolve_downloader
from .downloaders.game_file_source import fetch_game_file, runtime_swap_originals
from .downloaders.nexus_source import download_nexus
from .wabbajack_directives import apply_directive
from . import wabbajack_file
from .wabbajack_hash import hash_file, hashes_match
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
# Games whose mods aren't laid out under Data/, so the Root Builder -> Data/
# relocation (Bethesda layout) must not run.
_NO_DATA_FOLDER_GAMES = frozenset({"Cyberpunk 2077",
                                   "Vampire: The Masquerade - Bloodlines"})
# Games where a Root Builder mod's Root/ folder holds two different
# destinations, so the mod has to be split (see split_root_builder_mods).
_SPLIT_ROOT_BUILDER_GAMES = frozenset({"Vampire: The Masquerade - Bloodlines"})
# Suffix for the sibling mod that carries a split mod's game-root files.
ROOT_MOD_SUFFIX = " - Root"
# Wabbajack keeps a private copy of the game under this folder so its own
# installs never touch the real one. Mosaic deploys over the real install, so
# the copy is skipped -- on the first VtMB list all 4620 of its files that came
# from a downloaded archive hashed identical to a clean install.
_STOCK_GAME_DIR = "StockGame"


def _noop(*_a, **_k):
    return None


@dataclass
class WabbajackInstallCallbacks:
    on_status: Callable[[str], None] = _noop
    on_log: Callable[[str], None] = _noop
    on_download_start: Callable[[str, str, int], None] = _noop    # hash, name, size
    on_download_progress: Callable[[str, int, int], None] = _noop  # hash, cur, total
    on_download_finish: Callable[[str, bool], None] = _noop        # hash, ok
    on_build_progress: Callable[[int, int], None] = _noop          # done, total
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
    # Mods only other profiles use: not downloaded or built.
    skipped_mods: "list[str]" = field(default_factory=list)
    # MO2 Root Builder mods converted to root-folder mods.
    root_builder_mods: "list[str]" = field(default_factory=list)
    # The modlist's MO2 profiles this run installed, one Mosaic profile each.
    profiles_used: "list[str]" = field(default_factory=list)
    cancelled: bool = False

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
    profile_names: "list[str]" = field(default_factory=list)
    unplaced: "list[str]" = field(default_factory=list)

    @property
    def profile_name(self) -> str:
        return self.profile_names[0] if self.profile_names else ""

    def archive_hashes(self) -> "set[str]":
        return {d.archive_hash_path[0]
                for d in self.mod_directives + self.profile_directives + self.bsa_inputs
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
                        profiles: "str | Sequence[str] | None" = None) -> InstallPlan:
    """Split the modlist's directives by where they land (see module doc).
    Wabbajack writes ``To`` with Windows separators; they're normalised.
    ``profiles`` names the MO2 profile(s) to install -- one name, or several
    when the modlist ships variants the user wants side by side (the VtMB
    list "Fashionista - Steam" has 17 that share one pool of 405 mods). It
    may be omitted only when :func:`profile_choices` offers at most one,
    otherwise :class:`ProfileChoiceRequired` is raised. A kept profile
    directive's ``to`` starts with its profile name, so one run can build
    several profiles' files into separate scratch folders."""
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
        else:
            plan.unplaced.append(to)
    return plan


def limit_to_profile_mods(plan: InstallPlan, listed: "set[str]") -> "list[str]":
    """Keep only the mods the chosen profile's ``modlist.txt`` lists
    (enabled or disabled), dropping every other ``mods/<Name>`` directive and
    the ``TEMP_BSA_FILES`` inputs that only their archives used -- so their
    archives aren't downloaded either. A list with GOG and Steam profiles
    ships both stores' variants of some mods (Horizon: two Unofficial
    Fallout 4 Patch builds providing the same plugin); only the chosen
    profile's belong in the install. Names match case-insensitively.
    Returns the skipped mod names, sorted, not counting MO2 separator
    folders (``*_separator``), which are never built either way."""
    wanted = {n.lower() for n in listed}
    kept, skipped = [], set()
    for d in plan.mod_directives:
        mod = d.to.split("/", 1)[0]
        if mod.lower() in wanted:
            kept.append(d)
        elif not mod.lower().endswith("_separator"):
            # Separator folders aren't mods (the profile's modlist.txt
            # still carries the separator lines), so they're dropped
            # without being reported as skipped.
            skipped.add(mod)
    plan.mod_directives = kept
    temp_ids = {d.temp_id.lower() for d in kept if isinstance(d, CreateBSADirective)}
    plan.bsa_inputs = [d for d in plan.bsa_inputs
                       if d.to.split("/", 1)[0].lower() in temp_ids]
    return sorted(skipped)


def apply_root_builder_layout(plan: InstallPlan, *, data_folder: bool = True) -> "list[str]":
    """Convert MO2 Root Builder mods into Mosaic root-folder mods.

    Root Builder puts files that belong in the game's install folder under
    ``<Mod>/Root/`` (real case, Horizon 1.4.4: F4SE's ``f4se_loader.exe``
    and DLLs, a launcher ``.bat``, a downgrader). Mosaic's equivalent is a
    whole-mod ``rootFolder=true`` flag (deployed verbatim from the game root),
    so in such a mod ``Root/x`` becomes ``x`` and every other file moves
    under ``Data/`` (``meta.ini`` stays put; a path already under ``Data/``
    is kept). Returns the converted mod names, sorted; the caller flags
    them in ``meta.ini``. With ``data_folder=False`` (games whose mods have
    no ``Data/``, like Cyberpunk 2077, where every file already deploys to the
    game folder) only ``Root/x`` -> ``x`` happens and other files stay put."""
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
        if rest.lower() == "meta.ini" or head.lower() == "data" or not data_folder:
            return to
        return f"{mod}/Data/{rest}"

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


def _profile_mod_names(plan: InstallPlan, wabbajack_path) -> "set[str] | None":
    """The union of the chosen profiles' ``modlist.txt`` mod names, read
    straight from the ``.wabbajack`` before anything is downloaded. ``None``
    when none of them ships an inline ``modlist.txt`` (nothing to filter by);
    a profile that is missing one simply contributes nothing."""
    import tempfile
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
            names |= {e.name for e in read_modlist(dest) if not e.is_separator}
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
    pending_temp = {d.temp_id.lower() for d in plan.mod_directives
                    if isinstance(d, CreateBSADirective)}
    plan.bsa_inputs = [d for d in plan.bsa_inputs
                       if d.to.split("/", 1)[0].lower() not in done_temp - pending_temp]
    return built


# ---------------------------------------------------------------------------
# Step 2: downloads
# ---------------------------------------------------------------------------


def _verified(path: "Path | None", archive: Archive) -> bool:
    return path is not None and path.is_file() and hashes_match(archive.hash, hash_file(path))


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
                                  progress_cb=progress, cancel=cancel)
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
                  manual_folders: "list[Path] | None" = None) -> "dict[str, Path]":
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
        still: "list[tuple[Archive, str]]" = []
        for archive, why in need_manual:
            found = None if cancel.is_set() else find_downloaded(archive, folders)
            if found is not None:
                cb.on_log(f"Wabbajack: using {found} for {archive.name} (found in your downloads)")
                have[archive.hash] = found
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
            have[archive.hash] = Path(path)
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


def _write_profile(profile_dir: Path, scratch: Path, built_mods: "set[str]",
                   report: WabbajackInstallReport, log,
                   root_parents: "dict[str, str] | None" = None,
                   staging: "Path | None" = None) -> None:
    curated = scratch / "modlist.txt"
    entries: "list[ModEntry]" = []
    if curated.is_file():
        entries = [e for e in read_modlist(curated)
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
    entries += [ModEntry(name=n, enabled=not curated.is_file(), locked=False)
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

    for name in _LOAD_ORDER_FILES:
        src = scratch / name
        if src.is_file():
            shutil.copyfile(src, profile_dir / name)
            invalidate_plugins_cache(profile_dir / name)

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
                          resume: bool = False,
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
    ``resume`` installs into a profile that already holds an earlier run:
    files that are already built are kept, and only what's missing is
    downloaded and built."""
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
    plan = classify_directives(modlist, None if unnamed else [k for k, _ in pairs])
    # MO2 profile -> the Mosaic profile that gets its modlist.txt.
    targets: "dict[str, Path]" = ({plan.profile_name: Path(pairs[0][1])} if unnamed
                                  else {k: Path(v) for k, v in pairs})
    profile_dir = next(iter(targets.values()))

    game.set_active_profile_dir(profile_dir)
    game.load_paths()
    staging = Path(game.get_effective_mod_staging_path())
    game_root = game.get_game_path() if hasattr(game, "get_game_path") else None

    report.unplaced_files = list(plan.unplaced)
    report.profiles_used = list(plan.profile_names)
    if plan.unplaced:
        groups = Counter(u.split("/", 1)[0] if "/" in u else "(top level)"
                         for u in plan.unplaced)
        log(f"Wabbajack: {len(plan.unplaced)} file(s) outside mods/ and the chosen "
            "profile are not installed: "
            + ", ".join(f"{name} ({n})" for name, n in groups.most_common()))
        if groups.get(_STOCK_GAME_DIR):
            log(f"Wabbajack: {_STOCK_GAME_DIR}/ is the modlist's own copy of the game "
                "files; Mosaic deploys over your real install instead, so those are "
                "skipped on purpose")
    profile_mods = _profile_mod_names(plan, wabbajack_path)
    if profile_mods is not None:
        report.skipped_mods = limit_to_profile_mods(plan, profile_mods)
        if report.skipped_mods:
            log(f"Wabbajack: skipping {len(report.skipped_mods)} mod(s) the "
                f"{plan.profile_name!r} profile doesn't use: {', '.join(report.skipped_mods)}")
    target_game = mosaic_game_for(modlist.game_type)
    root_parents: "dict[str, str]" = {}
    if target_game in _SPLIT_ROOT_BUILDER_GAMES:
        # Root/ holds two destinations here, so the mods are split in two.
        overlay_dirs = set(getattr(game, "mod_folder_strip_prefixes", None) or ())
        roots, parents, folded = split_root_builder_mods(plan, overlay_dirs=overlay_dirs)
        report.root_builder_mods = roots
        root_parents = dict(zip(roots, parents))
        for name in roots:
            log(f"Wabbajack: {root_parents[name]} uses Root Builder; its game-root files "
                f"are installed as {name!r}, a root-folder mod")
        if folded:
            log(f"Wabbajack: {folded} Root/ file(s) the mod also ships itself were "
                "dropped; the mod's own copy is the one the game loads")
    elif target_game in _NO_DATA_FOLDER_GAMES:
        # Everything deploys to the game folder already, so these mods need no
        # root-folder flag; only their Root/ files must move up a level.
        for name in apply_root_builder_layout(plan, data_folder=False):
            log(f"Wabbajack: {name} uses Root Builder; moved its Root/ files to the mod's top level")
    else:
        report.root_builder_mods = apply_root_builder_layout(plan)
        for name in report.root_builder_mods:
            log(f"Wabbajack: {name} uses Root Builder; installing it as a root-folder mod "
                "(Root/ files go to the game folder, the rest to Data/)")

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
                         manual_folders=manual_folders)
    if cancel.is_set():
        report.cancelled = True
        return report

    work_root = profile_dir / ".wabbajack_work"
    profile_scratch = work_root / "profile"
    index = ArchiveIndex(work_root / "vfs")
    for h, path in have.items():
        index.add_archive(h, path)

    bsa_root = work_root / "bsa"
    # Archives are packed last, once every file that goes into them exists.
    archives = [d for d in plan.mod_directives if isinstance(d, CreateBSADirective)]
    jobs = ([(d, staging) for d in plan.mod_directives if not isinstance(d, CreateBSADirective)]
            + [(d, profile_scratch) for d in plan.profile_directives]
            + [(d, bsa_root) for d in plan.bsa_inputs]
            + [(d, staging) for d in archives])
    built_mods: "set[str]" = {d.to.split("/", 1)[0] for d in prebuilt}
    cb.on_status("Building files…")
    try:
        for done, (directive, dest_root) in enumerate(jobs, start=1):
            if cancel.is_set():
                report.cancelled = True
                return report
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
            if result.note:
                log(f"Wabbajack: {result.note}")
            if result.success:
                if dest_root is staging:
                    built_mods.add(directive.to.split("/", 1)[0])
            else:
                report.failed_directives.append((directive.to, result.error))
            cb.on_build_progress(done, len(jobs))

        cb.on_status("Writing profile…" if len(targets) == 1
                     else f"Writing {len(targets)} profiles…")
        for mo2_profile, dest in targets.items():
            _write_profile(dest, profile_scratch / mo2_profile, built_mods, report, log,
                           root_parents=root_parents, staging=staging)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

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

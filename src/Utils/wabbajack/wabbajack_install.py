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
   only one profile is used (the one with a ``modlist.txt``, else the first
   by name). ``TEMP_BSA_FILES/<TempID>/...`` builds into scratch as the input
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
   actually built) becomes the profile's, and ``plugins.txt``/``loadorder.txt``
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
import shutil
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from Utils.downloads.download_scheduler import order_by_size, run_smallest_first
from Utils.mods.modlist import ModEntry, read_modlist, write_modlist
from Utils.plugins.plugins import invalidate_plugins_cache
from Utils.profile.profile_state import write_wabbajack_modlist_info

from .downloaders import resolve_downloader
from .downloaders.nexus_source import download_nexus
from .wabbajack_directives import apply_directive
from .wabbajack_hash import hash_file, hashes_match
from .wabbajack_manifest import (
    Archive,
    CreateBSADirective,
    Directive,
    FromArchiveDirective,
    ModList,
    NexusState,
    PatchedFromArchiveDirective,
)
from .wabbajack_vfs import ArchiveIndex

PROFILE_FILES_DIR = "wabbajack_profile_files"
# Wabbajack builds the loose files that go into a rebuilt BSA/BA2 under
# TEMP_BSA_FILES/<TempID>/ before its CreateBSA directive packs them.
_BSA_TEMP_DIR = "temp_bsa_files"
_LOAD_ORDER_FILES = ("plugins.txt", "loadorder.txt")


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
    profile_used: str = ""
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not (self.cancelled or self.failed_archives or self.failed_directives)


# ---------------------------------------------------------------------------
# Step 1: classify
# ---------------------------------------------------------------------------


@dataclass
class InstallPlan:
    mod_directives: "list[Directive]" = field(default_factory=list)      # to = <Name>/<rel>
    profile_directives: "list[Directive]" = field(default_factory=list)  # to = <rel>
    bsa_inputs: "list[Directive]" = field(default_factory=list)          # to = <TempID>/<rel>
    profile_name: str = ""
    unplaced: "list[str]" = field(default_factory=list)

    def archive_hashes(self) -> "set[str]":
        return {d.archive_hash_path[0]
                for d in self.mod_directives + self.profile_directives + self.bsa_inputs
                if isinstance(d, (FromArchiveDirective, PatchedFromArchiveDirective))
                and d.archive_hash_path}


def classify_directives(modlist: ModList) -> InstallPlan:
    """Split the modlist's directives by where they land (see module doc).
    Wabbajack writes ``To`` with Windows separators; they're normalised."""
    plan = InstallPlan()
    by_profile: "dict[str, list[Directive]]" = {}
    for d in modlist.directives:
        to = (getattr(d, "to", "") or "").replace("\\", "/").strip("/")
        parts = to.split("/")
        head = parts[0].lower() if parts else ""
        if head == "mods" and len(parts) >= 3:
            plan.mod_directives.append(replace(d, to="/".join(parts[1:])))
        elif head == "profiles" and len(parts) >= 3:
            by_profile.setdefault(parts[1], []).append(replace(d, to="/".join(parts[2:])))
        elif head == _BSA_TEMP_DIR and len(parts) >= 3:
            plan.bsa_inputs.append(replace(d, to="/".join(parts[1:])))
        else:
            plan.unplaced.append(to)

    if by_profile:
        with_modlist = sorted(name for name, ds in by_profile.items()
                              if any(d.to.lower() == "modlist.txt" for d in ds))
        plan.profile_name = (with_modlist or sorted(by_profile))[0]
        plan.profile_directives = by_profile.pop(plan.profile_name)
        for name, ds in by_profile.items():
            plan.unplaced.extend(f"profiles/{name}/{d.to}" for d in ds)
    return plan


# ---------------------------------------------------------------------------
# Step 2: downloads
# ---------------------------------------------------------------------------


def _verified(path: "Path | None", archive: Archive) -> bool:
    return path is not None and path.is_file() and hashes_match(archive.hash, hash_file(path))


def _cached(archive: Archive, download_dir: Path) -> "Path | None":
    candidate = download_dir / archive.name
    if (archive.name and candidate.is_file()
            and (not archive.size or candidate.stat().st_size == archive.size)
            and _verified(candidate, archive)):
        return candidate
    return None


def _download_automatic(archive: Archive, download_dir: Path, *, nexus_downloader,
                        cb: WabbajackInstallCallbacks,
                        cancel: threading.Event) -> "tuple[Path | None, str]":
    """``(verified path, "")`` or ``(None, why)``. Never asks the user
    anything except the LoversLab login (one retry after a successful one)."""
    def progress(cur, total):
        cb.on_download_progress(archive.hash, cur, total or archive.size)

    state = archive.state
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
            return fn(state, download_dir / archive.name, progress_cb=progress, cancel=cancel)

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
                  max_workers: int, cb: WabbajackInstallCallbacks, cancel: threading.Event,
                  report: WabbajackInstallReport) -> "dict[str, Path]":
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
                archive, download_dir, nexus_downloader=nexus_downloader, cb=cb, cancel=cancel)
        cb.on_download_finish(archive.hash, path is not None)
        with lock:
            if path is not None:
                have[archive.hash] = path
            elif not cancel.is_set():
                need_manual.append((archive, why))

    run_smallest_first(order_by_size(archives, size_key=lambda a: a.size),
                       work, max_workers, stop=cancel)

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
        else:
            report.failed_archives.append(
                (archive.name, why if path is None else
                 "the chosen file doesn't match the modlist's hash"))
    return have


# ---------------------------------------------------------------------------
# Steps 4-5: profile + provenance
# ---------------------------------------------------------------------------


def _write_profile(profile_dir: Path, scratch: Path, built_mods: "set[str]",
                   report: WabbajackInstallReport, log) -> None:
    curated = scratch / "modlist.txt"
    entries: "list[ModEntry]" = []
    if curated.is_file():
        entries = [e for e in read_modlist(curated)
                   if e.is_separator or e.name in built_mods]
    listed = {e.name for e in entries}
    entries += [ModEntry(name=n, enabled=True, locked=False)
                for n in sorted(built_mods - listed)]
    write_modlist(profile_dir / "modlist.txt", entries)

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


def _stamp_mod_meta(mod_dir: Path, modlist: ModList) -> None:
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
    with open(meta, "w", encoding="utf-8") as fh:
        cp.write(fh)


def _rebuild_index(game, profile_dir: Path, log) -> None:
    """Same index rebuild the Collection installer runs after installing
    (collection_install.py, "Updating mod index")."""
    try:
        from Nexus.nexus_meta import collect_root_flagged_mods
        from Utils.deploy.deploy import load_per_mod_strip_prefixes
        from Utils.filemap import rebuild_mod_index
        staging = game.get_effective_mod_staging_path()
        try:
            index_dir = game.get_effective_filemap_path().parent
        except Exception:
            index_dir = profile_dir
        try:
            rf_mods = collect_root_flagged_mods(profile_dir / "modlist.txt", staging, log_fn=log)
        except Exception:
            rf_mods = set()
        rebuild_mod_index(
            index_dir / "modindex.bin", staging,
            strip_prefixes=set(getattr(game, "mod_folder_strip_prefixes", None) or ()) or None,
            per_mod_strip_prefixes=load_per_mod_strip_prefixes(profile_dir),
            allowed_extensions=set(getattr(game, "mod_install_extensions", None) or ()) or None,
            root_folder_mods=set(rf_mods or ()) or None,
            normalize_folder_case=getattr(game, "normalize_folder_case", True))
    except Exception as exc:
        log(f"Wabbajack: mod index rebuild skipped: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_wabbajack_install(*, wabbajack_path: "str | Path", modlist: ModList, game,
                          profile_dir: Path, download_dir: Path, nexus_downloader=None,
                          substitutions: "dict[str, str] | None" = None,
                          max_downloads: int = 3,
                          callbacks: "WabbajackInstallCallbacks | None" = None,
                          control: "WabbajackInstallControl | None" = None,
                          ) -> WabbajackInstallReport:
    """Install ``modlist`` into ``profile_dir`` (see the module doc).
    ``substitutions`` comes from ``wabbajack_directives.path_substitutions``."""
    cb = callbacks or WabbajackInstallCallbacks()
    cancel = (control or WabbajackInstallControl()).cancel
    log = cb.on_log
    report = WabbajackInstallReport()
    profile_dir = Path(profile_dir)
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    game.set_active_profile_dir(profile_dir)
    game.load_paths()
    staging = Path(game.get_effective_mod_staging_path())

    cb.on_status("Reading modlist…")
    plan = classify_directives(modlist)
    report.unplaced_files = list(plan.unplaced)
    report.profile_used = plan.profile_name
    if plan.unplaced:
        log(f"Wabbajack: {len(plan.unplaced)} file(s) outside mods/ and the chosen "
            "profile are not installed")

    needed = plan.archive_hashes()
    archives = [a for a in modlist.archives if a.hash in needed]
    cb.on_status(f"Downloading {len(archives)} archive(s)…")
    have = _download_all(archives, download_dir, nexus_downloader=nexus_downloader,
                         max_workers=max_downloads, cb=cb, cancel=cancel, report=report)
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
    built_mods: "set[str]" = set()
    cb.on_status("Building files…")
    try:
        for done, (directive, dest_root) in enumerate(jobs, start=1):
            if cancel.is_set():
                report.cancelled = True
                return report
            result = apply_directive(directive, dest_root=dest_root,
                                     wabbajack_path=wabbajack_path, archive_index=index,
                                     substitutions=substitutions, bsa_temp_root=bsa_root)
            if result.note:
                log(f"Wabbajack: {result.note}")
            if result.success:
                if dest_root is staging:
                    built_mods.add(directive.to.split("/", 1)[0])
            else:
                report.failed_directives.append((directive.to, result.error))
            cb.on_build_progress(done, len(jobs))

        cb.on_status("Writing profile…")
        _write_profile(profile_dir, profile_scratch, built_mods, report, log)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    report.installed_mods = sorted(built_mods)
    for name in report.installed_mods:
        try:
            _stamp_mod_meta(staging / name, modlist)
        except OSError as exc:
            log(f"Wabbajack: couldn't stamp {name}/meta.ini: {exc}")
    write_wabbajack_modlist_info(profile_dir, {
        "name": modlist.name, "author": modlist.author, "version": modlist.version,
        "game_type": modlist.game_type, "source_file": str(wabbajack_path),
    })
    _rebuild_index(game, profile_dir, log)
    cb.on_status("Done")
    return report

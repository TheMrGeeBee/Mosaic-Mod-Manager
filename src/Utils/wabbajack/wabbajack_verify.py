"""Check an installed Wabbajack profile against its modlist.

Every mod file the modlist builds is looked up in the profile's staging
folder and checked: it must exist, have the recorded size and, unless only a
quick check is asked for, the recorded Wabbajack hash. That is the check the
installer itself makes when it builds a file, run again over everything at
once.

Real case (2026-11-07): a LoreRim install reported a clean finish over 3975
bad files; the game crashed later instead of the install saying so. A Repair
only rebuilds files that are missing or the wrong size, so a file of the right
size with the wrong contents is never fixed by it -- :func:`remove_bad_files`
deletes the files this check rejects, and a Repair then builds them again.

What isn't held to the modlist's hash:

* each mod's ``meta.ini`` -- Mosaic writes its own sections into it;
* rebuilt BSA/BA2 archives -- never byte-identical to Wabbajack's (see
  ``wabbajack_directives``), so one only has to be an archive;
* ``RemappedInlineFile`` -- it holds this user's own paths;
* directive types the installer doesn't build -- only checked to exist.

The game copy (``Stock Game``) is not checked: deploying a profile changes
files in it (the script extender's loader replaces the launcher).

Not every mismatch is damage, and Repair only offers the ones that are
(:attr:`VerifyReport.repairable`). Mods are deployed as links, so whatever the
game writes lands in the mods folder (real case, LoreRim: 23 mismatches, none
an install error):

* a settings file (``.ini``, ``.json``, ...) was most likely changed on
  purpose, by the game, a plugin or the user -- reported as :data:`SETTINGS`;
* a file whose contents are another file of the same modlist was swapped in
  by the modlist itself (a main-menu randomizer copies a random theme over
  the menu files at each launch) -- reported as :data:`SWAPPED`, with the
  file it matches.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from Utils.mods.modlist import read_modlist

from .wabbajack_hash import hash_file, hashes_match
from .wabbajack_install import (
    _ARCHIVE_MAGICS,
    _is_separator_folder,
    _profile_mod_names,
    classify_directives,
    layout_root_builder_mods,
    limit_to_profile_mods,
    profile_choices,
)
from .wabbajack_manifest import (
    CreateBSADirective,
    FromArchiveDirective,
    InlineFileDirective,
    ModList,
    PatchedFromArchiveDirective,
)

MISSING = "missing"
WRONG_SIZE = "wrong size"
CHANGED = "contents differ"
NOT_ARCHIVE = "not a BSA/BA2 archive"
SETTINGS = "settings file changed"
SWAPPED = "replaced by another file of the modlist"

_SETTINGS_SUFFIXES = {".ini", ".json", ".toml", ".yaml", ".yml", ".cfg", ".txt", ".xml"}

_HASHED = (FromArchiveDirective, PatchedFromArchiveDirective, InlineFileDirective)


@dataclass
class VerifyProblem:
    path: str       # relative to the staging folder: <Mod>/<file>
    reason: str
    note: str = ""  # SWAPPED: the modlist file it matches

    @property
    def repairable(self) -> bool:
        return self.reason not in (SETTINGS, SWAPPED)

    @property
    def mod(self) -> str:
        return self.path.split("/", 1)[0]


@dataclass
class VerifyReport:
    checked: int = 0
    skipped: int = 0            # meta.ini files, not held to the modlist
    problems: "list[VerifyProblem]" = field(default_factory=list)
    cancelled: bool = False
    hashed: bool = True

    @property
    def ok(self) -> bool:
        return not self.repairable and not self.cancelled

    @property
    def repairable(self) -> "list[VerifyProblem]":
        return [p for p in self.problems if p.repairable]

    def by_mod(self) -> "dict[str, list[VerifyProblem]]":
        out: "dict[str, list[VerifyProblem]]" = {}
        for p in sorted(self.problems, key=lambda p: p.path.lower()):
            out.setdefault(p.mod, []).append(p)
        return out


def installed_directives(modlist: ModList, wabbajack_path, game, profile_dir: Path,
                         mo2_profile: "str | None" = None) -> list:
    """The mod-file directives the install of ``profile_dir`` built, with
    ``to`` relative to the staging folder -- laid out exactly as the
    installer lays them out. ``mo2_profile`` is the modlist profile it was
    installed from (recorded in the profile since then); without it, the
    profile's own ``modlist.txt`` says which mods belong."""
    choices = profile_choices(modlist)
    if mo2_profile and mo2_profile in choices:
        plan = classify_directives(modlist, mo2_profile)
        names = _profile_mod_names(plan, wabbajack_path)
        if names is not None:
            limit_to_profile_mods(plan, names)
        layout_root_builder_mods(plan, modlist, game)
        return plan.mod_directives
    plan = classify_directives(modlist, choices or None)
    layout_root_builder_mods(plan, modlist, game)
    listed = {e.name.lower() for e in read_modlist(Path(profile_dir) / "modlist.txt")}
    return [d for d in plan.mod_directives
            if d.to.split("/", 1)[0].lower() in listed]


class _Finder:
    """Exact path first; else the same path matched case-insensitively,
    as a Windows-made modlist may spell a folder differently."""

    def __init__(self, root: Path):
        self._root = root
        self._dirs: "dict[Path, dict[str, str]]" = {}
        self._lock = threading.Lock()

    def _listing(self, d: Path) -> "dict[str, str]":
        with self._lock:
            got = self._dirs.get(d)
        if got is None:
            try:
                got = {n.lower(): n for n in os.listdir(d)}
            except OSError:
                got = {}
            with self._lock:
                self._dirs[d] = got
        return got

    def find(self, rel: str) -> "Path | None":
        exact = self._root / rel
        if exact.is_file():
            return exact
        cur = self._root
        for part in rel.split("/"):
            real = self._listing(cur).get(part.lower())
            if real is None:
                return None
            cur = cur / real
        return cur if cur.is_file() else None


def _check(d, finder: _Finder, hash_files: bool) -> "str | None":
    path = finder.find(d.to)
    if path is None:
        return MISSING
    try:
        if isinstance(d, CreateBSADirective):
            with open(path, "rb") as fh:
                return None if fh.read(4) in _ARCHIVE_MAGICS else NOT_ARCHIVE
        if not isinstance(d, _HASHED):
            return None
        if d.size and path.stat().st_size != d.size:
            return WRONG_SIZE
        if hash_files and d.hash and not hashes_match(d.hash, hash_file(path)):
            return CHANGED
    except OSError:
        return MISSING
    return None


def verify_install(modlist: ModList, wabbajack_path, game,
                   profiles: "list[tuple[Path, str | None]]", staging: Path, *,
                   hash_files: bool = True, progress=None,
                   cancel: "threading.Event | None" = None,
                   workers: int = 4) -> VerifyReport:
    """Check every mod file the install of ``profiles`` -- ``(profile dir,
    MO2 profile)`` pairs that share ``staging`` -- built (see the module doc).
    A file several of them use is checked once. ``progress(done_files,
    total_files, done_bytes, total_bytes)`` is called as files are checked,
    from worker threads."""
    report = VerifyReport(hashed=hash_files)
    staging = Path(staging)
    todo, seen = [], set()
    for profile_dir, mo2 in profiles:
        for d in installed_directives(modlist, wabbajack_path, game, profile_dir, mo2):
            key = d.to.lower()
            if key in seen:
                continue
            seen.add(key)
            mod, _, rest = d.to.partition("/")
            if rest.lower() == "meta.ini" or _is_separator_folder(mod):
                report.skipped += 1
            else:
                todo.append(d)
    total_bytes = sum(d.size or 0 for d in todo)
    finder = _Finder(staging)
    lock = threading.Lock()
    done = [0, 0]

    def one(d):
        if cancel is not None and cancel.is_set():
            return None
        reason = _check(d, finder, hash_files)
        with lock:
            done[0] += 1
            done[1] += d.size or 0
            report.checked += 1
            if reason is not None:
                report.problems.append(VerifyProblem(d.to, reason))
            snapshot = (done[0], done[1])
        if progress is not None:
            progress(snapshot[0], len(todo), snapshot[1], total_bytes)
        return reason

    # Largest first, so the long hashes don't all queue up at the end.
    todo.sort(key=lambda d: d.size or 0, reverse=True)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, todo))
    report.cancelled = bool(cancel is not None and cancel.is_set())
    _explain(report, modlist, finder)
    return report


def _explain(report: VerifyReport, modlist: ModList, finder: _Finder) -> None:
    """Tell changed files apart from damaged ones (see the module doc)."""
    wrong = [p for p in report.problems if p.reason in (WRONG_SIZE, CHANGED)]
    if not wrong:
        return
    known: "dict[str, str]" = {}
    for d in modlist.directives:
        h = getattr(d, "hash", "")
        if h and isinstance(d, _HASHED):
            known.setdefault(h.strip(), (getattr(d, "to", "") or "").replace("\\", "/"))
    for p in wrong:
        path = finder.find(p.path)
        if path is None:
            continue
        if path.suffix.lower() in _SETTINGS_SUFFIXES:
            p.reason = SETTINGS
            continue
        try:
            source = known.get(hash_file(path).strip())
        except OSError:
            continue
        if source:
            p.reason, p.note = SWAPPED, source


def remove_bad_files(report: VerifyReport, staging: Path, log=lambda _m: None) -> int:
    """Delete the files ``report`` rejected (a missing one has nothing to
    delete), so a Repair builds them again. Only paths inside ``staging`` are
    touched. Returns how many were deleted."""
    staging = Path(staging)
    root = staging.resolve()
    finder = _Finder(staging)
    removed = 0
    for p in report.repairable:
        if p.reason == MISSING:
            continue
        path = finder.find(p.path)
        if path is None:
            continue
        try:
            if not path.resolve().is_relative_to(root):
                log(f"Wabbajack check: not deleting {p.path}: it's outside the mods folder")
                continue
            path.unlink()
            removed += 1
        except OSError as exc:
            log(f"Wabbajack check: couldn't delete {p.path}: {exc}")
    return removed


def verify_summary(report: VerifyReport) -> str:
    if report.cancelled:
        return (f"Check cancelled after {report.checked} file(s); "
                f"{len(report.problems)} problem(s) found so far.")
    how = "" if report.hashed else " (sizes only)"
    bad = report.repairable
    changed = len(report.problems) - len(bad)
    later = ""
    if changed:
        later = (f" {changed} other file(s) were changed since the install, by the game, "
                 "a plugin or the modlist itself (settings files, swapped menu files); "
                 "Repair leaves those alone.")
    if not bad:
        return f"All {report.checked} mod files match the modlist{how}.{later}"
    mods: "dict[str, int]" = {}
    for p in bad:
        mods[p.mod] = mods.get(p.mod, 0) + 1
    top = ", ".join(f"{m} ({n})" for m, n in sorted(mods.items(), key=lambda kv: -kv[1])[:3])
    more = f" and {len(mods) - 3} more" if len(mods) > 3 else ""
    return (f"{len(bad)} of {report.checked} mod files are missing or damaged{how}, "
            f"in {len(mods)} mod(s): {top}{more}.{later}")

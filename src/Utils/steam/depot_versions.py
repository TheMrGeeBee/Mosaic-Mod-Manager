"""Switching a Steam game to another build with Valve's own depots.

The user downloads the target build's depots in Steam's console
(``download_depot <app> <depot> <manifest>``, one line per paste), with their
own Steam login and licence; Mosaic never holds credentials or talks to
Steam's servers. This module (no Qt) then:

* reads which depots the user has installed, from the game's
  ``appmanifest_<app>.acf`` (``InstalledDepots``: language and owned DLC
  depots with their current manifests),
* plans a switch from a version table (shipped per app in ``versions/``, plus
  the user's own entries pasted from SteamDB), skipping depots whose
  installed manifest already matches,
* finds finished downloads from Steam's ``content_log.txt``,
* applies the downloaded files to the game folder with a full backup,
  staged copies, ``os.replace`` and rollback, and switches back from that
  backup.

What Steam writes to ``logs/console_log.txt`` (real sample, 2026-10-08,
Fallout 4 exe depot; ``content_log.txt`` only has the HTTP side)::

    Downloading depot 377162 (1 files, 48 MB) ...
    Depot download complete : "/home/u/.local/share/Steam/steamrt64\\steamapps\\content\\app_377160\\depot_377162" (manifest 5847529232406005096)

The files land in ``<Steam>/steamrt64/steamapps/content/app_<app>/depot_<depot>/``
(the log prints part of the path with backslashes). A re-run of an already
complete download logs "complete" again at once.

Mosaic never edits Steam's appmanifest. Steam may update the game back to
the latest build if it is launched through Steam.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .acf import get_ci, read_vdf

VERSIONS_DIR = Path(__file__).with_name("versions")
STATE_FILENAME = "version_state.json"
BACKUP_DIRNAME = "version_backup"      # a single throw-away backup; pre-store
STORE_DIRNAME = "version_store"        # one slot per build Mosaic keeps
INDEX_FILENAME = "version_store.json"
USER_VERSIONS_FILENAME = "steam_versions.user.json"
_STAGE_SUFFIX = ".mosaic-version"
_CHUNK = 1 << 20

_START_RE = re.compile(r"Downloading depot (\d+) \((\d+) files?, ([\d.]+) ([KMGT]?B)\)")
_DONE_RE = re.compile(r'Depot download complete : "(.+)" \(manifest (\d+)\)')


class VersionSwitchError(RuntimeError):
    """A switch can't start or failed; the message is for the user."""


# ---------------------------------------------------------------------------
# Steam: roots, the game's appmanifest, logs
# ---------------------------------------------------------------------------

def steam_roots() -> "list[Path]":
    """Existing Steam roots (native, Flatpak, Snap, ~/.steam/steam), each once."""
    from Utils.wine_proton.steam_finder import _STEAM_CANDIDATES
    out, seen = [], set()
    for c in _STEAM_CANDIDATES:
        try:
            key = c.resolve()
        except OSError:
            continue
        if c.is_dir() and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def appmanifest_path(game_root, app_id: str) -> "Path | None":
    from Utils.wine_proton.steam_finder import library_steamapps_for
    steamapps = library_steamapps_for(game_root)
    if steamapps is None:
        return None
    p = steamapps / f"appmanifest_{app_id}.acf"
    return p if p.is_file() else None


def read_installed_depots(game_root, app_id: str) -> "dict[str, str]":
    """``{depot: manifest}`` the user has installed (``InstalledDepots``)."""
    path = appmanifest_path(game_root, app_id)
    if path is None:
        return {}
    depots = get_ci(read_vdf(path), "AppState", "InstalledDepots", default={}) or {}
    return {str(d): str(get_ci(v, "manifest", default="")) for d, v in depots.items()
            if isinstance(v, dict) and get_ci(v, "manifest")}


_LOG_NAMES = ("console_log.previous.txt", "console_log.txt",
              "content_log.previous.txt", "content_log.txt")


def content_log_paths(roots: "list[Path] | None" = None) -> "list[Path]":
    """Steam's logs that carry ``download_depot`` progress, oldest first:
    the lines are in ``console_log.txt`` (2026-10-08 sample), and Steam
    rotates each log to ``*.previous.txt``."""
    out = []
    for r in roots if roots is not None else steam_roots():
        out += [r / "logs" / n for n in _LOG_NAMES if (r / "logs" / n).is_file()]
    return out


def _norm_path(raw: str) -> Path:
    return Path(raw.replace("\\", "/"))


def completed_downloads(log_text: str) -> "dict[tuple[str, str], Path]":
    """``{(depot, manifest): folder}`` for what each depot folder holds NOW:
    Steam reuses one ``depot_<id>`` folder for every manifest, so only the
    latest "Depot download complete" line per folder counts. Real case
    (2026-10-08): the 1.10.163 exe depot was downloaded, then the same
    folder got the 1.11.240 exe; the older line must no longer match."""
    latest: "dict[Path, tuple[str, str]]" = {}
    for m in _DONE_RE.finditer(log_text or ""):
        folder = _norm_path(m.group(1))
        dm = re.search(r"depot_(\d+)$", folder.name)
        if dm:
            latest[folder] = (dm.group(1), m.group(2))
    return {key: folder for folder, key in latest.items()}


def download_sizes(log_text: str) -> "dict[str, tuple[int, str]]":
    """``{depot: (files, "48 MB")}`` from the latest "Downloading depot" line."""
    out = {}
    for m in _START_RE.finditer(log_text or ""):
        out[m.group(1)] = (int(m.group(2)), f"{m.group(3)} {m.group(4)}")
    return out


def find_downloaded_depot(app_id: str, depot: str, manifest: str, *,
                          log_text: "str | None" = None,
                          roots: "list[Path] | None" = None) -> "Path | None":
    """The folder of a finished ``download_depot`` for exactly this
    manifest, per Steam's content log. Only the log ties a folder to a
    manifest (a folder alone may hold an older download), so there's no
    blind fallback here; :func:`content_folder_candidates` lists where to
    look when the log is unavailable."""
    if log_text is None:
        log_text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                             for p in content_log_paths(roots))
    folder = completed_downloads(log_text).get((str(depot), str(manifest)))
    if folder is not None and folder.is_dir() and f"app_{app_id}" in folder.parent.name:
        return folder
    return None


# ---------------------------------------------------------------------------
# Trusting a depot folder
# ---------------------------------------------------------------------------
# Steam reuses one depot_<id> folder for every manifest and does NOT shorten
# a file that got smaller: real case (2026-10-08), the 1.11.221 Fallout4.exe
# (55,293,864 bytes) was written over the 1.10.163 one (65,503,104) and kept
# its last 10.2 MB -- the version resource still read 1.11.221.0. So a
# folder is only trusted when Mosaic knows it started empty ("clean start":
# seen absent, or cleared by Mosaic) and every download completed into it
# since then was the wanted manifest.

DOWNLOADS_FILENAME = "depot_downloads.json"
_DONE_TS_RE = re.compile(
    r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] Depot download complete : "(.+)" \(manifest (\d+)\)',
    re.MULTILINE)


def _now_stamp() -> str:
    """Local time in Steam's log format, so stamps compare as strings."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def download_history(log_text: str) -> "dict[Path, list[tuple[str, str]]]":
    """``{folder: [(timestamp, manifest), ...]}`` in log order."""
    out: "dict[Path, list[tuple[str, str]]]" = {}
    for m in _DONE_TS_RE.finditer(log_text or ""):
        out.setdefault(_norm_path(m.group(2)), []).append((m.group(1), m.group(3)))
    return out


def _atomic_write_json(path: Path, data) -> None:
    """Write *data* as JSON, replacing *path* in one step.

    The temp file carries the writing thread's id: depot_downloads.json is
    written from worker threads (depot_status marks a clean start whenever it
    finds a folder absent), and the play bar's build dropdown can have two of
    those in flight at once -- one labelling the menu, one checking a pick. A
    shared "<name>.tmp" lets them overwrite each other's half-written file and
    land truncated JSON, which reads back as "no record" and makes a finished
    download look like it holds another version.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# Marking a clean start is a read-modify-write of one shared file, so two
# threads doing it at once can lose one of the two records -- and a depot with
# no record is treated as holding another version, i.e. the user is told to
# clear a download that is in fact fine. Within one process a lock settles it.
_RECORDS_LOCK = threading.Lock()


def _load_records(state_dir) -> dict:
    try:
        return json.loads((Path(state_dir) / DOWNLOADS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_records(state_dir, records: dict) -> None:
    _atomic_write_json(Path(state_dir) / DOWNLOADS_FILENAME, records)


def mark_clean_start(state_dir, folder: Path, when: "str | None" = None) -> None:
    with _RECORDS_LOCK:
        records = _load_records(state_dir)
        records[str(folder)] = {"clean_since": when or _now_stamp()}
        _save_records(state_dir, records)


def clear_depot_folder(state_dir, folder: Path) -> None:
    """Delete a depot download folder (and Steam's resume file for it) so the
    next download starts empty, and record that clean start."""
    folder = Path(folder)
    shutil.rmtree(folder, ignore_errors=True)
    dm = re.search(r"depot_(\d+)$", folder.name)
    am = re.search(r"app_(\d+)$", folder.parent.name)
    if dm and am:
        (folder.parent / f"state_{am.group(1)}_{dm.group(1)}.patch").unlink(missing_ok=True)
    mark_clean_start(state_dir, folder)


def expected_depot_folder(app_id: str, depot: str,
                          roots: "list[Path] | None" = None) -> "Path | None":
    """Where ``download_depot`` writes: the existing folder if any, else
    ``<first Steam root>/steamrt64/steamapps/content/...`` (as observed)."""
    found = content_folder_candidates(app_id, depot, roots)
    if found:
        return found[0]
    rs = roots if roots is not None else steam_roots()
    return (rs[0] / "steamrt64" / "steamapps" / "content" / f"app_{app_id}"
            / f"depot_{depot}") if rs else None


@dataclass
class DepotStatus:
    state: str                     # "downloaded" | "waiting" | "needs_clear"
    folder: "Path | None" = None
    holds: str = ""                # needs_clear: the manifest last written there, if known


def depot_status(app_id: str, depot: str, manifest: str, *, state_dir,
                 log_text: "str | None" = None,
                 roots: "list[Path] | None" = None) -> DepotStatus:
    if log_text is None:
        log_text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                             for p in content_log_paths(roots))
    folder = expected_depot_folder(app_id, depot, roots)
    if folder is None:
        return DepotStatus("waiting")
    history = download_history(log_text).get(folder, [])
    if not folder.exists():
        mark_clean_start(state_dir, folder)      # absent == clean
        return DepotStatus("waiting", folder)
    rec = _load_records(state_dir).get(str(folder))
    if rec is None:
        return DepotStatus("needs_clear", folder, history[-1][1] if history else "")
    since = [(ts, man) for ts, man in history if ts >= rec.get("clean_since", "")]
    if not since:
        return DepotStatus("waiting", folder)
    if all(man == str(manifest) for _ts, man in since):
        return DepotStatus("downloaded", folder)
    return DepotStatus("needs_clear", folder, since[-1][1])


def content_folder_candidates(app_id: str, depot: str,
                              roots: "list[Path] | None" = None) -> "list[Path]":
    out = []
    for r in roots if roots is not None else steam_roots():
        for base in (r / "steamrt64" / "steamapps", r / "steamapps"):
            p = base / "content" / f"app_{app_id}" / f"depot_{depot}"
            if p.is_dir():
                out.append(p)
    return out


# ---------------------------------------------------------------------------
# Version tables
# ---------------------------------------------------------------------------

@dataclass
class VersionEntry:
    label: str
    depots: "dict[str, str]"
    title: str = ""
    exe_version: str = ""
    remove: "list[str]" = field(default_factory=list)
    source: str = ""
    user: bool = False


@dataclass
class VersionTable:
    app_id: str
    exe_file: str = ""
    versions: "list[VersionEntry]" = field(default_factory=list)

    def get(self, label: str) -> "VersionEntry | None":
        return next((v for v in self.versions if v.label == label), None)


def _entry(d: dict, user: bool = False) -> VersionEntry:
    return VersionEntry(
        label=str(d.get("label", "")), title=str(d.get("title", "")),
        exe_version=str(d.get("exe_version", "")),
        depots={str(k): str(v) for k, v in (d.get("depots") or {}).items()},
        remove=[str(r) for r in d.get("remove") or []], source=str(d.get("source", "")),
        user=user)


def load_table(app_id: str, user_file: "Path | None" = None) -> VersionTable:
    """The shipped table for *app_id* (if any) plus the user's entries
    (which replace shipped ones with the same label)."""
    table = VersionTable(app_id=str(app_id))
    shipped = VERSIONS_DIR / f"{app_id}.json"
    if shipped.is_file():
        data = json.loads(shipped.read_text(encoding="utf-8"))
        table.exe_file = str(data.get("exe_file", ""))
        table.versions = [_entry(v) for v in data.get("versions") or []]
    if user_file is not None and Path(user_file).is_file():
        try:
            data = json.loads(Path(user_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for v in data.get("versions") or []:
            e = _entry(v, user=True)
            table.versions = [x for x in table.versions if x.label != e.label] + [e]
        table.exe_file = table.exe_file or str(data.get("exe_file", ""))
    return table


def save_user_entry(user_file: Path, entry: VersionEntry, exe_file: str = "") -> None:
    user_file = Path(user_file)
    try:
        data = json.loads(user_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    versions = [v for v in data.get("versions") or [] if v.get("label") != entry.label]
    d = asdict(entry)
    d.pop("user", None)
    versions.append(d)
    data["versions"] = versions
    if exe_file:
        data.setdefault("exe_file", exe_file)
    user_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = user_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(user_file)


def parse_depot_paste(text: str) -> "dict[str, str]":
    """``{depot: manifest}`` from text pasted from SteamDB or typed: any line
    holding a depot id (up to 10 digits) and a manifest id (15+ digits), in
    either order, e.g. ``377162  5847529232406005096`` or a
    ``download_depot 377160 377162 5847529232406005096`` line."""
    out = {}
    for line in (text or "").splitlines():
        nums = re.findall(r"\d+", line)
        manifests = [n for n in nums if len(n) >= 15]
        depots = [n for n in nums if 3 <= len(n) <= 10]
        if manifests and depots:
            # a download_depot line has app then depot: the depot is the last short number
            out[depots[-1]] = manifests[-1]
    return out


def capture_current(game_root, app_id: str, exe_file: str) -> "VersionEntry | None":
    """The installed build as a table entry (its depots' current manifests
    and the exe's version), so switching back can use depots too."""
    from Utils.wizard_support.pe_version import format_version, read_file_version
    depots = read_installed_depots(game_root, app_id)
    if not depots:
        return None
    path = appmanifest_path(game_root, app_id)
    build = str(get_ci(read_vdf(path), "AppState", "buildid", default="")) if path else ""
    version = read_file_version(Path(game_root) / exe_file) if exe_file else None
    exe = format_version(version) if version else ""
    # Named by Steam's build, not the exe: an exe-only downgrade (Mosaic's
    # Fallout 4 wizard) leaves an old exe on the newer build's data files.
    return VersionEntry(label=f"Steam build {build}" if build else "As installed",
                        depots=depots, exe_version="",
                        title=f"As installed by Steam (exe reports {exe})" if exe
                        else "As installed by Steam", source="Read from the appmanifest")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class SwitchPlan:
    download: "list[tuple[str, str]]"      # (depot, manifest) to fetch
    unchanged: "list[str]"                 # installed manifest already matches
    not_in_version: "list[str]"            # installed, but the entry has no manifest


def plan_switch(entry: VersionEntry, installed: "dict[str, str]") -> SwitchPlan:
    """Only the user's own depots (language, owned DLC), and only those
    whose installed manifest differs from the target's."""
    download, unchanged, missing = [], [], []
    for depot, cur in installed.items():
        target = entry.depots.get(depot)
        if target is None:
            missing.append(depot)
        elif target == cur:
            unchanged.append(depot)
        else:
            download.append((depot, target))
    return SwitchPlan(download, unchanged, missing)


def console_command(app_id: str, depot: str, manifest: str) -> str:
    return f"download_depot {app_id} {depot} {manifest}"


# ---------------------------------------------------------------------------
# Applying and switching back
# ---------------------------------------------------------------------------

def _copy_hash(src: Path, dst: Path) -> str:
    h = hashlib.sha256()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        for chunk in iter(lambda: fi.read(_CHUNK), b""):
            h.update(chunk)
            fo.write(chunk)
    shutil.copystat(src, dst)
    return h.hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _free_bytes(path: Path) -> int:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _resolve_ci(root: Path, rel: str) -> Path:
    """``root/rel`` matching each existing path part case-insensitively (the
    game folder may differ in case from the depot); parts that don't exist
    yet keep the depot's spelling."""
    cur = root
    for part in Path(rel).parts:
        exact = cur / part
        if not exact.exists() and cur.is_dir():
            try:
                match = next((c for c in cur.iterdir() if c.name.lower() == part.lower()), None)
            except OSError:
                match = None
            exact = match or exact
        cur = exact
    return cur


def _depot_files(depot_dirs: "dict[str, Path]") -> "dict[str, Path]":
    files: "dict[str, Path]" = {}
    for _depot, folder in depot_dirs.items():
        for p in sorted(Path(folder).rglob("*")):
            if p.is_file():
                files[p.relative_to(folder).as_posix()] = p
    return files


def state_path(state_dir) -> Path:
    return Path(state_dir) / STATE_FILENAME


def load_state(state_dir) -> "dict | None":
    try:
        return json.loads(state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(state_dir, state: dict) -> None:
    _atomic_write_json(state_path(state_dir), state)


def _is_wine_prefix(path: str) -> bool:
    """A Wine prefix, or a Proton one (``<dir>/pfx`` holds the real prefix)."""
    return any(os.path.isdir(os.path.join(path, *sub, "drive_c"))
               for sub in ((), ("pfx",)))


def deployment_traces(game_root, *, deploy_active: bool = False) -> "list[str]":
    """Why the game folder may still hold mod files, for any game (empty if
    clean). Real case (Fallout 4, 2026-10-08): Mosaic's record said deployed
    while ``Data/`` was clean but 9 root-folder mod files were still linked,
    and none of Skyrim's markers (``Data/.mm_deployed``, ``Data_Core/``,
    ``SkyrimSELauncher.bak``) existed -- so check generally."""
    root = Path(game_root)
    traces = []
    if deploy_active:
        traces.append("Mosaic's record says mods are deployed")
    for marker in ("Data/.mm_deployed", "Data_Core"):
        if (root / marker).exists():
            traces.append(f"{marker} exists")
    baks = sorted(p.name for p in root.glob("*Launcher*.bak"))
    if baks:
        traces.append(f"{baks[0]} exists (the launcher is swapped for a script extender)")
    for dirpath, dirnames, filenames in os.walk(root):
        # A Wine/Proton prefix inside the game folder (Cyberpunk 2077:
        # bin/x64/prefix_proton-*) is full of its own symlinks, none of them mods.
        dirnames[:] = [n for n in dirnames if not _is_wine_prefix(os.path.join(dirpath, n))]
        hit = next((n for n in dirnames + filenames if os.path.islink(os.path.join(dirpath, n))), None)
        if hit is not None:
            rel = os.path.relpath(os.path.join(dirpath, hit), root)
            traces.append(f"mod files are linked into the game folder (e.g. {rel})")
            break
    return traces


def runtime_swap_blocker(game_root, state_dir) -> str:
    """Why a build can't be changed while Skyrim's runtime swap is applied.

    That swap patches the exe, the masters and two BSAs back to an older
    version on top of the newer build's files, on purpose -- so the two
    systems own nine of the same files. Switching build now would take the
    patched files to be this build's own and keep them as that build in the
    store, and the swap's own revert would later write its build's files over
    whichever build is installed by then. One of them has to be undone first,
    and the runtime swap is the cheap one. Empty for every other game, which
    has no such record.
    """
    try:
        from Utils.modding_tools.skyrim_runtime import describe
        info = describe(game_root, state_dir, game_running=False)
    except Exception:
        return ""
    if not info.swapped:
        return ""
    def _v(parts, fallback):
        return _short_version(".".join(str(p) for p in parts)) if parts else fallback
    return (f"The Skyrim runtime swap is applied: the exe and the files it patches read "
            f"{_v(info.recorded_to, 'another version')} while the rest of the game is "
            f"{_v(info.recorded_from, 'a different build')}. Switching build now would keep "
            "those patched files as if they were part of it. Revert the runtime swap first "
            "(Wizard \u25b8 Skyrim runtime swap), then switch.")


def _check_can_change(game_root: Path, game_running: bool, deploy_active: bool = False,
                      state_dir=None) -> None:
    if game_running:
        raise VersionSwitchError("The game is running. Close it first.")
    traces = deployment_traces(game_root, deploy_active=deploy_active)
    if traces:
        raise VersionSwitchError("Mods are deployed (" + "; ".join(traces) + "). Restore the "
                                 "game first, so only the game's own files are in its folder.")
    blocker = runtime_swap_blocker(game_root, state_dir) if state_dir is not None else ""
    if blocker:
        raise VersionSwitchError(blocker)


def apply_version(game_root, entry: VersionEntry, depot_dirs: "dict[str, Path]", state_dir, *,
                  from_label: str, exe_file: str = "", game_running: bool = False,
                  deploy_active: bool = False, log_fn=lambda _m: None) -> dict:
    """Put the downloaded depot files into the game folder. Every file that
    gets replaced (and every file in ``entry.remove``) is first copied to the
    backup with its sha256; new files are staged next to their targets and
    swapped in with ``os.replace``. Any failure puts everything back. Returns
    the saved state."""
    game_root = Path(game_root)
    _check_can_change(game_root, game_running, deploy_active, state_dir)
    if load_state(state_dir) is not None:
        raise VersionSwitchError("The game is already switched to another version. "
                                 "Switch back first.")
    files = _depot_files(depot_dirs)
    if not files:
        raise VersionSwitchError("The downloaded depots are empty.")
    backup = _slot_dir(state_dir, from_label or "previous")

    targets = {rel: _resolve_ci(game_root, rel) for rel in files}
    removals = {rel: _resolve_ci(game_root, rel) for rel in entry.remove}
    removals = {rel: p for rel, p in removals.items() if p.is_file() and rel not in files}
    # Paths another known version differs on that this one leaves alone. They
    # aren't part of this switch, but the slot has to hold them or switching
    # back to it would read them as "not in that build" and delete them.
    keep = {rel: _resolve_ci(game_root, rel) for rel in read_index(state_dir).get("managed") or []
            if rel not in files and rel not in removals}
    saved = list(targets.values()) + list(removals.values()) + list(keep.values())
    backup_bytes = sum(p.stat().st_size for p in saved if p.is_file())
    stage_bytes = sum(p.stat().st_size for p in files.values())
    same_fs = os.stat(_existing(backup)).st_dev == os.stat(game_root).st_dev
    need_game = stage_bytes + (backup_bytes if same_fs else 0)
    if _free_bytes(game_root) < need_game * 1.05:
        raise VersionSwitchError(f"Not enough free space: about {need_game / 1e9:.1f} GB is needed "
                                 "next to the game for the backup and the new files.")
    if not same_fs and _free_bytes(backup) < backup_bytes * 1.05:
        raise VersionSwitchError(f"Not enough free space for the {backup_bytes / 1e9:.1f} GB backup.")

    record: "dict[str, dict]" = {}
    kept: "dict[str, str]" = {}
    staged: "dict[str, Path]" = {}
    swapped: "list[str]" = []
    removed: "list[str]" = []
    try:
        shutil.rmtree(backup, ignore_errors=True)
        log_fn(f"Backing up {sum(1 for p in saved if p.is_file())} "
               f"file(s) ({backup_bytes / 1e9:.1f} GB)…")
        for rel, target in list(targets.items()) + list(removals.items()):
            if target.is_file():
                record.setdefault(rel, {})["original_sha256"] = _copy_hash(target, backup / rel)
        for rel, target in keep.items():
            if target.is_file():
                kept[rel] = _copy_hash(target, backup / rel)
        log_fn(f"Preparing {len(files)} new file(s)…")
        for rel, src in files.items():
            stage = targets[rel].with_name(targets[rel].name + _STAGE_SUFFIX)
            record.setdefault(rel, {})["new_sha256"] = _copy_hash(src, stage)
            staged[rel] = stage
        for rel, stage in staged.items():
            os.replace(stage, targets[rel])
            swapped.append(rel)
        for rel, p in removals.items():
            p.unlink()
            removed.append(rel)
            record[rel]["removed"] = True
        if exe_file and entry.exe_version:
            from Utils.wizard_support.pe_version import format_version, read_file_version
            got = read_file_version(game_root / exe_file)
            if got is None or format_version(got) != entry.exe_version:
                raise VersionSwitchError(
                    f"{exe_file} is version {format_version(got) if got else 'unknown'} after "
                    f"the switch, not {entry.exe_version}: the downloads don't match this "
                    "version. Nothing was changed.")
    except Exception as exc:
        log_fn(f"Switch failed ({exc}); putting the previous files back…")
        for rel in swapped + removed:
            src = backup / rel
            dst = targets.get(rel) or removals[rel]
            if src.is_file():
                shutil.copy2(src, dst)
            elif dst.exists():
                dst.unlink()          # a file the old version didn't have
        for stage in staged.values():
            stage.unlink(missing_ok=True)
        shutil.rmtree(backup, ignore_errors=True)
        if isinstance(exc, VersionSwitchError):
            raise
        raise VersionSwitchError(f"Couldn't switch versions: {exc}") from exc

    state = {"current": entry.label, "from": from_label,
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "backup_dir": str(backup), "files": record}
    _write_state(state_dir, state)
    _register_slot(state_dir, from_label=from_label or "previous", to_label=entry.label,
                   slot_dir=backup, record=record, kept=kept,
                   touched=set(files) | set(removals))
    log_fn(f"Switched to {entry.label}: {len(swapped)} file(s) replaced or added, "
           f"{len(removed)} removed.")
    return state


def _existing(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def revert_version(game_root, state_dir, *, game_running: bool = False,
                   deploy_active: bool = False, log_fn=lambda _m: None) -> str:
    """Switch back to the build the last switch left, from Mosaic's store.
    Returns the label switched back to.

    The build being left is kept as a slot of its own on the way out, so
    switching to it again later needs no download either -- that is the whole
    point of the store, and why this no longer throws the backup away.
    """
    state = load_state(state_dir)
    if state is None:
        raise VersionSwitchError("There's no version switch to undo.")
    return swap_to(game_root, state_dir, state.get("from") or "", game_running=game_running,
                   deploy_active=deploy_active, log_fn=log_fn)


# ---------------------------------------------------------------------------
# The version store: one complete file set per build that is NOT in the game
# folder, so going back to a build is a local file swap, not another download.
# ---------------------------------------------------------------------------
# Measured on a real Skyrim SE pair (1.7.104 vs 1.6.1170, 2026-11-06): 29 of
# the 46 files a switch touches genuinely differ, 14.42 GB of 14.5 -- every BSA
# is repacked between builds. So there is nothing to dedupe or patch; the only
# way to make a round trip free is to keep both sets of bytes. Steam's own
# download folder can't do it: it reuses one depot_<id> folder per depot, so it
# holds exactly one manifest at a time (see "Trusting a depot folder" above).
#
# The index's invariant: the game folder holds the build named ``current``, and
# every OTHER build Mosaic has on hand is a slot holding, for each path in
# ``managed``, either that build's file or ``null`` meaning "this build has no
# such file". ``managed`` is every path any known switch touches, so a swap can
# also delete a file the target build simply doesn't have.
#
# ``version_backup/`` was the same thing for a single build, deleted on revert.
# Slots record their own directory, so a switch made before the store existed
# keeps its files where they are and still works.

def _safe_label(label: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", label or "previous")


def _slot_dir(state_dir, label: str) -> Path:
    return Path(state_dir) / STORE_DIRNAME / _safe_label(label)


def _empty_index() -> dict:
    return {"version": 1, "base": "", "current": "", "managed": [], "slots": {}}


def read_index(state_dir) -> dict:
    """The store index, migrating an install that predates it.

    Such an install has no index, only a ``version_state.json`` from a switch
    made while ``version_backup/`` was a throw-away backup. That state does
    describe one complete slot -- the build switched away from, with a hash per
    file it replaced and no entry for the files the new build added -- so it
    converts exactly. The conversion isn't written back here; the next switch
    saves it.
    """
    try:
        index = json.loads((Path(state_dir) / INDEX_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        index = None
    if isinstance(index, dict) and isinstance(index.get("slots"), dict):
        return index
    state = load_state(state_dir)
    if state is None:
        return _empty_index()
    files = state.get("files") or {}
    return {"version": 1, "base": state.get("from") or "",
            "current": state.get("current") or "", "managed": sorted(files),
            "slots": {state.get("from") or "": {
                "dir": state.get("backup_dir") or "", "at": state.get("at") or "",
                # No original_sha256 == the build being left didn't have the file.
                "files": {rel: info.get("original_sha256")
                          for rel, info in files.items()}}}}


def _write_index(state_dir, index: dict) -> None:
    _atomic_write_json(Path(state_dir) / INDEX_FILENAME, index)


def stored_labels(state_dir) -> "list[str]":
    """Builds Mosaic can switch to with no download, newest slot last."""
    slots = read_index(state_dir).get("slots") or {}
    return sorted(slots, key=lambda lbl: (slots[lbl].get("at") or "", lbl))


def current_label(state_dir, base_label: str = "") -> str:
    """The build in the game folder, or *base_label* if nothing was switched."""
    return read_index(state_dir).get("current") or base_label


def slot_bytes(slot: dict) -> int:
    """How much disk one stored build takes."""
    d, total = Path(slot.get("dir") or ""), 0
    for rel, sha in (slot.get("files") or {}).items():
        if sha is None:
            continue
        try:
            total += (d / rel).stat().st_size
        except OSError:
            pass
    return total


def store_bytes(state_dir) -> int:
    """How much disk every build Mosaic keeps for this game takes."""
    return sum(slot_bytes(s) for s in (read_index(state_dir).get("slots") or {}).values())


def _backfill_slots(slots: dict, *, skip: str, new_rels: "set[str]", source: Path) -> None:
    """Give older slots the paths that only became managed now.

    A path becomes managed the first time a switch touches it, so until now no
    switch had changed it -- which means its content in the game folder just
    before this switch, i.e. what *source* holds, is also what every older
    slot's build had. Without this, switching to one of those slots would read
    the missing entry as "that build has no such file" and delete it.
    """
    for label, slot in slots.items():
        if label == skip:
            continue
        files = slot.setdefault("files", {})
        dst = Path(slot.get("dir") or "")
        for rel in new_rels:
            if rel in files:
                continue
            src = source / rel
            files[rel] = _copy_hash(src, dst / rel) if src.is_file() else None


def _register_slot(state_dir, *, from_label: str, to_label: str, slot_dir: Path,
                   record: dict, kept: "dict[str, str]", touched: "set[str]") -> None:
    """Keep the build an :func:`apply_version` just left as a store slot."""
    index = read_index(state_dir)
    slots = index.setdefault("slots", {})
    was_managed = set(index.get("managed") or [])
    index.setdefault("base", from_label)
    files = {rel: info.get("original_sha256") for rel, info in record.items()}
    files.update(kept)
    for rel in was_managed:
        files.setdefault(rel, None)
    _backfill_slots(slots, skip=from_label, new_rels=touched - was_managed,
                    source=Path(slot_dir))
    slots[from_label] = {"dir": str(slot_dir), "files": files,
                         "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    slots.pop(to_label, None)          # its files are in the game folder now
    index["managed"] = sorted(was_managed | touched)
    index["current"] = to_label
    _write_index(state_dir, index)


def _sync_state(state_dir, index: dict) -> None:
    """Keep ``version_state.json`` saying what the index says, so the wizard's
    check page and an older Mosaic still see the switch."""
    base, cur = index.get("base") or "", index.get("current") or ""
    if not cur or cur == base:
        state_path(state_dir).unlink(missing_ok=True)
        return
    slot = (index.get("slots") or {}).get(base) or {}
    _write_state(state_dir, {
        "current": cur, "from": base,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backup_dir": slot.get("dir", ""),
        "files": {rel: {"original_sha256": sha}
                  for rel, sha in (slot.get("files") or {}).items() if sha}})


def swap_to(game_root, state_dir, label: str, *, game_running: bool = False,
            deploy_active: bool = False, log_fn=lambda _m: None) -> str:
    """Switch to a build Mosaic already keeps, with no Steam download.

    The game folder's copies become the slot for the build being left, and the
    target slot's files take their place. Every file is checked against the
    hash recorded when the slot was written, before anything is touched, so
    there's no separate exe-version check to make: a build whose bytes all
    match is that build.

    Unlike :func:`apply_version`, files are staged and swapped one at a time,
    because the outgoing slot is written first and is a complete rollback
    source -- peak extra space is one file, not the whole build.
    """
    game_root = Path(game_root)
    _check_can_change(game_root, game_running, deploy_active, state_dir)
    index = read_index(state_dir)
    slots = index.get("slots") or {}
    cur = index.get("current") or ""
    if label and label == cur:
        return cur
    slot = slots.get(label)
    if slot is None:
        raise VersionSwitchError(
            f"Mosaic doesn't keep {label or 'that version'}'s files, so it can't switch to it "
            "without downloading. Use the Game version wizard.")
    if not cur:
        raise VersionSwitchError("Mosaic doesn't know which build is installed, so it can't "
                                 "keep it before switching. Use the Game version wizard.")
    want = dict(slot.get("files") or {})
    slot_dir = Path(slot.get("dir") or "")
    out_dir = _slot_dir(state_dir, cur)
    if out_dir == slot_dir:
        raise VersionSwitchError(f"{cur} and {label} would share one folder in Mosaic's store. "
                                 "Rename one of them in your version list.")

    log_fn(f"Checking Mosaic's copy of {label}…")
    for rel, sha in want.items():
        if sha is None:
            continue
        src = slot_dir / rel
        if not src.is_file() or _sha256(src) != sha:
            raise VersionSwitchError(
                f"Mosaic's copy of {rel} for {label} is missing or damaged, so switching could "
                'leave a broken game. Use Steam\'s "Verify integrity of game files" instead.')

    managed = sorted(set(index.get("managed") or []) | set(want))
    targets = {rel: _resolve_ci(game_root, rel) for rel in managed}
    out_bytes = sum(p.stat().st_size for p in targets.values() if p.is_file())
    incoming = [(slot_dir / rel).stat().st_size for rel, sha in want.items() if sha is not None]
    if _free_bytes(_existing(out_dir)) < out_bytes * 1.05:
        raise VersionSwitchError(
            f"Not enough free space: about {out_bytes / 1e9:.1f} GB is needed to keep {cur}'s "
            "files so you can switch back to it.")
    need_game = max(0, sum(incoming) - out_bytes) + max(incoming or [0])
    if _free_bytes(game_root) < need_game * 1.05:
        raise VersionSwitchError(f"Not enough free space: about {need_game / 1e9:.1f} GB is "
                                 "needed next to the game to put the new files in place.")

    out_files: "dict[str, str | None]" = {}
    done: "list[str]" = []
    try:
        shutil.rmtree(out_dir, ignore_errors=True)
        log_fn(f"Keeping {cur}'s {sum(1 for p in targets.values() if p.is_file())} file(s) "
               f"({out_bytes / 1e9:.1f} GB), so you can switch back to it…")
        for rel, target in targets.items():
            out_files[rel] = _copy_hash(target, out_dir / rel) if target.is_file() else None
        log_fn(f"Putting {label}'s {len(incoming)} file(s) in place…")
        for rel in managed:
            target, sha = targets[rel], want.get(rel)
            if sha is not None:
                stage = target.with_name(target.name + _STAGE_SUFFIX)
                _copy_hash(slot_dir / rel, stage)
                os.replace(stage, target)
            elif target.is_file():
                target.unlink()
            done.append(rel)
    except Exception as exc:
        log_fn(f"Switch failed ({exc}); putting {cur}'s files back…")
        for rel in done:
            target, src = targets[rel], out_dir / rel
            if out_files.get(rel) is not None and src.is_file():
                shutil.copy2(src, target)
            elif target.exists():
                target.unlink()          # a file only the target build has
        for target in targets.values():
            target.with_name(target.name + _STAGE_SUFFIX).unlink(missing_ok=True)
        shutil.rmtree(out_dir, ignore_errors=True)
        if isinstance(exc, VersionSwitchError):
            raise
        raise VersionSwitchError(f"Couldn't switch to {label}: {exc}") from exc

    slots[cur] = {"dir": str(out_dir), "files": out_files,
                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    slots.pop(label, None)
    shutil.rmtree(slot_dir, ignore_errors=True)
    index.update(slots=slots, managed=managed, current=label)
    index.setdefault("base", "")
    _write_index(state_dir, index)
    _sync_state(state_dir, index)
    log_fn(f"Switched to {label} from Mosaic's own copy — nothing was downloaded.")
    return label


# ---------------------------------------------------------------------------
# One list of builds to pick from, and one way to pick
# ---------------------------------------------------------------------------

@dataclass
class SwitchOption:
    """A build the user can pick, and how switching to it would happen."""
    label: str
    state: str                             # see below
    title: str = ""
    name: str = ""                         # what to call it; see build_names
    entry: "VersionEntry | None" = None
    depot_dirs: "dict[str, Path]" = field(default_factory=dict)
    download: "list[tuple[str, str]]" = field(default_factory=list)
    size_bytes: int = 0

    CURRENT = "current"                    # it's in the game folder now
    STORED = "stored"                      # Mosaic keeps it: an instant swap
    CACHED = "cached"                      # Steam's depot folders still hold it
    NEEDS_DOWNLOAD = "needs_download"

    @property
    def instant(self) -> bool:
        return self.state in (self.STORED, self.CACHED)

    @property
    def display(self) -> str:
        return self.name or self.label


def _short_version(version: str) -> str:
    """1.7.104.0 -> 1.7.104, the way these builds are actually named. A
    non-zero fourth part is kept: it's telling something apart."""
    parts = version.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 and parts[3] == "0" else version


def _exe_version_at(root, exe_file: str) -> str:
    if not exe_file or not root:
        return ""
    from Utils.wizard_support.pe_version import format_version, read_file_version
    version = read_file_version(_resolve_ci(Path(root), exe_file))
    return _short_version(format_version(version)) if version else ""


def _installed_build_version(game_root, state_dir, exe_file: str) -> str:
    """The version of the build sitting in the game folder.

    Normally its exe says so. But Skyrim's runtime swap patches the exe (and a
    few masters) back to an older version on purpose, leaving them on the newer
    build's data files -- LoreRim wants exactly that, 1.7.104 game files under a
    1.6.1170 exe. While that is in place the exe reports the swap, not the
    build, so Mosaic's own record of what it patched away from is used instead.
    A game with no such record (every game but Skyrim) just reads the exe.
    """
    try:
        from Utils.modding_tools.skyrim_runtime import describe
        info = describe(game_root, state_dir, game_running=False)
    except Exception:
        info = None
    if info is not None and info.swapped and info.recorded_from is not None:
        return _short_version(".".join(str(part) for part in info.recorded_from))
    return _exe_version_at(game_root, exe_file)


def build_names(game_root, state_dir, exe_file: str,
                index: "dict | None" = None) -> "dict[str, str]":
    """``{label: what to call it}`` for the installed build and every stored one.

    A build Steam installed is identified in the appmanifest only by its build
    id, which is not what anyone calls a version -- Skyrim SE's build 24914197
    is "1.7.104", and only its exe says so. A build that isn't installed right
    now has its exe in its store slot, so it can be read there.

    Labels stay the identity used everywhere else; this is presentation only.
    """
    if index is None:
        index = read_index(state_dir)
    names: "dict[str, str]" = {}
    current = index.get("current") or ""
    if current:
        names[current] = _installed_build_version(game_root, state_dir, exe_file) or current
    for label, slot in (index.get("slots") or {}).items():
        names[label] = _exe_version_at(slot.get("dir") or "", exe_file) or label
    # Two builds shipping one exe version would read as duplicates in a menu;
    # their labels at least tell them apart.
    by_name: "dict[str, list[str]]" = {}
    for label, name in names.items():
        by_name.setdefault(name, []).append(label)
    for labels in by_name.values():
        if len(labels) > 1:
            names.update({label: label for label in labels})
    return names


def switch_options(game_root, app_id: str, state_dir, table: VersionTable, *,
                   exe_file: str = "", installed: "dict[str, str] | None" = None,
                   log_text: "str | None" = None,
                   roots: "list[Path] | None" = None) -> "list[SwitchOption]":
    """Every build the user can pick for this game, current one first.

    "What am I running" comes from Mosaic's index, never from Steam's
    appmanifest: Mosaic doesn't edit the appmanifest, so Steam still reports
    the build it installed while a switched build is on disk.
    """
    index = read_index(state_dir)
    slots = index.get("slots") or {}
    exe_file = exe_file or table.exe_file
    names = build_names(game_root, state_dir, exe_file, index)
    base = capture_current(game_root, app_id, exe_file)
    base_label = index.get("base") or (base.label if base else "")
    cur = index.get("current") or base_label
    if installed is None:
        installed = read_installed_depots(game_root, app_id)
    if log_text is None:
        log_text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                             for p in content_log_paths(roots))

    ordered = [(base_label, base.title if base else "", None)] if base_label else []
    ordered += [(e.label, e.title, e) for e in table.versions]
    ordered += [(lbl, "", None) for lbl in stored_labels(state_dir)]
    out, seen = [], set()
    for label, title, entry in ordered:
        if not label or label in seen:
            continue
        seen.add(label)
        name = names.get(label, label)
        if label == cur:
            out.append(SwitchOption(label, SwitchOption.CURRENT, title, name))
        elif label in slots:
            out.append(SwitchOption(label, SwitchOption.STORED, title, name,
                                    size_bytes=slot_bytes(slots[label])))
        elif entry is None:
            out.append(SwitchOption(label, SwitchOption.NEEDS_DOWNLOAD, title, name))
        else:
            plan = plan_switch(entry, installed)
            dirs, missing = {}, []
            for depot, manifest in plan.download:
                st = depot_status(app_id, depot, manifest, state_dir=state_dir,
                                  log_text=log_text, roots=roots)
                if st.state == "downloaded" and st.folder is not None:
                    dirs[depot] = st.folder
                else:
                    missing.append((depot, manifest))
            ready = bool(plan.download) and not missing
            out.append(SwitchOption(
                label, SwitchOption.CACHED if ready else SwitchOption.NEEDS_DOWNLOAD,
                title, name, entry, dirs, missing or plan.download))
    out.sort(key=lambda o: o.state != SwitchOption.CURRENT)
    return out


def switch_to(game_root, state_dir, option: SwitchOption, *, exe_file: str = "",
              game_running: bool = False, deploy_active: bool = False,
              log_fn=lambda _m: None) -> str:
    """Switch to *option*'s build by whichever route it needs: a local swap
    from the store, or applying what Steam has already downloaded. Applying
    runs on the build Steam installed, so a switched build is put back first
    -- which leaves it in the store, ready to swap back to."""
    index = read_index(state_dir)
    cur, base = index.get("current") or "", index.get("base") or ""
    if option.state == SwitchOption.CURRENT:
        return option.label
    if option.state == SwitchOption.STORED:
        return swap_to(game_root, state_dir, option.label, game_running=game_running,
                       deploy_active=deploy_active, log_fn=log_fn)
    if option.state != SwitchOption.CACHED or option.entry is None or not option.depot_dirs:
        raise VersionSwitchError(
            f"{option.label} isn't downloaded yet. Use the Game version wizard to get it "
            "from Steam first.")
    if cur and base and cur != base:
        log_fn(f"Putting {base} back first, and keeping {cur}…")
        swap_to(game_root, state_dir, base, game_running=game_running,
                deploy_active=deploy_active, log_fn=log_fn)
    apply_version(game_root, option.entry, option.depot_dirs, state_dir,
                  from_label=base or cur or "previous", exe_file=exe_file,
                  game_running=game_running, deploy_active=deploy_active, log_fn=log_fn)
    return option.label

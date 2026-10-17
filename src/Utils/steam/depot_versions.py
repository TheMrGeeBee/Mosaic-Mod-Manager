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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .acf import get_ci, read_vdf

VERSIONS_DIR = Path(__file__).with_name("versions")
STATE_FILENAME = "version_state.json"
BACKUP_DIRNAME = "version_backup"
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


def _load_records(state_dir) -> dict:
    try:
        return json.loads((Path(state_dir) / DOWNLOADS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_records(state_dir, records: dict) -> None:
    p = Path(state_dir) / DOWNLOADS_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def mark_clean_start(state_dir, folder: Path, when: "str | None" = None) -> None:
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
    p = state_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


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
        hit = next((n for n in dirnames + filenames if os.path.islink(os.path.join(dirpath, n))), None)
        if hit is not None:
            rel = os.path.relpath(os.path.join(dirpath, hit), root)
            traces.append(f"mod files are linked into the game folder (e.g. {rel})")
            break
    return traces


def _check_can_change(game_root: Path, game_running: bool, deploy_active: bool = False) -> None:
    if game_running:
        raise VersionSwitchError("The game is running. Close it first.")
    traces = deployment_traces(game_root, deploy_active=deploy_active)
    if traces:
        raise VersionSwitchError("Mods are deployed (" + "; ".join(traces) + "). Restore the "
                                 "game first, so only the game's own files are in its folder.")


def apply_version(game_root, entry: VersionEntry, depot_dirs: "dict[str, Path]", state_dir, *,
                  from_label: str, exe_file: str = "", game_running: bool = False,
                  deploy_active: bool = False, log_fn=lambda _m: None) -> dict:
    """Put the downloaded depot files into the game folder. Every file that
    gets replaced (and every file in ``entry.remove``) is first copied to the
    backup with its sha256; new files are staged next to their targets and
    swapped in with ``os.replace``. Any failure puts everything back. Returns
    the saved state."""
    game_root = Path(game_root)
    _check_can_change(game_root, game_running, deploy_active)
    if load_state(state_dir) is not None:
        raise VersionSwitchError("The game is already switched to another version. "
                                 "Switch back first.")
    files = _depot_files(depot_dirs)
    if not files:
        raise VersionSwitchError("The downloaded depots are empty.")
    backup = Path(state_dir) / BACKUP_DIRNAME / re.sub(r"[^\w.\-]+", "_", from_label or "previous")

    targets = {rel: _resolve_ci(game_root, rel) for rel in files}
    removals = {rel: _resolve_ci(game_root, rel) for rel in entry.remove}
    removals = {rel: p for rel, p in removals.items() if p.is_file() and rel not in files}
    backup_bytes = sum(p.stat().st_size for p in list(targets.values()) + list(removals.values())
                       if p.is_file())
    stage_bytes = sum(p.stat().st_size for p in files.values())
    same_fs = os.stat(_existing(backup)).st_dev == os.stat(game_root).st_dev
    need_game = stage_bytes + (backup_bytes if same_fs else 0)
    if _free_bytes(game_root) < need_game * 1.05:
        raise VersionSwitchError(f"Not enough free space: about {need_game / 1e9:.1f} GB is needed "
                                 "next to the game for the backup and the new files.")
    if not same_fs and _free_bytes(backup) < backup_bytes * 1.05:
        raise VersionSwitchError(f"Not enough free space for the {backup_bytes / 1e9:.1f} GB backup.")

    record: "dict[str, dict]" = {}
    staged: "dict[str, Path]" = {}
    swapped: "list[str]" = []
    removed: "list[str]" = []
    try:
        shutil.rmtree(backup, ignore_errors=True)
        log_fn(f"Backing up {sum(1 for p in targets.values() if p.is_file()) + len(removals)} "
               f"file(s) ({backup_bytes / 1e9:.1f} GB)…")
        for rel, target in list(targets.items()) + list(removals.items()):
            if target.is_file():
                record.setdefault(rel, {})["original_sha256"] = _copy_hash(target, backup / rel)
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
    log_fn(f"Switched to {entry.label}: {len(swapped)} file(s) replaced or added, "
           f"{len(removed)} removed.")
    return state


def _existing(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def revert_version(game_root, state_dir, *, game_running: bool = False,
                   deploy_active: bool = False, log_fn=lambda _m: None) -> str:
    """Put back the files the last switch replaced or removed, and delete the
    ones it added. Refuses (changing nothing) if the backup is damaged.
    Returns the label switched back to."""
    game_root = Path(game_root)
    _check_can_change(game_root, game_running, deploy_active)
    state = load_state(state_dir)
    if state is None:
        raise VersionSwitchError("There's no version switch to undo.")
    backup = Path(state["backup_dir"])
    files = state.get("files") or {}
    log_fn("Checking the backup…")
    for rel, info in files.items():
        if "original_sha256" in info:
            src = backup / rel
            if not src.is_file() or _sha256(src) != info["original_sha256"]:
                raise VersionSwitchError(
                    f"The backup of {rel} is missing or damaged, so switching back could leave "
                    "a broken game. Use Steam's \"Verify integrity of game files\" instead.")
    for rel, info in files.items():
        dst = _resolve_ci(game_root, rel)
        if "original_sha256" in info:
            stage = dst.with_name(dst.name + _STAGE_SUFFIX)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / rel, stage)
            os.replace(stage, dst)
        elif dst.is_file():
            dst.unlink()
    shutil.rmtree(backup, ignore_errors=True)
    state_path(state_dir).unlink(missing_ok=True)
    log_fn(f"Switched back to {state.get('from') or 'the previous version'}.")
    return state.get("from") or ""

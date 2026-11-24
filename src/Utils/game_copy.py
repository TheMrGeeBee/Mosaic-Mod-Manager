"""Game copies: a profile's own copy of the game files ("Stock Game").

MO2 + Wabbajack run a modlist from a ``Stock Game/`` folder next to it instead
of the Steam install, so the list's game version can never be changed under it
by Steam or by another list. Mosaic does the same through the per-profile
``game_path`` override; Steam-only code keeps using the Steam install (see
``BaseGame.get_steam_install_path``).

A copy is made from the Steam install, at the build Steam has installed or at
any build Mosaic keeps in its build store (``Utils.steam.depot_versions``):

1. Clone the Steam install. A reflink clone (btrfs, XFS, bcachefs) is instant
   and shares the data until a file changes, so it costs no space. Without
   reflinks the caller must agree to a real copy first.
2. For a stored build, lay that build's files over the clone, or delete those
   it doesn't have, exactly as a build switch would (each checked against the
   store's recorded hash).
3. Write ``.mosaic-game-copy.json`` and move the finished folder into place.

Never hardlinks: a Steam update or verify that rewrites a file in place would
change the copy, and a change in the copy would change Steam's file.

Pure logic, no Qt. Nothing in the Steam install is ever written.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

MARKER_NAME = ".mosaic-game-copy.json"
COPY_DIRNAME = "Stock Game"                 # MO2 / Wabbajack's name, next to the modlist
_PARTIAL_SUFFIX = ".mosaic-partial"
_FICLONE = 0x40049409                       # linux/fs.h: _IOW(0x94, 9, int)
_NO_REFLINK = {errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EINVAL,
               errno.ENOTTY, errno.ENOSYS}
_CHUNK = 1 << 20

Progress = Callable[[int, int], None]       # (bytes done, bytes total)


class GameCopyError(RuntimeError):
    """A copy can't be made, used or deleted; the message is for the user."""


class ReflinkUnavailable(GameCopyError):
    """The destination can't share data with the Steam install. ``size`` is
    what a real copy would take, so the user can decide."""

    def __init__(self, size: int):
        super().__init__(
            f"This folder can't share files with the Steam install (it's on another drive, "
            f"or the filesystem has no reflinks), so the copy would take about "
            f"{size / 1e9:.1f} GB.")
        self.size = size


@dataclass
class CopyInfo:
    """What ``.mosaic-game-copy.json`` records about a copy."""
    game: str
    build: str                  # the store label it was made at
    version: str                # the exe's version, e.g. "1.6.1170"
    source: str                 # the Steam install it was cloned from
    method: str                 # "reflink" or "copy"
    created_at: str


# ---- reading -------------------------------------------------------------------

def read_marker(folder: "str | Path | None") -> CopyInfo | None:
    """The copy's record, or None when *folder* isn't a copy Mosaic made."""
    if not folder:
        return None
    try:
        data = json.loads((Path(folder) / MARKER_NAME).read_text(encoding="utf-8"))
        return CopyInfo(**{k: str(data.get(k, "")) for k in CopyInfo.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def profiles_using(profiles_dir: "str | Path", folder: "str | Path") -> "list[str]":
    """Names of the profiles whose game folder override is *folder*."""
    from Utils.profile.profile_state import read_profile_settings
    target = _norm(folder)
    names = []
    try:
        entries = sorted(p for p in Path(profiles_dir).iterdir() if p.is_dir())
    except OSError:
        return []
    for prof in entries:
        try:
            gp = read_profile_settings(prof).get("game_path")
        except Exception:
            continue
        if isinstance(gp, str) and gp and _norm(gp) == target:
            names.append(prof.name)
    return names


def default_destination(parent: "str | Path", modlist_name: str) -> Path:
    """``<parent>/<modlist name>/Stock Game``."""
    safe = "".join("_" if c in '/\\:*?"<>|' else c for c in modlist_name).strip() or "Modlist"
    return Path(parent) / safe / COPY_DIRNAME


@dataclass
class BuildChoices:
    """The builds a copy can be made at, without downloading anything."""
    current: str                            # the build in the Steam install now
    labels: "list[str]"                     # current first, then stored builds
    names: "dict[str, str]"                 # label -> what to call it ("1.6.1170")
    exe_file: str
    state_dir: Path


def build_choices(game) -> BuildChoices:
    """The Steam install's build plus every build in Mosaic's build store."""
    from Utils.config_paths import get_game_config_dir
    from Utils.steam import depot_versions as dv
    from Utils.wine_proton.steam_finder import game_steam_id
    steam_root = game.get_steam_install_path()
    app_id = str(game_steam_id(game) or getattr(game, "steam_id", "") or "")
    state_dir = get_game_config_dir(game.name)
    table = dv.load_table(app_id, state_dir / dv.USER_VERSIONS_FILENAME) if app_id else None
    exe_file = (table.exe_file if table else "") or getattr(game, "exe_name", "") or ""
    index = dv.read_index(state_dir)
    base = dv.capture_current(steam_root, app_id, "") if app_id and steam_root else None
    current = index.get("current") or index.get("base") or (base.label if base else "") \
        or "installed"
    labels = [current] + [lbl for lbl in dv.stored_labels(state_dir) if lbl != current]
    try:
        names = dv.build_names(steam_root, state_dir, exe_file, index, current)
    except Exception:
        names = {}
    return BuildChoices(current=current, labels=labels,
                        names={lbl: names.get(lbl) or lbl for lbl in labels},
                        exe_file=exe_file, state_dir=state_dir)


# ---- remembering where copies go ----------------------------------------------------

def _prefs_file(game_name: str) -> Path:
    from Utils.config_paths import get_game_config_dir
    return get_game_config_dir(game_name) / "game_copies.json"


def last_parent(game_name: str) -> Path | None:
    """The folder the last copy's modlist folder was made in (e.g. ~/games/Modlists)."""
    try:
        raw = json.loads(_prefs_file(game_name).read_text(encoding="utf-8")).get("last_parent")
        return Path(raw) if raw else None
    except (OSError, ValueError, AttributeError):
        return None


def remember_parent(game_name: str, parent: "str | Path") -> None:
    try:
        _prefs_file(game_name).write_text(json.dumps({"last_parent": str(parent)}, indent=2),
                                          encoding="utf-8")
    except OSError:
        pass


# ---- checks ---------------------------------------------------------------------

def check_destination(dest: "str | Path", steam_root: "str | Path") -> None:
    """Raise GameCopyError unless *dest* is a sensible new copy location."""
    from Utils.wine_proton.steam_finder import library_steamapps_for
    dest, steam_root = Path(dest), Path(steam_root)
    if library_steamapps_for(dest / "x") is not None:
        raise GameCopyError(
            "A game copy can't go inside a Steam library's steamapps/common folder: Mosaic "
            "would take it for a Steam install and Steam might touch it. Pick a folder next "
            "to your modlists instead.")
    d, s = _norm(dest), _norm(steam_root)
    if d == s or d.is_relative_to(s) or s.is_relative_to(d):
        raise GameCopyError("The copy can't be inside the Steam install, or contain it.")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise GameCopyError(f"{dest} already exists and isn't empty.")


def source_problems(steam_root: "str | Path", *, deploy_active: bool) -> "list[str]":
    """Why the Steam install can't be copied right now (empty when it can).
    Copying while mods are deployed would copy the mods too."""
    from Utils.steam.depot_versions import deployment_traces
    root = Path(steam_root)
    if not root.is_dir():
        return [f"{root} doesn't exist"]
    return deployment_traces(root, deploy_active=deploy_active)


def tree_bytes(root: "str | Path") -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def reflink_supported(src_dir: "str | Path", dest_parent: "str | Path") -> bool:
    """Can files from *src_dir* be reflinked into *dest_parent*? Tries one file."""
    src_dir, dest_parent = Path(src_dir), Path(dest_parent)
    sample = next((p for p in sorted(src_dir.iterdir()) if p.is_file() and not p.is_symlink()),
                  None)
    if sample is None:
        return False
    dest_parent.mkdir(parents=True, exist_ok=True)
    probe = dest_parent / f".mosaic-reflink-probe-{os.getpid()}"
    try:
        return _clone_file(sample, probe, allow_copy=False) == "reflink"
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


# ---- making a copy ---------------------------------------------------------------

def create_copy(*, game_name: str, steam_root: "str | Path", dest: "str | Path",
                build: str, current_build: str, state_dir: "str | Path",
                exe_file: str, allow_full_copy: bool = False,
                deploy_active: bool = False,
                progress: Progress = lambda _d, _t: None,
                log_fn: Callable[[str], None] = lambda _m: None) -> CopyInfo:
    """Make a copy of the Steam install at *build* in *dest*. *current_build* is
    the label of the build in the Steam install now; any other *build* must be
    in the build store under *state_dir*. Raises GameCopyError (or
    ReflinkUnavailable when a real copy would be needed and isn't allowed).
    On failure nothing is left behind."""
    steam_root, dest = Path(steam_root), Path(dest)
    check_destination(dest, steam_root)
    problems = source_problems(steam_root, deploy_active=deploy_active)
    if problems:
        raise GameCopyError("Restore the game first: the Steam install has mod files in it ("
                            + "; ".join(problems) + ").")
    slot = None
    if build != current_build:
        from Utils.steam import depot_versions as dv
        slot = (dv.read_index(state_dir).get("slots") or {}).get(build)
        if slot is None:
            raise GameCopyError(f"Mosaic doesn't keep {build}'s files. Switch the Steam install "
                                "to it once with the Game version wizard, or pick another build.")
    reflink = reflink_supported(steam_root, dest.parent)
    total = tree_bytes(steam_root)
    if not reflink and not allow_full_copy:
        raise ReflinkUnavailable(total)
    from Utils.path_utils import free_bytes
    if not reflink and free_bytes(dest.parent) < total * 1.05:
        raise GameCopyError(f"Not enough free space: the copy needs about {total / 1e9:.1f} GB.")

    work = dest.with_name(dest.name + _PARTIAL_SUFFIX)
    shutil.rmtree(work, ignore_errors=True)            # a stale, interrupted attempt
    try:
        log_fn(f"Game copy: {'cloning' if reflink else 'copying'} {steam_root} "
               f"({total / 1e9:.1f} GB{', shared, so no extra space' if reflink else ''}) …")
        method = _clone_tree(steam_root, work, allow_copy=allow_full_copy,
                             progress=progress, total=total)
        if slot is not None:
            _apply_slot(work, slot, build, log_fn)
        version = _exe_version(work, exe_file)
        info = CopyInfo(game=game_name, build=build, version=version, source=str(steam_root),
                        method=method, created_at=datetime.now(timezone.utc)
                        .isoformat(timespec="seconds"))
        (work / MARKER_NAME).write_text(json.dumps(asdict(info), indent=2), encoding="utf-8")
        if dest.exists():
            dest.rmdir()                               # checked empty above
        os.replace(work, dest)
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        if isinstance(exc, GameCopyError):
            raise
        raise GameCopyError(f"Couldn't make the game copy: {exc}") from exc
    log_fn(f"Game copy: ready at {dest} ({info.version or build}, {method}).")
    return info


def delete_copy(folder: "str | Path", profiles_dir: "str | Path") -> None:
    """Delete a copy Mosaic made, once no profile uses it and nothing is deployed in it."""
    folder = Path(folder)
    if read_marker(folder) is None:
        raise GameCopyError(f"{folder} isn't a game copy Mosaic made, so it won't delete it.")
    users = profiles_using(profiles_dir, folder)
    if users:
        raise GameCopyError("Still used by: " + ", ".join(users)
                            + ". Point those profiles elsewhere first.")
    from Utils.steam.depot_versions import deployment_traces
    if deployment_traces(folder):
        raise GameCopyError("Mods are deployed in this copy. Restore first.")
    shutil.rmtree(folder)


# ---- internals ------------------------------------------------------------------

def _norm(p: "str | Path") -> Path:
    try:
        return Path(p).expanduser().resolve()
    except OSError:
        return Path(os.path.abspath(Path(p).expanduser()))


def _clone_file(src: Path, dst: Path, *, allow_copy: bool) -> str:
    """Reflink *src* to *dst* (falling back to a real copy when allowed).
    Returns "reflink" or "copy". Raises OSError otherwise."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        try:
            fcntl.ioctl(fo.fileno(), _FICLONE, fi.fileno())
            how = "reflink"
        except OSError as exc:
            if exc.errno not in _NO_REFLINK or not allow_copy:
                raise
            shutil.copyfileobj(fi, fo, _CHUNK)
            how = "copy"
    shutil.copystat(src, dst)
    return how


def _clone_tree(src: Path, dst: Path, *, allow_copy: bool, progress: Progress,
                total: int) -> str:
    """Copy the tree; symlinks stay symlinks. Returns "reflink" unless any file
    had to be copied for real."""
    done, method = 0, "reflink"
    for dirpath, dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        out = dst / rel
        out.mkdir(parents=True, exist_ok=True)
        for name in dirnames + filenames:
            s = Path(dirpath) / name
            if s.is_symlink():
                (out / name).symlink_to(os.readlink(s))
        for name in filenames:
            s = Path(dirpath) / name
            if s.is_symlink():
                continue
            if _clone_file(s, out / name, allow_copy=allow_copy) == "copy":
                method = "copy"
            done += s.stat().st_size
            progress(done, total)
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
    return method


def _apply_slot(root: Path, slot: dict, build: str, log_fn) -> None:
    """Make *root* hold the stored *build*: each managed path becomes the slot's
    file, or is deleted where that build has none."""
    from Utils.steam import depot_versions as dv
    slot_dir = Path(slot.get("dir") or "")
    files = slot.get("files") or {}
    log_fn(f"Game copy: putting {build}'s {sum(1 for s in files.values() if s)} file(s) in place …")
    for rel, sha in files.items():
        target = dv._resolve_ci(root, rel)
        if sha is None:
            if target.is_file() or target.is_symlink():
                target.unlink()
            continue
        src = slot_dir / rel
        if not src.is_file() or dv._sha256(src) != sha:
            raise GameCopyError(f"Mosaic's stored copy of {rel} for {build} is missing or "
                                "damaged. Pick another build, or switch the Steam install to "
                                "it with the Game version wizard first.")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        _clone_file(src, target, allow_copy=True)


def _exe_version(root: Path, exe_file: str) -> str:
    if not exe_file:
        return ""
    try:
        from Utils.steam.depot_versions import _exe_version_at
        return _exe_version_at(root, exe_file)
    except Exception:
        return ""

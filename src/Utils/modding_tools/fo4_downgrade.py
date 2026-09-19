"""
fo4_downgrade.py
Downgrade Fallout 4 from the Anniversary Edition (1.11.240) to Old-Gen
(1.10.163) by applying the community xdelta patches to the three root files
that differ: Fallout4.exe, Fallout4Launcher.exe and steam_api64.dll.

Pure logic — no Qt, no Wine. These are files Steam owns, so it is
transactional: every original is backed up first, every patch is applied to a
temp file and verified, and only then are the files swapped in. Any failure
leaves the game folder exactly as it was (or restores it), and a recorded
downgrade can be reverted from the backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from Utils.wizard_support.pe_version import Version, format_version, read_file_version

FROM_VERSION: Version = (1, 11, 240, 0)     # Anniversary Edition
TO_VERSION: Version = (1, 10, 163, 0)       # Old-Gen

# The root files the AE→OG patch replaces. Fallout4.exe's version identifies
# the game build. Data/ is left alone (Old-Gen mods run on AE data with the
# Backported Archive2 Support plugin).
TARGETS = ("Fallout4.exe", "Fallout4Launcher.exe", "steam_api64.dll")
VERSION_TARGET = "Fallout4.exe"

PATCH_SUFFIXES = frozenset({".xdelta", ".vcdiff", ".delta", ".patch"})
# Where the patch comes from, for messages shown to the user.
PATCH_SOURCE = ("Nexus mod 98059, \u201cAnniversaryEdition(1.11.240) to "
                "LastGen(1.10.163) downgrade patch\u201d")
STATE_FILENAME = "downgrade_state.json"
BACKUP_DIRNAME = "downgrade_backup"

# Mosaic swaps Fallout4Launcher.exe for the script extender's loader on deploy
# (game.script_extender_swap), parking the real one here. While it exists the
# launcher in the game folder is NOT Steam's, so the game must be restored first.
_LAUNCHER_BACKUP = "Fallout4Launcher.bak"

_INSTALL_XDELTA3 = ("xdelta3 was not found. Install it (Arch: 'sudo pacman -S xdelta3', "
                    "Debian/Ubuntu: 'sudo apt install xdelta3', Fedora: 'sudo dnf install xdelta') "
                    "and try again.")


class DowngradeError(RuntimeError):
    """A downgrade/revert step failed; the message is fit to show the user."""


def _noop(_msg: str) -> None:
    pass


# ---- discovery ---------------------------------------------------------------

def find_xdelta3() -> str | None:
    return shutil.which("xdelta3")


def read_game_version(game_root: "str | Path") -> Version | None:
    return read_file_version(Path(game_root) / VERSION_TARGET)


# BA2 versions the Old-Gen exe can read by itself (only v1). Steam's Anniversary
# Edition / Next-Gen data ships most of its archives as v7/v8, which the Old-Gen
# exe cannot open without the Backported Archive2 Support System plugin.
OLDGEN_BA2_VERSIONS = frozenset({1})

# The two mods an Old-Gen install on Anniversary/Next-Gen data cannot run
# without (Nexus mod ids, checked against a working install's meta.ini).
BACKPORTED_BA2_MOD = ("Backported Archive2 Support System", 81859)
ADDRESS_LIBRARY_MOD = ("Address Library for F4SE Plugins", 47327)


def count_newer_format_archives(game_root: "str | Path") -> int:
    """How many ``Data/*.ba2`` archives the Old-Gen exe cannot read on its own.

    Reads only each archive's 8-byte header (``BTDX`` + version). Anything that
    is not a BA2, or cannot be read, is not counted. On an unmodified Steam
    install this is most of the game's own archives, which is why the game
    shows a black screen and exits after a downgrade until the Backported
    Archive2 Support plugin is installed.
    """
    data = Path(game_root) / "Data"
    count = 0
    try:
        entries = list(data.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.suffix.lower() != ".ba2":
            continue
        try:
            with open(entry, "rb") as fh:
                header = fh.read(8)
        except OSError:
            continue
        if len(header) == 8 and header[:4] == b"BTDX":
            version = int.from_bytes(header[4:8], "little")
            if version not in OLDGEN_BA2_VERSIONS:
                count += 1
    return count


def launcher_swapped(game_root: "str | Path") -> bool:
    """True while Mosaic's deploy has swapped in the script extender launcher."""
    return (Path(game_root) / _LAUNCHER_BACKUP).exists()


def backup_dir(state_dir: "str | Path") -> Path:
    return Path(state_dir) / BACKUP_DIRNAME


def load_state(state_dir: "str | Path") -> dict | None:
    try:
        data = json.loads((Path(state_dir) / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class Assessment:
    """What the wizard's Check page shows about the current install."""
    version: Version | None
    build: str                      # "anniversary" | "oldgen" | "unsupported"
    downgraded: bool                # a Mosaic-recorded downgrade is still in place
    blockers: list[str]             # why a downgrade can't run right now
    revert_blockers: list[str]      # why a revert can't run right now

    @property
    def can_downgrade(self) -> bool:
        return self.build == "anniversary" and not self.blockers

    @property
    def can_revert(self) -> bool:
        return self.downgraded and not self.revert_blockers


def assess(
    game_root: "str | Path",
    state_dir: "str | Path",
    *,
    xdelta3: str | None,
    game_running: bool,
) -> Assessment:
    """Inspect the install: which build it is, whether a Mosaic downgrade is in
    place, and what (if anything) currently blocks a downgrade or a revert.

    A recorded downgrade only counts while Fallout4.exe still reads as
    Old-Gen — if Steam has since re-downloaded the Anniversary exe, the record
    is stale and the game is simply patchable again.
    """
    game_root = Path(game_root)
    version = read_game_version(game_root)
    if version == FROM_VERSION:
        build = "anniversary"
    elif version == TO_VERSION:
        build = "oldgen"
    else:
        build = "unsupported"
    state = load_state(state_dir)
    downgraded = bool(state and state.get("downgraded")) and build == "oldgen"

    shared: list[str] = []
    if game_running:
        shared.append("Fallout 4 is running — close it first.")
    if launcher_swapped(game_root):
        shared.append("Mosaic's mods are deployed — restore the game first.")

    blockers = list(shared)
    missing = [t for t in TARGETS if not (game_root / t).is_file()]
    if missing:
        blockers.append(f"Missing from the game folder: {', '.join(missing)}.")
    if not xdelta3:
        blockers.append("xdelta3 isn't installed.")
    return Assessment(version, build, downgraded, blockers, shared)


def _squash(text: str) -> str:
    """Lowercase, letters and digits only. The real download drops the dot from
    ".exe" and the underscore from "steam_api64" ("Fallout4exe.xdelta",
    "SteamAPI64.xdelta"), so names are compared with all punctuation removed."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _target_keys(target: str) -> list[str]:
    """Squashed names a patch file may carry for *target*, most specific first
    ("fallout4exe" before "fallout4")."""
    path = Path(target)
    return sorted({_squash(path.name), _squash(path.stem)}, key=len, reverse=True)


def find_patches(root: "str | Path") -> dict[str, Path]:
    """Map each of :data:`TARGETS` to its patch file somewhere under *root*.

    Matched by file name, ignoring case and punctuation ("Fallout4exe.xdelta",
    "Fallout4.exe.xdelta", "Fallout4 (AE to LastGen).vcdiff" all identify
    Fallout4.exe). Each patch goes to the target with the longest matching name,
    so Fallout4Launcher's patch is never taken for Fallout4's. Raises
    :class:`DowngradeError` if any target has no patch or more than one.
    """
    root = Path(root)
    patch_files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in PATCH_SUFFIXES
    )
    if not patch_files:
        raise DowngradeError(
            "This archive doesn't contain any .xdelta patch files, so it isn't the "
            f"downgrade patch. You need the main file of {PATCH_SOURCE}.")

    found: dict[str, list[Path]] = {t: [] for t in TARGETS}
    for patch in patch_files:
        name = _squash(patch.name[:-len(patch.suffix)])
        best_len, best_target = 0, None
        for target in TARGETS:
            for key in _target_keys(target):
                if key in name:
                    if len(key) > best_len:
                        best_len, best_target = len(key), target
                    break                     # keys are longest-first: this is the target's best
        if best_target is not None:
            found[best_target].append(patch)

    listing = ", ".join(p.name for p in patch_files)
    ambiguous = [t for t, ps in found.items() if len(ps) > 1]
    if ambiguous:
        raise DowngradeError(
            f"Found more than one patch for {', '.join(ambiguous)} "
            f"(patch files: {listing}).")
    missing = [t for t, ps in found.items() if not ps]
    if missing:
        raise DowngradeError(
            f"Couldn't identify a patch for {', '.join(missing)} "
            f"(patch files found: {listing}).")
    return {t: ps[0] for t, ps in found.items()}


# ---- apply -------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fmt(version: Version | None) -> str:
    return format_version(version) if version else "an unreadable version"


def _write_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (STATE_FILENAME + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, state_dir / STATE_FILENAME)


def _restore_from_backup(game_root: Path, backup: Path, names: list[str]) -> None:
    """Put *names* back from *backup*; raises DowngradeError if any can't be."""
    failed: list[str] = []
    for name in names:
        tmp = game_root / f".{name}.mosaic-restore"
        try:
            shutil.copy2(backup / name, tmp)
            os.replace(tmp, game_root / name)
        except OSError:
            failed.append(name)
        finally:
            tmp.unlink(missing_ok=True)
    if failed:
        raise DowngradeError(
            f"Could not roll back {', '.join(failed)}. The original files are in "
            f"{backup} — copy them back into the game folder, or use Steam's "
            "'Verify integrity of game files'.")


def apply_downgrade(
    game_root: "str | Path",
    patches: dict[str, Path],
    state_dir: "str | Path",
    *,
    xdelta3: str | None,
    run: Callable = subprocess.run,
    log_fn: Callable[[str], None] = _noop,
) -> Version:
    """Patch the three root files to 1.10.163.0, transactionally.

    *patches* is the result of :func:`find_patches`; *state_dir* is where the
    backup and ``downgrade_state.json`` are kept. Returns the resulting version.
    """
    game_root, state_dir = Path(game_root), Path(state_dir)

    if not xdelta3:
        raise DowngradeError(_INSTALL_XDELTA3)
    missing = [t for t in TARGETS if not (game_root / t).is_file()]
    if missing:
        raise DowngradeError(f"Not found in the game folder: {', '.join(missing)}.")
    no_patch = [t for t in TARGETS if t not in patches]
    if no_patch:
        raise DowngradeError(f"No patch supplied for {', '.join(no_patch)}.")
    if launcher_swapped(game_root):
        raise DowngradeError(
            "Mosaic's mods are currently deployed (Fallout4Launcher.bak exists). "
            "Restore the game first, then run the downgrade.")
    current = read_game_version(game_root)
    if current == TO_VERSION:
        raise DowngradeError(f"Fallout 4 is already {format_version(TO_VERSION)}.")
    if current != FROM_VERSION:
        raise DowngradeError(
            f"Fallout4.exe is {_fmt(current)}; these patches only apply to "
            f"{format_version(FROM_VERSION)} (the current Steam version).")

    backup = backup_dir(state_dir)
    if backup.exists():
        shutil.rmtree(backup)       # stale, from an earlier attempt — the game is verified unmodified above
    backup.mkdir(parents=True)

    temps: dict[str, Path] = {}
    replaced: list[str] = []
    try:
        original_hashes: dict[str, str] = {}
        for target in TARGETS:
            shutil.copy2(game_root / target, backup / target)
            original_hashes[target] = _sha256(backup / target)
        log_fn(f"Downgrade: backed up {', '.join(TARGETS)} to {backup}")

        for target in TARGETS:
            tmp = game_root / f".{target}.mosaic-downgrade"
            temps[target] = tmp
            log_fn(f"Downgrade: patching {target} …")
            proc = run(
                [xdelta3, "-d", "-f", "-s", str(game_root / target),
                 str(patches[target]), str(tmp)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                lines = (proc.stderr or proc.stdout or "").strip().splitlines()
                # xdelta3 puts the real error on one line and a hint on the next.
                detail = " ".join(lines[-2:]) if lines else f"exit {proc.returncode}"
                raise DowngradeError(
                    f"xdelta3 could not patch {target}: {detail}. The patch doesn't "
                    "match the installed file.")
            shutil.copymode(game_root / target, tmp)

        patched = read_file_version(temps[VERSION_TARGET])
        if patched != TO_VERSION:
            raise DowngradeError(
                f"The patched {VERSION_TARGET} reports {_fmt(patched)}, not "
                f"{format_version(TO_VERSION)} — this patch doesn't match the installed game.")
        patched_hashes = {t: _sha256(temps[t]) for t in TARGETS}

        for target in TARGETS:
            try:
                os.replace(temps[target], game_root / target)
            except OSError as exc:
                _restore_from_backup(game_root, backup, replaced)
                raise DowngradeError(
                    f"Could not replace {target} ({exc}); the game files were rolled back."
                ) from exc
            replaced.append(target)
    except DowngradeError:
        shutil.rmtree(backup, ignore_errors=True)
        raise
    finally:
        for tmp in temps.values():
            tmp.unlink(missing_ok=True)

    state = {
        "downgraded": True,
        "from": format_version(FROM_VERSION),
        "to": format_version(TO_VERSION),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backup_dir": str(backup),
        "files": {
            t: {"original_sha256": original_hashes[t], "patched_sha256": patched_hashes[t]}
            for t in TARGETS
        },
    }
    try:
        _write_state(state_dir, state)
    except OSError as exc:
        log_fn(f"Downgrade: warning — couldn't record the downgrade state ({exc}); "
               f"the originals are backed up in {backup}.")
    log_fn(f"Downgrade: Fallout 4 is now {format_version(TO_VERSION)}.")
    return TO_VERSION


# ---- revert ------------------------------------------------------------------

def revert_downgrade(
    game_root: "str | Path",
    state_dir: "str | Path",
    *,
    log_fn: Callable[[str], None] = _noop,
) -> None:
    """Restore the backed-up Anniversary Edition files and clear the state."""
    game_root, state_dir = Path(game_root), Path(state_dir)
    state = load_state(state_dir)
    if not state or not state.get("downgraded"):
        raise DowngradeError("Nothing to revert — no Mosaic downgrade is recorded for this game.")
    if launcher_swapped(game_root):
        raise DowngradeError(
            "Mosaic's mods are currently deployed (Fallout4Launcher.bak exists). "
            "Restore the game first, then revert the downgrade.")

    backup = backup_dir(state_dir)
    files = state.get("files") or {}
    for target in TARGETS:
        wanted = (files.get(target) or {}).get("original_sha256")
        saved = backup / target
        if not saved.is_file() or not wanted or _sha256(saved) != wanted:
            raise DowngradeError(
                f"The backup of {target} is missing or has been modified, so it won't "
                "be restored. Use Steam's 'Verify integrity of game files' instead.")

    temps: list[Path] = []
    try:
        for target in TARGETS:
            tmp = game_root / f".{target}.mosaic-revert"
            temps.append(tmp)
            shutil.copy2(backup / target, tmp)
        for target, tmp in zip(TARGETS, temps):
            os.replace(tmp, game_root / target)
    except OSError as exc:
        raise DowngradeError(f"Could not restore the original files: {exc}") from exc
    finally:
        for tmp in temps:
            tmp.unlink(missing_ok=True)

    (state_dir / STATE_FILENAME).unlink(missing_ok=True)
    shutil.rmtree(backup, ignore_errors=True)
    log_fn(f"Downgrade: reverted to {_fmt(read_game_version(game_root))}.")

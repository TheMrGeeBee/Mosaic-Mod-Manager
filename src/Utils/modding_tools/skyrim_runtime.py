"""
skyrim_runtime.py
Move Skyrim Special Edition between game runtimes (e.g. the 1.7.104 Steam serves
today <-> 1.6.1170, the last build SKSE64 supports) by applying the hash-verified
HDiffPatch patches that "runtime swap" mods ship in ``RuntimeSwap/`` (SRS -
Best of All Worlds, Nexus mod 189855).

Why this is not just "install the SRS mod": the mod patches the game through a
``version.dll`` hijack at launch, which Mosaic's deploy can't host (the patched
base files live in ``Data_Core/``'s blind spot, and its relaunch never completes
under Wine). Mosaic instead applies the *same* patches itself, once, while the
game is restored — so the next deploy snapshots the patched files into
``Data_Core/`` like any other vanilla file.

Pure logic — no Qt, no Wine. These are files Steam owns, so it is transactional:
every source file is hash-checked, backed up, patched to a temp file, and
hash-checked again before anything is swapped in. Any failure leaves the game
folder exactly as it was (or rolls it back), and a recorded swap can be reverted
from the hash-checked backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from Utils.wizard_support.pe_version import Version, format_version, read_file_version

STEAM_APP_ID = 489830
VERSION_TARGET = "SkyrimSE.exe"          # its file version identifies the runtime

# Nexus mods known to ship a RuntimeSwap/ folder Mosaic can apply natively. A
# collection listing one of these is asking for a runtime change.
RUNTIME_SWAP_MOD_IDS = frozenset({189855})   # SRS - Best of All Worlds

SWAP_DIRNAME = "RuntimeSwap"
MANIFEST_NAME = "manifest.json"
STATE_FILENAME = "runtime_state.json"
BACKUP_DIRNAME = "runtime_backup"

# Mosaic's deploy leaves these behind while the game is deployed.
_DEPLOY_MARKER = ("Data", ".mm_deployed")
_CORE_DIRNAME = "Data_Core"
_LAUNCHER_BACKUP = "SkyrimSELauncher.bak"       # script_extender_swap parks the real launcher here

_INSTALL_HPATCHZ = ("hpatchz was not found. Install it (Arch: 'paru -S hdiffpatch-bin', or "
                    "download 'hdiffpatch_*_bin_linux64.zip' from "
                    "https://github.com/sisong/HDiffPatch/releases and put hpatchz on your "
                    "PATH) and try again.")


class RuntimeSwapError(RuntimeError):
    """A runtime-swap step failed; the message is fit to show the user."""


def _noop(_msg: str) -> None:
    pass


# ---- the swap description ------------------------------------------------------

@dataclass(frozen=True)
class FileSwap:
    path: str                       # relative to the game root, forward slashes
    source_sha256: str
    target_sha256: str
    source_size: int
    forward_patch: Path             # absolute, inside the swap folder


@dataclass(frozen=True)
class Transition:
    """One source-runtime -> target-runtime change, as described by a swap manifest."""
    source: Version
    target: Version
    files: tuple[FileSwap, ...]
    app_id: int


def parse_version(text: str) -> Version:
    """"1.7.104" / "1.7.104.0" -> (1, 7, 104, 0)."""
    try:
        parts = [int(p) for p in str(text).strip().split(".")]
    except ValueError as exc:
        raise RuntimeSwapError(f"Unreadable version {text!r} in the runtime swap manifest.") from exc
    if not 1 <= len(parts) <= 4:
        raise RuntimeSwapError(f"Unreadable version {text!r} in the runtime swap manifest.")
    return tuple(parts + [0] * (4 - len(parts)))          # type: ignore[return-value]


def find_swap_dir(root: "str | Path") -> Path | None:
    """The ``RuntimeSwap`` folder (the one holding manifest.json) under *root*, or None."""
    root = Path(root)
    direct = root / SWAP_DIRNAME
    if (direct / MANIFEST_NAME).is_file():
        return direct
    for manifest in root.rglob(MANIFEST_NAME):
        if manifest.parent.name.lower() == SWAP_DIRNAME.lower():
            return manifest.parent
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_relpath(rel: str) -> str:
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or ".." in p.parts or "\\" in rel:
        raise RuntimeSwapError(f"The runtime swap manifest lists an unsafe path: {rel!r}.")
    return p.as_posix()


def load_transition(swap_dir: "str | Path", *, verify_patches: bool = True) -> Transition:
    """Parse ``<swap_dir>/manifest.json`` and check its patches are intact.

    Refuses anything it can't apply safely: an algorithm other than HDiffPatch, a
    file the swap would create or delete (only in-place patches are supported),
    an unsafe path, or a patch whose SHA-256 differs from the manifest's.
    """
    swap_dir = Path(swap_dir)
    try:
        manifest = json.loads((swap_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeSwapError(f"Couldn't read the runtime swap manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RuntimeSwapError("The runtime swap manifest is not in a format Mosaic understands.")

    algorithm = str(manifest.get("algorithm", ""))
    if not algorithm.lower().startswith("hdiffpatch"):
        raise RuntimeSwapError(
            f"This runtime swap uses {algorithm or 'an unknown patch algorithm'!r}, "
            "which Mosaic can't apply.")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimeSwapError("The runtime swap manifest lists no files.")

    files: list[FileSwap] = []
    for entry in entries:
        try:
            rel = _safe_relpath(str(entry["path"]))
            if not (entry.get("sourcePresent", True) and entry.get("targetPresent", True)):
                raise RuntimeSwapError(
                    f"{rel}: this swap adds or removes a file, which Mosaic doesn't support.")
            patch_rel = _safe_relpath(str(entry["forwardPatch"]))
            patch = swap_dir / "patches" / patch_rel
            if not patch.is_file():
                raise RuntimeSwapError(f"The patch for {rel} is missing from the archive ({patch_rel}).")
            wanted = str(entry.get("forwardPatchSha256", "")).lower()
            if verify_patches and wanted and _sha256(patch) != wanted:
                raise RuntimeSwapError(
                    f"The patch for {rel} is corrupt (checksum mismatch). Re-download the archive.")
            files.append(FileSwap(
                path=rel,
                source_sha256=str(entry["sourceSha256"]).lower(),
                target_sha256=str(entry["targetSha256"]).lower(),
                source_size=int(entry.get("sourceSize", 0)),
                forward_patch=patch,
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeSwapError(f"The runtime swap manifest has a malformed file entry: {exc!r}.") from exc

    if VERSION_TARGET not in {f.path for f in files}:
        raise RuntimeSwapError(f"This runtime swap doesn't patch {VERSION_TARGET}, so it can't be verified.")
    try:
        app_id = int(manifest.get("appId", STEAM_APP_ID))
    except (TypeError, ValueError):
        app_id = -1
    return Transition(
        source=parse_version(manifest.get("sourceVersion", "")),
        target=parse_version(manifest.get("targetVersion", "")),
        files=tuple(files),
        app_id=app_id,
    )


# ---- discovery -----------------------------------------------------------------

HPATCHZ_VERSION = "5.1.3"
HPATCHZ_ZIP_URL = ("https://github.com/sisong/HDiffPatch/releases/download/"
                   f"v{HPATCHZ_VERSION}/hdiffpatch_v{HPATCHZ_VERSION}_bin_linux64.zip")
# SHA-256 of that exact release zip — a download that differs is never run.
HPATCHZ_ZIP_SHA256 = "628963bf2ee9108a97260fa5eef44acd9ec94369b76090a957c9182b3abbb558"
_HPATCHZ_MEMBER = "linux64/hpatchz"          # statically linked, MIT licensed


def managed_hpatchz_path() -> Path:
    """Where Mosaic keeps its own copy of hpatchz (used when none is on PATH)."""
    from Utils.config_paths import get_config_dir
    return get_config_dir() / "bin" / f"hpatchz-{HPATCHZ_VERSION}"


def find_hpatchz() -> str | None:
    """hpatchz on PATH (the AppImage bundles one), else Mosaic's managed copy."""
    found = shutil.which("hpatchz")
    if found:
        return found
    managed = managed_hpatchz_path()
    return str(managed) if managed.is_file() and os.access(managed, os.X_OK) else None


def _download_bytes(url: str) -> bytes:
    import urllib.request
    from Utils.ca_bundle import get_ssl_context
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60, context=get_ssl_context()) as resp:
        return resp.read()


def ensure_hpatchz(
    *,
    fetch: Callable[[str], bytes] = _download_bytes,
    log_fn: Callable[[str], None] = _noop,
) -> str:
    """Path to a usable hpatchz, downloading Mosaic's managed copy if there is none.

    The download is the upstream HDiffPatch release zip, accepted only if its
    SHA-256 matches :data:`HPATCHZ_ZIP_SHA256`. Raises :class:`RuntimeSwapError`
    (with a manual-install hint) if it can't be obtained.
    """
    found = find_hpatchz()
    if found:
        return found
    import platform
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise RuntimeSwapError(_INSTALL_HPATCHZ)
    log_fn(f"Runtime swap: downloading hpatchz {HPATCHZ_VERSION} …")
    try:
        blob = fetch(HPATCHZ_ZIP_URL)
    except Exception as exc:                    # noqa: BLE001 — any network failure is the same to the user
        raise RuntimeSwapError(f"Couldn't download hpatchz ({exc}). {_INSTALL_HPATCHZ}") from exc
    if hashlib.sha256(blob).hexdigest() != HPATCHZ_ZIP_SHA256:
        raise RuntimeSwapError(
            "The downloaded hpatchz didn't match its expected checksum, so it was not used. "
            + _INSTALL_HPATCHZ)
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            data = zf.read(_HPATCHZ_MEMBER)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise RuntimeSwapError(f"The downloaded hpatchz archive was unreadable ({exc!r}).") from exc
    dest = managed_hpatchz_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(data)
        tmp.chmod(0o755)
        os.replace(tmp, dest)
    except OSError as exc:
        raise RuntimeSwapError(f"Couldn't save hpatchz ({exc}). {_INSTALL_HPATCHZ}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    log_fn(f"Runtime swap: hpatchz saved to {dest}")
    return str(dest)


def read_runtime_version(game_root: "str | Path") -> Version | None:
    return read_file_version(Path(game_root) / VERSION_TARGET)


def is_deployed(game_root: "str | Path") -> bool:
    """True while Mosaic's deploy is in place (or an interrupted one left debris).

    Patching then would either break links into Data_Core/ or be undone by the
    next restore, so the game must be restored first. The marker is the reliable
    signal; the launcher backup only exists when script_extender_swap is on.
    """
    root = Path(game_root)
    return ((root.joinpath(*_DEPLOY_MARKER)).exists()
            or (root / _CORE_DIRNAME).exists()
            or (root / _LAUNCHER_BACKUP).exists())


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
    """What the wizard / preflight shows about the install, for one transition."""
    version: Version | None
    state: str                      # "source" | "target" | "unsupported"
    swapped_by_mosaic: bool         # a Mosaic-recorded swap is still in place
    blockers: list[str]             # why a swap can't run right now
    revert_blockers: list[str]      # why a revert can't run right now

    @property
    def can_apply(self) -> bool:
        return self.state == "source" and not self.blockers

    @property
    def can_revert(self) -> bool:
        return self.swapped_by_mosaic and not self.revert_blockers


def assess(
    game_root: "str | Path",
    state_dir: "str | Path",
    transition: Transition,
    *,
    hpatchz: str | None,
    game_running: bool,
) -> Assessment:
    """Inspect the install against *transition*. Cheap: reads only the exe version.

    A recorded swap only counts while SkyrimSE.exe still reads as the target — if
    Steam has since re-downloaded the newer exe, the record is stale and the game
    is simply patchable again. (File hashes are checked by :func:`apply_transition`.)
    """
    game_root = Path(game_root)
    version = read_runtime_version(game_root)
    if version == transition.source:
        state = "source"
    elif version == transition.target:
        state = "target"
    else:
        state = "unsupported"
    recorded = load_state(state_dir)
    swapped = bool(recorded and recorded.get("applied")) and state == "target"

    shared: list[str] = []
    if game_running:
        shared.append("Skyrim is running — close it first.")
    if is_deployed(game_root):
        shared.append("Mosaic's mods are deployed — restore the game first.")

    blockers = list(shared)
    missing = [f.path for f in transition.files if not (game_root / f.path).is_file()]
    if missing:
        blockers.append(f"Missing from the game folder: {', '.join(missing)}.")
    if not hpatchz:
        blockers.append("hpatchz isn't installed.")
    return Assessment(version, state, swapped, blockers, shared)


# ---- apply ---------------------------------------------------------------------

def _fmt(version: Version | None) -> str:
    return format_version(version) if version else "an unreadable version"


def _write_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (STATE_FILENAME + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, state_dir / STATE_FILENAME)


def _temp_for(game_root: Path, rel: str, tag: str) -> Path:
    target = game_root / rel
    return target.with_name(f".{target.name}.mosaic-{tag}")


def _restore_from_backup(game_root: Path, backup: Path, paths: list[str]) -> None:
    """Put *paths* back from *backup*; raises RuntimeSwapError if any can't be."""
    failed: list[str] = []
    for rel in paths:
        tmp = _temp_for(game_root, rel, "restore")
        try:
            shutil.copy2(backup / rel, tmp)
            os.replace(tmp, game_root / rel)
        except OSError:
            failed.append(rel)
        finally:
            tmp.unlink(missing_ok=True)
    if failed:
        raise RuntimeSwapError(
            f"Could not roll back {', '.join(failed)}. The original files are in "
            f"{backup} — copy them back into the game folder, or use Steam's "
            "'Verify integrity of game files'.")


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def apply_transition(
    game_root: "str | Path",
    transition: Transition,
    state_dir: "str | Path",
    *,
    hpatchz: str | None,
    run: Callable = subprocess.run,
    log_fn: Callable[[str], None] = _noop,
) -> Version:
    """Patch every file in *transition* from the source to the target runtime,
    transactionally. Returns the resulting version.

    *state_dir* is where the backup and ``runtime_state.json`` are kept.
    """
    game_root, state_dir = Path(game_root), Path(state_dir)

    if not hpatchz:
        raise RuntimeSwapError(_INSTALL_HPATCHZ)
    if transition.app_id != STEAM_APP_ID:
        raise RuntimeSwapError("This runtime swap is for a different game.")
    missing = [f.path for f in transition.files if not (game_root / f.path).is_file()]
    if missing:
        raise RuntimeSwapError(f"Not found in the game folder: {', '.join(missing)}.")
    if is_deployed(game_root):
        raise RuntimeSwapError(
            "Mosaic's mods are currently deployed. Restore the game first, then run "
            "the runtime swap.")
    current = read_runtime_version(game_root)
    if current == transition.target:
        raise RuntimeSwapError(f"Skyrim is already {format_version(transition.target)}.")
    if current != transition.source:
        raise RuntimeSwapError(
            f"{VERSION_TARGET} is {_fmt(current)}; this swap only applies to "
            f"{format_version(transition.source)}.")

    # Every file must be the exact original the patches were made against. Checked
    # before anything is touched — a half-modified install must not be patched.
    log_fn("Runtime swap: checking the current game files …")
    changed = [f.path for f in transition.files if _sha256(game_root / f.path) != f.source_sha256]
    if changed:
        raise RuntimeSwapError(
            f"These files are not the original {format_version(transition.source)} versions: "
            f"{', '.join(changed)}. Use Steam's 'Verify integrity of game files', then try again.")

    needed = 2 * sum((game_root / f.path).stat().st_size for f in transition.files)
    if _free_bytes(game_root) < needed or _free_bytes(state_dir) < needed // 2:
        raise RuntimeSwapError(
            f"Not enough free disk space (about {needed // (1 << 20)} MB needed for the "
            "backup and the patched copies).")

    backup = backup_dir(state_dir)
    if backup.exists():
        shutil.rmtree(backup)       # stale, from an earlier attempt — the game is verified original above
    backup.mkdir(parents=True)

    paths = [f.path for f in transition.files]
    temps: dict[str, Path] = {}
    replaced: list[str] = []
    keep_backup = False
    try:
        for f in transition.files:
            dst = backup / f.path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(game_root / f.path, dst)
            if _sha256(dst) != f.source_sha256:
                raise RuntimeSwapError(f"The backup of {f.path} doesn't match the original (disk error?).")
        log_fn(f"Runtime swap: backed up {len(paths)} files to {backup}")

        for f in transition.files:
            tmp = _temp_for(game_root, f.path, "runtime")
            temps[f.path] = tmp
            log_fn(f"Runtime swap: patching {f.path} …")
            proc = run(
                [hpatchz, "-f", str(game_root / f.path), str(f.forward_patch), str(tmp)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                lines = (proc.stderr or proc.stdout or "").strip().splitlines()
                detail = lines[-1] if lines else f"exit {proc.returncode}"
                raise RuntimeSwapError(f"hpatchz could not patch {f.path}: {detail}.")
            if not tmp.is_file() or _sha256(tmp) != f.target_sha256:
                raise RuntimeSwapError(
                    f"The patched {f.path} doesn't match the expected checksum — the patch "
                    "doesn't fit the installed game.")
            shutil.copymode(game_root / f.path, tmp)

        patched = read_file_version(temps[VERSION_TARGET])
        if patched != transition.target:
            raise RuntimeSwapError(
                f"The patched {VERSION_TARGET} reports {_fmt(patched)}, not "
                f"{format_version(transition.target)}.")

        for f in transition.files:
            try:
                os.replace(temps[f.path], game_root / f.path)
            except OSError as exc:
                try:
                    _restore_from_backup(game_root, backup, replaced)
                except RuntimeSwapError:
                    keep_backup = True          # the backup is now the only good copy
                    raise
                raise RuntimeSwapError(
                    f"Could not replace {f.path} ({exc}); the game files were rolled back."
                ) from exc
            replaced.append(f.path)
    except RuntimeSwapError:
        if not keep_backup:
            shutil.rmtree(backup, ignore_errors=True)
        raise
    finally:
        for tmp in temps.values():
            tmp.unlink(missing_ok=True)

    state = {
        "applied": True,
        "from": format_version(transition.source),
        "to": format_version(transition.target),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backup_dir": str(backup),
        "files": {
            f.path: {"original_sha256": f.source_sha256, "patched_sha256": f.target_sha256}
            for f in transition.files
        },
    }
    try:
        _write_state(state_dir, state)
    except OSError as exc:
        log_fn(f"Runtime swap: warning — couldn't record the swap state ({exc}); "
               f"the originals are backed up in {backup}.")
    log_fn(f"Runtime swap: Skyrim is now {format_version(transition.target)}.")
    return transition.target


# ---- revert --------------------------------------------------------------------

def revert_transition(
    game_root: "str | Path",
    state_dir: "str | Path",
    *,
    log_fn: Callable[[str], None] = _noop,
) -> None:
    """Restore the backed-up original files and clear the recorded state."""
    game_root, state_dir = Path(game_root), Path(state_dir)
    state = load_state(state_dir)
    if not state or not state.get("applied"):
        raise RuntimeSwapError("Nothing to revert — no Mosaic runtime swap is recorded for this game.")
    if is_deployed(game_root):
        raise RuntimeSwapError(
            "Mosaic's mods are currently deployed. Restore the game first, then revert the swap.")

    backup = backup_dir(state_dir)
    files = state.get("files") or {}
    if not files:
        raise RuntimeSwapError("The recorded runtime swap lists no files. Use Steam's "
                               "'Verify integrity of game files' instead.")
    for rel, info in files.items():
        wanted = (info or {}).get("original_sha256")
        try:
            rel = _safe_relpath(rel)
        except RuntimeSwapError:
            raise RuntimeSwapError("The recorded runtime swap is damaged. Use Steam's "
                                   "'Verify integrity of game files' instead.") from None
        saved = backup / rel
        if not saved.is_file() or not wanted or _sha256(saved) != wanted:
            raise RuntimeSwapError(
                f"The backup of {rel} is missing or has been modified, so it won't "
                "be restored. Use Steam's 'Verify integrity of game files' instead.")

    temps: list[tuple[str, Path]] = []
    try:
        for rel in files:
            tmp = _temp_for(game_root, rel, "revert")
            temps.append((rel, tmp))
            shutil.copy2(backup / rel, tmp)
        for rel, tmp in temps:
            os.replace(tmp, game_root / rel)
    except OSError as exc:
        raise RuntimeSwapError(f"Could not restore the original files: {exc}") from exc
    finally:
        for _rel, tmp in temps:
            tmp.unlink(missing_ok=True)

    (state_dir / STATE_FILENAME).unlink(missing_ok=True)
    shutil.rmtree(backup, ignore_errors=True)
    log_fn(f"Runtime swap: reverted to {_fmt(read_runtime_version(game_root))}.")

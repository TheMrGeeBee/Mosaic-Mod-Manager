"""Vampire: The Masquerade - Bloodlines — Unofficial Patch installer extraction.

The Unofficial Patch (Nexus ``vampirebloodlines`` mod 80, an Inno Setup
``VTMBup*.exe``, ~600 MB) is a whole game-root payload: the ``Unofficial_Patch/``
mod folder and ``Patch_Extras/``, the Advanced EXE Loader (``Loader.exe``,
``Bin/loader/``), the patched ``Vampire.exe`` (the only file with the 4 GB
Large-Address-Aware flag — Steam's own ``vampire.exe`` is a 25 KB stub without
it), and also replacement ``Bin/*.dll`` and 109 files inside the vanilla
``Vampire/`` folder.

Run under Wine, the official installer replaces Steam's ``vampire.exe`` with the
patched one. ``innoextract`` only unpacks files, so this module does that one
step itself: Steam's stock exe is replaced by the patched one (nobody plays
modded Bloodlines on the stock exe, so no backup is kept — Steam's "Verify
integrity" restores it if ever wanted). The vanilla ``Vampire/`` folder and
``Bin/*.dll`` are never written. Pure logic — no Qt — so it can be tested
against the real installer.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

NEXUS_DOMAIN = "vampirebloodlines"
NEXUS_MOD_ID = 80
NEXUS_URL = f"https://www.nexusmods.com/{NEXUS_DOMAIN}/mods/{NEXUS_MOD_ID}?tab=files"

# Installer filenames are "VTMBup120.6.exe" (author) or
# "VTMBup120.6 80 12.0 <date> <hash>.exe" (Nexus).
INSTALLER_PREFIX = "vtmbup"

_APP = "app"                      # the installer's {app} directory
# Game-root-relative paths copied into the game folder as-is. Anything else —
# notably Vampire/ and Bin/*.dll — is deliberately left alone.
ALLOWED = ("Unofficial_Patch", "Patch_Extras", "Loader.exe", "Bin/loader")
PATCHED_EXE_IN_INSTALLER = "Vampire.exe"     # handled by install_exe(), not copied
_VANILLA = "vampire"
_LAA = 0x20                       # IMAGE_FILE_LARGE_ADDRESS_AWARE


class PatchError(Exception):
    """User-facing failure."""


def find_innoextract() -> str | None:
    return shutil.which("innoextract")


def find_installer(directories, mod_id: int = NEXUS_MOD_ID) -> Path | None:
    """Newest ``VTMBup*.exe`` in *directories* (Mosaic's cache, Downloads)."""
    best: tuple[float, Path] | None = None
    for d in directories:
        try:
            entries = list(Path(d).iterdir())
        except OSError:
            continue
        for p in entries:
            if not (p.is_file() and p.suffix.lower() == ".exe"
                    and p.name.lower().startswith(INSTALLER_PREFIX)):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, p)
    return best[1] if best else None


def is_allowed(rel: str) -> bool:
    """True for a game-root-relative path inside the copy allow-list.

    Case-insensitive; never true for anything under the vanilla ``Vampire/`` or
    for the exe itself (that goes through ``install_exe``).
    """
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if not parts or ".." in parts or parts[0].lower() == _VANILLA:
        return False
    low = "/".join(parts).lower()
    for allowed in ALLOWED:
        a = allowed.lower()
        if low == a or low.startswith(a + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Patch version
# ---------------------------------------------------------------------------
# The patch shows its own version in the in-game Options screen; it comes from
# resource/gameui_english.txt:  "GameUI_Patch"  "Unofficial Patch 12.0".
# Steam installs keep the patch in Unofficial_Patch/; a prepatched GOG install
# may carry it elsewhere, so the vanilla Vampire/ folder is checked too.
_VERSION_FILE = ("resource", "gameui_english.txt")
_VERSION_DIRS = ("Unofficial_Patch", "Vampire")
_PATCH_KEY_RE = re.compile(r'"GameUI_Patch"\s+"([^"]*)"', re.IGNORECASE)
_VERSION_RE = re.compile(r"Unofficial Patch\s+v?(\d+(?:\.\d+)*)", re.IGNORECASE)


def _child_ci(parent: Path, name: str) -> Path | None:
    """Case-insensitive child lookup (Linux paths are case-sensitive)."""
    try:
        for n in os.listdir(parent):
            if n.lower() == name.lower():
                return parent / n
    except OSError:
        pass
    return None


def _decode(raw: bytes) -> str:
    """Source ``.txt`` resources are UTF-16 (BOM) as often as UTF-8."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def parse_version(text: str) -> str | None:
    """The version number from a ``GameUI_Patch`` line or a free-form title."""
    m = _PATCH_KEY_RE.search(text)
    m = _VERSION_RE.search(m.group(1) if m else text)
    return m.group(1) if m else None


def version_tuple(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (version or "").split("."))
    except ValueError:
        return ()


def read_installed_version(game_root: Path) -> tuple[str, str] | None:
    """(version, folder it was found in) of the installed patch, or None."""
    for folder in _VERSION_DIRS:
        node = _child_ci(game_root, folder)
        for part in _VERSION_FILE:
            node = _child_ci(node, part) if node is not None else None
        if node is None or not node.is_file():
            continue
        try:
            text = _decode(node.read_bytes())
        except OSError:
            continue
        version = parse_version(text)
        if version:
            return version, folder
    return None


def installer_version(installer: Path, tool: str | None = None) -> str | None:
    """The patch version an installer carries, from its own title line."""
    tool = tool or find_innoextract()
    if tool is None or not installer.is_file():
        return None
    try:
        proc = subprocess.Popen([tool, "--list", str(installer)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, errors="replace")
    except OSError:
        return None
    try:
        first = proc.stdout.readline() if proc.stdout else ""   # Listing "<name> <ver>"
    finally:
        proc.kill()
        proc.wait()
    return parse_version(first)


# ---------------------------------------------------------------------------
# The game exe
# ---------------------------------------------------------------------------

def is_large_address_aware(exe: Path) -> bool:
    """True if the PE at *exe* has the 4 GB (Large Address Aware) flag."""
    try:
        with open(exe, "rb") as f:
            head = f.read(4096)
        if head[:2] != b"MZ":
            return False
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if head[pe:pe + 4] != b"PE\0\0":
            return False
        return bool(struct.unpack_from("<H", head, pe + 4 + 18)[0] & _LAA)
    except (OSError, struct.error):
        return False


def find_game_exe(game_root: Path) -> Path | None:
    """The game's own exe: Steam installs ``vampire.exe`` (lowercase)."""
    try:
        names = os.listdir(game_root)
    except OSError:
        return None
    for n in sorted(names, key=lambda x: (x != "vampire.exe", x)):
        if n.lower() == "vampire.exe" and (game_root / n).is_file():
            return game_root / n
    return None


def _sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install_exe(patched: Path, game_root: Path, log_fn=None) -> str:
    """Make the game exe the patched one, as the official installer does.

    Returns "patched" (replaced Steam's stock exe), "updated" (already patched,
    a different build written) or "unchanged".
    """
    log = log_fn or (lambda _m: None)
    exe = find_game_exe(game_root)
    if exe is None:
        raise PatchError("vampire.exe was not found in the game folder.")
    if not is_large_address_aware(patched):
        raise PatchError("The installer's Vampire.exe isn't the 4 GB build — "
                         "refusing to install it.")
    was_patched = is_large_address_aware(exe)
    if was_patched and _sha1(exe) == _sha1(patched):
        return "unchanged"
    tmp = exe.with_name(exe.name + ".mosaic-new")
    shutil.copy2(patched, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, exe)             # same name/case as Steam's exe
    log(f"Installed the 4 GB-patched exe as {exe.name}.")
    return "updated" if was_patched else "patched"


def assess(game_root: Path) -> dict:
    """What the game folder already has."""
    patch = game_root / "Unofficial_Patch"
    loader_ok = ((game_root / "Loader.exe").is_file()
                 and (game_root / "Bin" / "loader").is_dir())
    exe = find_game_exe(game_root)
    installed = read_installed_version(game_root)
    return {
        "game_found": (game_root / "Vampire").is_dir(),
        "patch_version": installed[0] if installed else None,
        "patch_version_in": installed[1] if installed else None,
        "patch_installed": patch.is_dir() and any(patch.iterdir()),
        "loader_present": loader_ok,
        "exe_found": exe is not None,
        "exe_patched": exe is not None and is_large_address_aware(exe),
    }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _list_files(installer: Path, tool: str) -> list[str]:
    """Installer-relative-to-app file paths, via ``innoextract --list``."""
    try:
        r = subprocess.run([tool, "--list", "--silent", str(installer)],
                           capture_output=True, text=True, errors="replace")
    except OSError as exc:
        raise PatchError(f"Could not run innoextract ({tool}): {exc}") from exc
    if r.returncode != 0:
        raise PatchError(
            "That file isn't a readable Inno Setup installer: "
            + (r.stderr.strip().splitlines() or ["innoextract failed"])[-1])
    prefix = _APP + "/"
    out = []
    for line in r.stdout.splitlines():
        m = re.match(r'\s*(?:- )?"?(.+?)"?(?:\s+\(.*)?$', line)
        name = (m.group(1) if m else line).strip().strip('"')
        if name.startswith(prefix):
            out.append(name[len(prefix):])
    return out


def install_patch(installer: Path, game_root: Path, tool: str | None = None,
                  *, log_fn=None, progress_fn=None) -> int:
    """Extract the allow-listed parts of *installer* into *game_root* and make
    the game exe the patched one.

    Returns the number of files written (the exe counts as one when it was
    installed or updated). Existing patch files are overwritten in place (this
    is also the update path); ``Vampire/`` is never touched.
    """
    log = log_fn or (lambda _m: None)
    tool = tool or find_innoextract()
    if tool is None:
        raise PatchError(
            "innoextract is required to unpack the patch installer but wasn't "
            "found. Install it (e.g. `sudo pacman -S innoextract`, "
            "`sudo apt install innoextract`) and try again.")
    if not installer.is_file():
        raise PatchError("Installer not found.")
    if not (game_root / "Vampire").is_dir():
        raise PatchError(
            "The game folder has no Vampire/ directory — check the game path.")
    if find_game_exe(game_root) is None:
        raise PatchError("vampire.exe was not found in the game folder — "
                         "check the game path.")

    listed = _list_files(installer, tool)
    members = [m for m in listed if is_allowed(m)]
    if not any(m.lower().startswith("unofficial_patch/") for m in members):
        raise PatchError("This installer doesn't contain Unofficial_Patch/ — "
                         "is it the Bloodlines Unofficial Patch (VTMBup*.exe)?")
    if PATCHED_EXE_IN_INSTALLER not in listed:
        raise PatchError("This installer doesn't contain the patched Vampire.exe.")
    total = len(members)
    log(f"Installer holds {total} allow-listed file(s); extracting…")

    # Stage inside the game folder so the final moves are same-filesystem renames.
    stage = Path(tempfile.mkdtemp(prefix=".mosaic-vtmb-", dir=game_root))
    try:
        cmd = [tool, "--exclude-temp", "-d", str(stage)]
        for a in (*ALLOWED, PATCHED_EXE_IN_INSTALLER):
            cmd += ["-I", f"{_APP}/{a}"]
        cmd.append(str(installer))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    errors="replace")
        except OSError as exc:
            raise PatchError(f"Could not run innoextract ({tool}): {exc}") from exc
        done = 0
        tail: list[str] = []
        for line in proc.stdout:               # type: ignore[union-attr]
            tail = (tail + [line.rstrip()])[-6:]
            if ' - "' in line or line.lstrip().startswith("- "):
                done += 1
                if progress_fn and done % 200 == 0:
                    progress_fn(min(done, total), total)
        if proc.wait() != 0:
            raise PatchError("innoextract failed: " + " | ".join(tail[-3:]))

        src_root = stage / _APP
        if not src_root.is_dir():
            raise PatchError("Extraction produced no files.")
        written = 0
        for dirpath, _dirs, files in os.walk(src_root):
            for fn in files:
                src = Path(dirpath) / fn
                rel = src.relative_to(src_root).as_posix()
                if not is_allowed(rel):          # belt and braces
                    continue
                dest = game_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dest)
                written += 1
        patched = src_root / PATCHED_EXE_IN_INSTALLER
        if patched.is_file():
            if install_exe(patched, game_root, log) != "unchanged":
                written += 1
        if progress_fn:
            progress_fn(total, total)
        log(f"Wrote {written} file(s) into the game folder.")
        return written
    finally:
        shutil.rmtree(stage, ignore_errors=True)

"""MosaicPathIndex: a file-lookup index for Bethesda games under Wine/Proton.

Under Wine, every file lookup whose exact path misses -- a different letter
case, or a file that isn't there at all -- makes Wine read the whole folder
and compare every entry. A modded Data/ holds thousands of entries and the
game makes hundreds of thousands of such lookups while it loads: LoreRim spent
about 150 s of a 360 s load on it (MO2 avoids it through its USVFS).

MosaicPathIndex.dll is a small script-extender plugin (source:
native/path_index/) that indexes the game folder once and answers those
lookups itself. One source is built twice: 64-bit for SKSE64, SKSE VR, F4SE,
F4SE VR and SFSE, and 32-bit (MosaicPathIndex32.dll, installed under the
same name) for SKSE, NVSE, FOSE and OBSE. Mosaic puts it into the game on
deploy, together with a list of every file and folder so the DLL needn't list
~250K entries through Wine, and takes both away again on restore.

The DLL is built by CI (and by native/path_index/build.sh for a source
checkout) into this folder. Without it nothing is installed.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

DLL_NAME = "MosaicPathIndex.dll"
DLL32_NAME = "MosaicPathIndex32.dll"     # shipped name of the 32-bit build
LIST_NAME = "MosaicPathIndex.idx"
LOG_NAME = "MosaicPathIndex.log"
LIST_MAGIC = "MOSAIC-PATHINDEX 1\n"
OUR_FILES = (DLL_NAME, LIST_NAME, LOG_NAME)

# FILETIME counts 100 ns ticks from 1601; Wine reports a folder's last-write
# time as its Unix mtime converted exactly like this.
_TICKS_1601_TO_1970 = 116444736000000000

LogFn = Callable[[str], None]


def bundled_dll(bits: int = 64) -> Path | None:
    """The built DLL shipped with Mosaic for a *bits*-bit game, or None (a
    source checkout that hasn't run native/path_index/build.sh)."""
    p = Path(__file__).with_name(DLL32_NAME if bits == 32 else DLL_NAME)
    return p if p.is_file() else None


def filetime(mtime_ns: int) -> int:
    return mtime_ns // 100 + _TICKS_1601_TO_1970


def write_file_list(game_root: Path, out: Path) -> tuple[int, int]:
    """Write every file and folder under *game_root* to *out* in the DLL's
    format, each folder with its last-write time. Returns (files, folders).

    Symlinked folders are followed the way Wine follows them; a folder reached
    twice (a link loop) is listed once. Names Windows can't hold are skipped:
    the DLL never sees them either.
    """
    files = folders = 0
    seen: set[tuple[int, int]] = set()
    lines: list[str] = [LIST_MAGIC]

    def walk(path: Path, rel: str) -> None:
        nonlocal files, folders
        try:
            st = os.stat(path)
        except OSError:
            return
        key = (st.st_dev, st.st_ino)
        if key in seen:
            return
        seen.add(key)
        lines.append(f"D {filetime(st.st_mtime_ns)} {rel or '.'}\n")
        folders += 1
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        subdirs = []
        for e in entries:
            name = e.name
            if "\n" in name or "\\" in name:
                continue
            child = f"{rel}\\{name}" if rel else name
            try:
                is_dir = e.is_dir()          # follows symlinks, like Wine
            except OSError:
                is_dir = False
            if is_dir:
                subdirs.append((Path(e.path), child))
            else:
                lines.append(f"F {child}\n")
                files += 1
        for p, child in subdirs:
            walk(p, child)

    walk(game_root, "")
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8", errors="surrogateescape")
    os.replace(tmp, out)
    return files, folders


def install(game_root: Path, plugin_dir: str, log_fn: LogFn, bits: int = 64) -> bool:
    """Put the DLL for a *bits*-bit game and a fresh file list into
    ``<game_root>/Data/<plugin_dir>``.

    Run after deploy, once every file is in place. Returns False (and logs
    why) when nothing was installed.
    """
    dll = bundled_dll(bits)
    if dll is None:
        log_fn(f"  Fast file lookups: {DLL32_NAME if bits == 32 else DLL_NAME} isn't built "
               "in this copy of Mosaic (run native/path_index/build.sh); skipped.")
        return False
    dest = Path(game_root) / "Data" / plugin_dir
    try:
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / DLL_NAME
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_bytes(dll.read_bytes())       # a real file: never a link
        files, folders = write_file_list(Path(game_root), dest / LIST_NAME)
    except OSError as exc:
        log_fn(f"  Fast file lookups: couldn't install MosaicPathIndex: {exc}")
        return False
    log_fn(f"  Fast file lookups: MosaicPathIndex installed; indexed {files} file(s) "
           f"in {folders} folder(s).")
    return True


def remove(game_root: Path, plugin_dir: str, log_fn: LogFn) -> int:
    """Take the DLL, the list and the DLL's log out of the game again.

    Runs before restore clears Data/, which would otherwise treat them as
    files the game made and move them to overwrite."""
    dest = Path(game_root) / "Data" / plugin_dir
    removed = 0
    for name in OUR_FILES + (LIST_NAME + ".tmp",):
        p = dest / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
                removed += 1
        except OSError as exc:
            log_fn(f"  Fast file lookups: couldn't remove {p}: {exc}")
    if removed:
        log_fn("  Fast file lookups: MosaicPathIndex removed.")
    return removed

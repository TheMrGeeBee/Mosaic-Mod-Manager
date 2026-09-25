"""
collection_audit.py
Post-install checks on the finished profile — problems that don't stop an install
but make the game silently misbehave.

Pure logic, no Qt. Today: SKSE64 only works with the exact game runtime it was
built for, and its DLL's name says which (``skse64_1_6_1170.dll``). A profile
whose SKSE64 doesn't match ``SkyrimSE.exe`` launches the game and does nothing
visible (SKSE refuses to load and the mods that need it never start).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

from Utils.wizard_support.pe_version import Version, format_version

_SKSE_DLL = re.compile(r"^skse64_(\d+)_(\d+)_(\d+)\.dll$", re.IGNORECASE)


def find_skse_runtimes(mods_dir: "str | Path", enabled_mods: Iterable[str],
                       extra_dirs: Iterable["str | Path"] = ()) -> dict[tuple[int, int, int], list[str]]:
    """``{(major, minor, build): [mod names]}`` for every ``skse64_<v>.dll`` at the
    top level of an enabled mod folder (where SKSE archives put it) or of an
    *extra_dirs* folder such as the profile's Root_Folder."""
    found: dict[tuple[int, int, int], list[str]] = {}

    def scan(folder: Path, label: str) -> None:
        try:
            with os.scandir(folder) as it:
                names = [e.name for e in it if e.is_file()]
        except OSError:
            return
        for name in names:
            m = _SKSE_DLL.match(name)
            if m:
                found.setdefault(tuple(int(g) for g in m.groups()), []).append(label)  # type: ignore[arg-type]

    root = Path(mods_dir)
    for name in enabled_mods:
        scan(root / name, name)
    for extra in extra_dirs:
        scan(Path(extra), Path(extra).name)
    return found


def check_skse_runtime(exe_version: Version | None,
                       found: dict[tuple[int, int, int], list[str]]) -> str | None:
    """A warning when the profile's SKSE64 doesn't match the game runtime, else None.

    Returns None when there is no SKSE64 in the profile (a collection may not
    need it, or it may live in the game folder) or the exe version is unreadable.
    """
    if not found or exe_version is None:
        return None
    game = exe_version[:3]
    if game in found:
        return None
    have = ", ".join(sorted(".".join(map(str, v)) for v in found))
    return (f"SKSE64 in this profile is built for {have}, but SkyrimSE.exe is "
            f"{format_version(exe_version)}. SKSE will refuse to load and every mod "
            "that needs it will silently not run. Switch the game runtime (or install "
            "the matching SKSE64 build) before playing.")

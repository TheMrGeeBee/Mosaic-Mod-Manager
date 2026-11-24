"""BSA Memory Map (``BSAMemoryMap.dll``, Nexus 175531) vs. SKSE hooks under Proton.

Wine loads SkyrimSE.exe at its preferred base 0x140000000 (no ASLR) and hands
out large allocations from the low address range. BSA Memory Map maps whole
BSA archives while SKSE plugins load, and those mappings fill the entire ±2 GB
window around the exe. Every hooking plugin that loads after it (MinHook,
Detours, the CommonLib trampoline) then fails to get a nearby trampoline, e.g.
"dosemetha.dll main.cpp(110): Failed to hook GetLocationsCandidates".
Reproduced in stock Wabbajack + MO2 as well (LoreRim, 2026-11-23), so it is
the plugin under Wine, not Mosaic's deploy.

Only ``bEnabled=0`` cures it. ``bEnableMmap=0`` alone still leaves its own
Detours hook failing with error 8. Windows is unaffected because ASLR places
the exe far away from those mappings.

This module only detects and, when the user asks, switches the plugin off. It
never changes a mod on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from Utils.wine_proton.framework_detect import resolve_file_ci

PLUGINS_REL = Path("SKSE") / "Plugins"
DLL_NAME = "BSAMemoryMap.dll"
INI_NAME = "BSAMemoryMap.ini"

# `bEnabled = 1  ; comment` — key match is case-insensitive like the plugin's
# INI reader. Group 1 is everything up to the value, group 2 the value.
_ENABLED_RE = re.compile(rb"^([ \t]*bEnabled[ \t]*=[ \t]*)([^;#\r\n]*)", re.I | re.M)
_FALSE = {b"0", b"false", b"off", b"no"}


@dataclass
class BsaMemoryMapFinding:
    """BSA Memory Map deployed and switched on."""
    dll: Path
    ini: Path | None                                  # deployed ini, if any
    ini_sources: list[Path] = field(default_factory=list)   # every copy to edit

    @property
    def can_disable(self) -> bool:
        """True when there is a ``bEnabled`` line to flip."""
        return any(_read_enabled(p) is not None for p in self.ini_sources)


def _read_enabled(ini: Path) -> bool | None:
    """The ``bEnabled`` value, or None when the file or the key is missing."""
    try:
        m = _ENABLED_RE.search(ini.read_bytes())
    except OSError:
        return None
    if m is None:
        return None
    return m.group(2).strip().lower() not in _FALSE


def find_enabled(data_dir: Path, staged_inis: list[Path] = ()) -> BsaMemoryMapFinding | None:
    """Return a finding when ``BSAMemoryMap.dll`` is deployed in *data_dir* and
    not switched off. No ini, or an ini without ``bEnabled``, counts as on (the
    plugin's default). *staged_inis* are the mod's own copies, edited together
    with the deployed one so the next deploy keeps the change. Never raises."""
    try:
        dll = resolve_file_ci(data_dir, PLUGINS_REL / DLL_NAME)
        if dll is None:
            return None
        ini = resolve_file_ci(data_dir, PLUGINS_REL / INI_NAME)
        if ini is not None and _read_enabled(ini) is False:
            return None
        sources: list[Path] = []
        seen: set[tuple[int, int]] = set()
        for p in [*staged_inis, *([ini] if ini is not None else [])]:
            try:
                st = p.stat()                         # follows symlinks
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)              # hardlink/symlink = one file
            if key not in seen:
                seen.add(key)
                sources.append(p)
        return BsaMemoryMapFinding(dll=dll, ini=ini, ini_sources=sources)
    except Exception:                                 # noqa: BLE001 — never block a launch
        return None


def disable(finding: BsaMemoryMapFinding) -> list[Path]:
    """Set ``bEnabled=0`` in every copy of the ini. Rewrites in place (keeps
    symlinks, hardlinks, line endings and the rest of the file untouched).
    Returns the files it changed. Raises OSError if a write fails."""
    changed: list[Path] = []
    for p in finding.ini_sources:
        data = p.read_bytes()
        new, n = _ENABLED_RE.subn(
            lambda m: m.group(1) + b"0" + m.group(2)[len(m.group(2).rstrip()):],
            data, count=1)
        if n and new != data:
            with open(p, "r+b") as fh:                # same inode, follows symlinks
                fh.write(new)
                fh.truncate()
            changed.append(p)
    return changed

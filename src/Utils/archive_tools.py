"""
archive_tools.py
Small helpers shared by every place that unpacks an archive.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# Native 7-zip binaries, best first: `7zzs` is what the AppImage bundles, `7zz`
# is the standalone build the Flatpak ships in /app/bin, `7z` is p7zip-full and
# `7za` its reduced variant.
_SEVEN_ZIP_NAMES = ("7zzs", "7zz", "7z", "7za")


def find_7z() -> "str | None":
    """Path of the first native 7-zip binary on ``PATH``, or ``None``.

    Prefer this over py7zr for anything beyond plain archives: py7zr cannot
    decode the BCJ2 filter that SKSE-style archives use.

    ``PATH`` is read on every call on purpose -- the AppImage and Flatpak
    launchers extend it at startup, and tests change it.
    """
    for name in _SEVEN_ZIP_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def strip_single_top_dir(root: Path) -> Path:
    """If *root* holds a single directory (ignoring macOS ``__MACOSX``
    metadata), return it so the caller can copy its *contents* rather than the
    wrapper folder; otherwise return *root* itself.

    Mod archives often wrap everything in a version-named folder such as
    ``f4se_0_07_07/``.
    """
    entries = [e for e in root.iterdir() if e.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root

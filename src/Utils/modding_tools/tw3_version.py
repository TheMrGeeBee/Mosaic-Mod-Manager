"""
tw3_version.py
Detect whether a Witcher 3 install is running the Next-Gen (4.x) or
Remastered (5.x) branch, by reading witcher3.exe's own PE file-version
resource -- the same version CDPR shows in its own patch notes. CDPR ships
both as separate, user-selectable Steam/GOG branches; Mosaic has no way to
switch between them (no binary-diff relationship exists), but can detect
which one is currently installed to warn a Collection install that expects
the other.

Remastered moved the renderer-specific exe subfolder from bin/x64/ to
bin/x64_dx12/ (DX12-only) -- confirmed against a real installed Remastered
copy (5.0.15.61352). A Next-Gen (pre-Remastered) install uses bin/x64/
instead. Both are checked: Mosaic's own existing Games.BaseGame.exe_name
property for this game (``bin/x64/witcher3.exe``) predates the Remastered
transition and no longer points at a real file on a Remastered-only
install -- that property is used for Steam-library auto-detection, not for
launching (Steam's own rungameid protocol handles that independently), so
it hasn't broken the Play button, but it isn't a reliable source for "where
is the real exe" either. Worth a follow-up fix in witcher_3.py separately.
"""

from __future__ import annotations

from pathlib import Path

from Utils.wizard_support.pe_version import Version, format_version, read_file_version

STEAM_APP_ID = "292030"

_EXE_CANDIDATES = (
    "bin/x64_dx12/witcher3.exe",  # Remastered (5.x)
    "bin/x64/witcher3.exe",       # Next-Gen / pre-Remastered (4.x and earlier)
)


def find_witcher3_exe(game_root: "str | Path") -> "Path | None":
    """Return the real witcher3.exe under *game_root*, checking both the
    Remastered and pre-Remastered subfolder layouts. None if neither exists."""
    root = Path(game_root)
    for rel in _EXE_CANDIDATES:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def read_tw3_version(game_root: "str | Path") -> "Version | None":
    """The installed witcher3.exe's file version, or None if it can't be
    found/read."""
    exe = find_witcher3_exe(game_root)
    return read_file_version(exe) if exe else None


def classify_tw3_version(version: "Version | None") -> str:
    """Return "remastered", "next-gen", or "unknown" from a file-version
    tuple. Major version 5 = Remastered, confirmed against a real install
    (5.0.15.61352); major version 4 = Next-Gen is CDPR's own documented
    version (4.04) but wasn't independently re-verified on a live system
    here, since the only system available had already been upgraded past
    it."""
    if version is None:
        return "unknown"
    major = version[0]
    if major >= 5:
        return "remastered"
    if major == 4:
        return "next-gen"
    return "unknown"


BRANCH_LABELS = {"remastered": "Remastered", "next-gen": "Next-Gen"}


def describe_tw3_version(game_root: "str | Path") -> str:
    """Human-readable summary, e.g. "5.0.15.61352 (Remastered)", or
    "Version unknown" if the exe can't be found/read."""
    version = read_tw3_version(game_root)
    if version is None:
        return "Version unknown"
    label = BRANCH_LABELS.get(classify_tw3_version(version))
    suffix = f" ({label})" if label else ""
    return f"{format_version(version)}{suffix}"

"""
openmw_cfg.py
Utility for managing data= and content= entries in openmw.cfg.

OpenMW uses ~/.config/openmw/openmw.cfg (native) or
~/.var/app/org.openmw.OpenMW/config/openmw/openmw.cfg (Flatpak).

Unlike Morrowind.ini, load order is determined solely by the order of
content= lines — no mtime manipulation is needed.

Managed keys (fully replaced on every deploy):
  data=        — directories OpenMW searches for assets and plugins
  content=     — ordered plugin load list (enabled plugins only)
  groundcover= — grass/groundcover plugins (preserved if caller passes None)

Groundcover plugins must be listed as ``groundcover=`` and NOT as ``content=``:
OpenMW would otherwise spawn every blade of grass as a normal object. Which plugins
are groundcover is kept per profile in ``groundcover.txt`` (see read_groundcover).

All other lines (sections, comments, and other key=value pairs) are left
untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

# Vanilla masters are always present and always load first.
_VANILLA_MASTERS = [
    "Morrowind.esm",
    "builtin.omwscripts",
    "Tribunal.esm",
    "Bloodmoon.esm",
]

# Vanilla BSAs — always included as fallback-archive entries before mod BSAs.
_VANILLA_BSAS = [
    "Morrowind.bsa",
    "Tribunal.bsa",
    "Bloodmoon.bsa",
]

# Exact key names we own (lowercase).
_MANAGED_KEYS = {"data", "content", "groundcover", "fallback-archive"}


def _is_managed_line(line: str) -> bool:
    """Return True if this config line is one we manage."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    if "=" not in stripped:
        return False
    key = stripped.split("=", 1)[0].strip().lower()
    return key in _MANAGED_KEYS


def _read_plugins_txt(plugins_txt: Path) -> list[str]:
    """Return the ordered list of ENABLED plugin filenames from plugins.txt.

    plugins.txt is MO2-style: ``*Name`` is enabled, a bare ``Name`` is disabled and
    must not reach openmw.cfg (it used to be written as ``content=`` anyway, so
    disabling a plugin in Mosaic had no effect on OpenMW). '#' lines are comments.
    """
    from Utils.plugins.plugins import read_plugins
    return [e.name for e in read_plugins(plugins_txt, star_prefix=True) if e.enabled]


GROUNDCOVER_FILE = "groundcover.txt"

# Name fragments that mark grass/groundcover plugins in practice (Fantasia Grass
# "FGM_*", "Ozzy's Grass - …", "Grass WG.esp", "Aesthesia groundcover" …). Used only to
# SUGGEST plugins; nothing is marked without the user choosing it.
_GROUNDCOVER_HINTS = ("grass", "groundcover", "fgm_")


def looks_like_groundcover(plugin_name: str) -> bool:
    """True if *plugin_name* looks like a grass/groundcover plugin."""
    low = plugin_name.lower()
    return low.endswith((".esp", ".omwaddon")) and any(h in low for h in _GROUNDCOVER_HINTS)


def read_groundcover(profile_dir: "Path | None") -> "list[str] | None":
    """Plugins the user marked as groundcover for this profile, in file order.

    ``None`` means the profile has never used the feature (no groundcover.txt), in
    which case deploy leaves any ``groundcover=`` lines already in openmw.cfg alone.
    """
    if profile_dir is None:
        return None
    path = Path(profile_dir) / GROUNDCOVER_FILE
    if not path.is_file():
        return None
    out: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name = raw.strip()
        if not name or name.startswith("#") or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return out


def write_groundcover(profile_dir: Path, names: "Iterable[str]") -> None:
    """Save the profile's groundcover plugin names (de-duplicated, order kept)."""
    from Utils.atomic_write import write_atomic_text
    seen: set[str] = set()
    lines: list[str] = []
    for n in names:
        n = str(n).strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            lines.append(n)
    write_atomic_text(Path(profile_dir) / GROUNDCOVER_FILE,
                      "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def set_groundcover(profile_dir: Path, names: "Iterable[str]", marked: bool) -> "list[str]":
    """Mark or unmark *names* as groundcover; returns the new list."""
    change = {str(n).lower(): str(n) for n in names}
    current = read_groundcover(profile_dir) or []
    if marked:
        have = {c.lower() for c in current}
        current = current + [n for low, n in change.items() if low not in have]
    else:
        current = [c for c in current if c.lower() not in change]
    write_groundcover(profile_dir, current)
    return current


def update_openmw_cfg(
    cfg_path: Path,
    data_dirs: list[Path],
    plugins_txt: Path,
    groundcover_plugins: list[str] | None = None,
    fallback_archives: list[str] | None = None,
    log_fn=None,
) -> None:
    """Rewrite the managed data= / content= / groundcover= / fallback-archive= entries in openmw.cfg.

    Args:
        cfg_path:            Path to openmw.cfg.
        data_dirs:           Ordered list of data directories to write as data= entries.
                             Later entries take priority in OpenMW's VFS (override earlier).
        plugins_txt:         Path to the profile's plugins.txt.
        groundcover_plugins: Explicit list of groundcover plugin names to write.  When
                             None, any existing groundcover= lines from the cfg are
                             preserved unchanged.
        fallback_archives:   Ordered list of .bsa archive names to write as
                             fallback-archive= entries.  When None, existing
                             fallback-archive= lines from the cfg are preserved unchanged.
        log_fn:              Optional logging callable.
    """
    _log = log_fn or (lambda _: None)

    # ------------------------------------------------------------------
    # Read existing cfg, stripping managed lines.
    # When groundcover_plugins is None, collect existing groundcover= values
    # so we can re-emit them unchanged.
    # ------------------------------------------------------------------
    preserved: list[str] = []
    existing_groundcover: list[str] = []
    existing_fallback_archives: list[str] = []

    if cfg_path.is_file():
        for raw in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            is_kv = stripped and not stripped.startswith("#") and "=" in stripped
            if is_kv:
                key = stripped.split("=", 1)[0].strip().lower()
                if key == "groundcover" and groundcover_plugins is None:
                    # Preserve existing groundcover= lines when caller did not supply overrides.
                    existing_groundcover.append(stripped.split("=", 1)[1].strip().strip('"'))
                    continue
                if key == "fallback-archive" and fallback_archives is None:
                    # Preserve existing fallback-archive= lines when caller did not supply overrides.
                    existing_fallback_archives.append(stripped.split("=", 1)[1].strip().strip('"'))
                    continue
                if key in _MANAGED_KEYS:
                    continue
            preserved.append(raw)

    # Strip trailing blank lines so the managed block attaches cleanly.
    while preserved and not preserved[-1].strip():
        preserved.pop()

    # ------------------------------------------------------------------
    # Build the ordered plugin list.
    # ------------------------------------------------------------------
    active = _read_plugins_txt(plugins_txt)
    vanilla_lower = {p.lower() for p in _VANILLA_MASTERS}
    # A groundcover plugin is listed as groundcover=, never as content= as well.
    gc_lower = {g.lower() for g in groundcover_plugins or ()}
    user_plugins = [p for p in active
                    if p.lower() not in vanilla_lower and p.lower() not in gc_lower]
    ordered = _VANILLA_MASTERS + user_plugins

    # ------------------------------------------------------------------
    # Assemble managed block.
    # ------------------------------------------------------------------
    managed: list[str] = [""]  # blank separator
    for d in data_dirs:
        managed.append(f'data="{d}"')
    for plugin in ordered:
        managed.append(f"content={plugin}")
    gc_list = groundcover_plugins if groundcover_plugins is not None else existing_groundcover
    for gc in gc_list:
        managed.append(f"groundcover={gc}")
    if fallback_archives is not None:
        # Always include vanilla BSAs first, then mod BSAs (deduped).
        vanilla_bsa_lower = {b.lower() for b in _VANILLA_BSAS}
        mod_bsas = [b for b in fallback_archives if b.lower() not in vanilla_bsa_lower]
        fa_list = _VANILLA_BSAS + mod_bsas
    else:
        fa_list = existing_fallback_archives
    for fa in fa_list:
        managed.append(f"fallback-archive={fa}")

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("\n".join(preserved + managed) + "\n", encoding="utf-8")
    _log(
        f"  Wrote {len(data_dirs)} data dir(s), {len(ordered)} plugin(s), "
        f"and {len(fa_list)} fallback-archive(s) to {cfg_path}."
    )


def restore_openmw_cfg(
    cfg_path: Path,
    data_dirs: list[Path],
    log_fn=None,
) -> None:
    """Restore openmw.cfg to vanilla state: base data dirs and vanilla masters only.

    Args:
        cfg_path:  Path to openmw.cfg.
        data_dirs: Vanilla data directories (typically just the game's Data Files dir).
        log_fn:    Optional logging callable.
    """
    _log = log_fn or (lambda _: None)

    preserved: list[str] = []
    if cfg_path.is_file():
        for raw in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            is_kv = stripped and not stripped.startswith("#") and "=" in stripped
            if is_kv:
                key = stripped.split("=", 1)[0].strip().lower()
                if key in _MANAGED_KEYS:
                    continue
            preserved.append(raw)

    while preserved and not preserved[-1].strip():
        preserved.pop()

    managed: list[str] = [""]
    for d in data_dirs:
        managed.append(f'data="{d}"')
    for plugin in _VANILLA_MASTERS:
        managed.append(f"content={plugin}")
    for bsa in _VANILLA_BSAS:
        managed.append(f"fallback-archive={bsa}")

    cfg_path.write_text("\n".join(preserved + managed) + "\n", encoding="utf-8")
    _log(
        f"  Restored openmw.cfg to {len(data_dirs)} vanilla data dir(s) "
        f"and {len(_VANILLA_MASTERS)} vanilla plugin(s)."
    )

"""Match BG3 Nexus requirements against installed .pak UUIDs.

Nexus lists a mod's requirements by *Nexus mod id*, so a fork of a required
mod (a separate Nexus page with its own id, e.g. "ImpUI P8 Fork" for "ImpUI
(ImprovedUI)") never satisfies the id check, even though the game is happy:
BG3 resolves dependencies by the module UUID in each pak's meta.lsx, and the
fork keeps the original's UUID.

A Nexus requirement only carries a name, so the name has to be turned into a
UUID first.  Installed paks supply that mapping themselves: every module's
own Name, plus every Name a meta.lsx gives in its Dependencies block (CPCCE
declares "ImpUI (ImprovedUI)" -> 26922ba9-...).  A requirement counts as
satisfied only when one of the UUIDs its name maps to is actually installed.
"""

from __future__ import annotations

import re
from pathlib import Path

from Utils.mods.modlist import ModEntry
from Utils.mods.modsettings import scan_mod_paks

BG3_NEXUS_DOMAIN = "baldursgate3"


def normalize_requirement_name(name: str) -> str:
    """Case- and punctuation-insensitive form used for name matching."""
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


def pak_satisfied_requirement_names(staging_root: Path,
                                    mod_names: "list[str] | None" = None
                                    ) -> set[str]:
    """Normalized requirement names satisfied by installed pak UUIDs.

    *mod_names* limits the scan to those staged mod folders (e.g. the enabled
    ones); None scans every folder under *staging_root*.
    """
    if mod_names is None:
        try:
            mod_names = sorted(p.name for p in Path(staging_root).iterdir()
                               if p.is_dir())
        except OSError:
            return set()
    entries = [ModEntry(name=n, enabled=True, locked=False) for n in mod_names]
    infos = scan_mod_paks(Path(staging_root), entries)

    installed = set(infos)
    name_to_uuids: dict[str, set[str]] = {}
    for uuid, info in infos.items():
        name_to_uuids.setdefault(
            normalize_requirement_name(info.name), set()).add(uuid)
        for dep_uuid, dep_name in info.dependency_names.items():
            name_to_uuids.setdefault(
                normalize_requirement_name(dep_name), set()).add(dep_uuid)

    return {name for name, uuids in name_to_uuids.items()
            if name and uuids & installed}


class LazyPakRequirementCheck:
    """Answers "is this requirement satisfied by an installed pak UUID?",
    scanning paks only on the first question (a full scan takes ~1 s), and
    only for BG3 — every other game always answers False."""

    def __init__(self, game_domain: str, staging_root: Path,
                 mod_names: "list[str] | None" = None):
        self._active = (game_domain or "").strip().lower() == BG3_NEXUS_DOMAIN
        self._staging_root = staging_root
        self._mod_names = mod_names
        self._names: "set[str] | None" = None

    def __call__(self, requirement_name: str) -> bool:
        if not self._active or not requirement_name:
            return False
        if self._names is None:
            try:
                self._names = pak_satisfied_requirement_names(
                    self._staging_root, self._mod_names)
            except Exception:
                self._names = set()
        return normalize_requirement_name(requirement_name) in self._names

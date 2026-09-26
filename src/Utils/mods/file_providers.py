"""Which enabled mods ship a given file, and where each copy lives on disk.

Backs the asset-compare view: when several mods provide the same path (a
texture from two texture packs, a TexGen output over an original) the preview
lets the user flip between, or line up, every copy.

Pure logic, no Qt. The mod index (``modindex.bin``) stores *post-strip* paths
— per-mod "ignore this folder" prefixes are already applied — so a provider's
real file is found by re-adding the strip prefixes and resolving each path
segment case-insensitively (the staging folders are on a case-sensitive
filesystem, while the index keys are lowercased).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping


@dataclass(frozen=True)
class Provider:
    mod_name: str
    rel_str: str                # deploy-relative path as stored in the index
    is_winner: bool             # this copy is the one the filemap deploys
    disk_path: "Path | None"    # real file, or None when it can't be located


def resolve_on_disk(mod_dir: "Path | None", rel_str: str,
                    strips: Iterable[str] = ()) -> "Path | None":
    """Locate *rel_str* under *mod_dir*, trying the mod's strip prefixes.

    Tries the path as stored first, then each ``<strip>/<path>`` (longest strip
    first). Every segment is matched case-insensitively. Returns None when no
    candidate is an existing file."""
    if mod_dir is None:
        return None
    rel = rel_str.replace("\\", "/").strip("/")
    if not rel:
        return None
    candidates = [rel]
    for s in sorted({s.strip("/") for s in strips if s.strip("/")},
                    key=len, reverse=True):
        candidates.append(f"{s}/{rel}")
    for cand in candidates:
        hit = _walk_ci(mod_dir, cand.split("/"))
        if hit is not None:
            return hit
    return None


def _walk_ci(base: Path, parts: list[str]) -> "Path | None":
    cur = base
    for part in parts:
        direct = cur / part
        if direct.exists():
            cur = direct
            continue
        want = part.lower()
        found = None
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.name.lower() == want:
                        found = Path(entry.path)
                        break
        except OSError:
            return None
        if found is None:
            return None
        cur = found
    return cur if cur.is_file() else None


def find_providers(
    full_index: "Mapping[str, tuple[Mapping[str, str], Mapping[str, str]]] | None",
    key: str,
    winner: "Mapping[str, str]",
    mod_order: Iterable[str],
    mod_dir_for: "Callable[[str], Path | None]",
    strips_for: "Callable[[str], Iterable[str]]" = lambda _m: (),
) -> list[Provider]:
    """Every mod in *mod_order* that ships the post-strip lowercase path *key*.

    *mod_order* lists the enabled mods, highest priority first. The mod the
    filemap says wins (``winner[key]``) is always returned first and flagged;
    the rest keep *mod_order*. Winner comes from the filemap rather than being
    recomputed here, so it can never disagree with what deploy actually does.
    Root-flagged files are ignored — they don't deploy into the data folder
    and so never compete with a data-folder file."""
    if not full_index:
        return []
    key = key.replace("\\", "/").lower()
    won_by = winner.get(key)
    found: list[Provider] = []
    for mod in mod_order:
        entry = full_index.get(mod)
        if not entry:
            continue
        rel_str = entry[0].get(key)
        if rel_str is None:
            continue
        found.append(Provider(
            mod_name=mod, rel_str=rel_str, is_winner=(mod == won_by),
            disk_path=resolve_on_disk(mod_dir_for(mod), rel_str, strips_for(mod)),
        ))
    found.sort(key=lambda p: not p.is_winner)   # stable: winner first, rest as-is
    return found

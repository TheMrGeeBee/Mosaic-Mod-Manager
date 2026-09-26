"""The layered view of every mesh and texture a game would load.

Models the override stack: the base game's archives form the bottom layer, then
each mod in load order (lowest priority first). A path that a later layer also
supplies overrides the earlier copy; a path only one layer supplies is simply
added. The catalog keeps *every* layer's copy so a viewer can list what each
mod ships, and flags which copy wins.

Two engine rules make "last mod wins" slightly more than a straight fold:

* a loose file beats a file inside any BSA, whatever the mods' order;
* BSAs are ordered by their plugin's load order, not the modlist's — that
  comes pre-computed in ``bsa_winner`` (``compute_bsa_winner_map``).

Pure logic over plain dicts, so it is testable without a game or a GUI. See
``catalog_loader`` for building one from Mosaic's on-disk indexes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from Utils.archives.bsa_file_reader import BsaFile, BsaReadError
from Utils.mods.file_providers import resolve_on_disk

BASE = ""          # AssetEntry.mod for the base game layer


def norm_key(path: str) -> str:
    return path.replace("\\", "/").lstrip("/").lower()


def is_viewable(key: str) -> bool:
    """Meshes (.nif under meshes/) and textures (.dds under textures/)."""
    return ((key.startswith("meshes/") and key.endswith(".nif"))
            or (key.startswith("textures/") and key.endswith(".dds")))


@dataclass(frozen=True)
class AssetEntry:
    path: str            # normalised: lowercase, forward slashes
    mod: str             # mod name, or BASE for the base game
    kind: str            # "loose" | "bsa"
    archive: str = ""    # BSA key (file name) for kind == "bsa"
    is_winner: bool = True

    @property
    def source_label(self) -> str:
        return self.archive if self.kind == "bsa" else ""


class AssetCatalog:
    def __init__(
        self, *,
        base_name: str,
        base_archives: Iterable[Path],
        mod_order: Iterable[str],
        loose: "Mapping[str, Mapping[str, str]]",
        bsas: "Mapping[str, list[tuple[str, Iterable[str]]]]",
        loose_winner: Mapping[str, str],
        bsa_winner: Mapping[str, str],
        mod_dir_for: Callable[[str], "Path | None"],
        strips_for: Callable[[str], Iterable[str]] = lambda _m: (),
    ):
        """
        base_archives: the game's own BSAs, lowest priority first.
        mod_order:     enabled mods, LOWEST priority first (load order).
        loose:         {mod: {rel_key: rel_str}} — viewable files only.
        bsas:          {mod: [(archive_key, paths)]} — viewable paths only.
        loose_winner:  rel_key → winning mod for loose files (filemap.txt).
        bsa_winner:    rel_key → winning mod among BSAs (engine plugin order).
        """
        self.base_name = base_name
        self._base_archives = list(base_archives)
        self.mod_order = list(mod_order)
        self._loose = {m: dict(v) for m, v in loose.items()}
        self._bsas = {m: [(a, frozenset(norm_key(p) for p in ps)) for a, ps in v]
                      for m, v in bsas.items()}
        self._loose_winner = loose_winner
        self._bsa_winner = bsa_winner
        self._mod_dir_for = mod_dir_for
        self._strips_for = strips_for
        self._lock = threading.Lock()          # guards the open-archive cache
        self._base_lock = threading.Lock()     # guards the lazy base map (may call _archive)
        self._open: dict[Path, "BsaFile | None"] = {}
        self._base_files: "dict[str, Path] | None" = None
        self._mod_keys: "set[str] | None" = None

    # -- mods ---------------------------------------------------------------------
    def mods(self) -> list[str]:
        """Mods that ship at least one viewable file, lowest priority first."""
        return [m for m in self.mod_order if self._loose.get(m) or self._bsas.get(m)]

    def _mod_key_set(self) -> set[str]:
        if self._mod_keys is None:
            keys: set[str] = set()
            for m in self.mod_order:
                keys.update(self._loose.get(m, ()))
                for _a, ps in self._bsas.get(m, ()):
                    keys.update(ps)
            self._mod_keys = keys
        return self._mod_keys

    def _mod_winner(self, key: str) -> "tuple[str, str, str] | None":
        """(mod, kind, archive) of the winning MOD copy, or None if only the
        base game has it. Loose beats BSA."""
        m = self._loose_winner.get(key)
        if m is not None and key in self._loose.get(m, ()):
            return (m, "loose", "")
        m = self._bsa_winner.get(key)
        if m is not None:
            # Highest-priority archive of that mod holding the key.
            for arch, ps in reversed(self._bsas.get(m, [])):
                if key in ps:
                    return (m, "bsa", arch)
        # Filemap/BSA maps can disagree with the index (stale index): fall back
        # to the highest-priority mod that has any copy.
        for m in reversed(self.mod_order):
            if key in self._loose.get(m, ()):
                return (m, "loose", "")
        for m in reversed(self.mod_order):
            for arch, ps in reversed(self._bsas.get(m, [])):
                if key in ps:
                    return (m, "bsa", arch)
        return None

    def mod_entries(self, mod: str) -> list[AssetEntry]:
        out: list[AssetEntry] = []
        for key in self._loose.get(mod, ()):
            w = self._mod_winner(key)
            out.append(AssetEntry(key, mod, "loose", "", w == (mod, "loose", "")))
        for arch, ps in self._bsas.get(mod, []):
            for key in ps:
                w = self._mod_winner(key)
                out.append(AssetEntry(key, mod, "bsa", arch, w == (mod, "bsa", arch)))
        return out

    # -- base game ----------------------------------------------------------------
    def _base_map(self) -> dict[str, Path]:
        with self._base_lock:
            if self._base_files is None:
                files: dict[str, Path] = {}
                for arch in self._base_archives:       # later archives override
                    bsa = self._archive(arch)
                    if bsa is None:
                        continue
                    for p in bsa.paths():
                        if is_viewable(p):
                            files[p] = arch
                self._base_files = files
            return self._base_files

    def base_entries(self) -> list[AssetEntry]:
        """The base game's meshes and textures (reads the archives' tables of
        contents on first use). ``is_winner`` is False where a mod overrides."""
        mod_keys = self._mod_key_set()
        return [AssetEntry(k, BASE, "bsa", arch.name, k not in mod_keys)
                for k, arch in self._base_map().items()]

    # -- resolution ---------------------------------------------------------------
    def resolve(self, path: str) -> "AssetEntry | None":
        """The copy of *path* the game would load, or None if nothing has it."""
        key = norm_key(path)
        w = self._mod_winner(key)
        if w is not None:
            return AssetEntry(key, w[0], w[1], w[2], True)
        arch = self._base_map().get(key)
        if arch is not None:
            return AssetEntry(key, BASE, "bsa", arch.name, True)
        return None

    def read(self, entry: AssetEntry) -> bytes:
        """Bytes of *entry*'s file. Raises OSError / BsaReadError if unreadable."""
        if entry.kind == "loose":
            rel = self._loose.get(entry.mod, {}).get(entry.path, entry.path)
            disk = resolve_on_disk(self._mod_dir_for(entry.mod), rel,
                                   self._strips_for(entry.mod))
            if disk is None:
                raise FileNotFoundError(f"{entry.path} is missing from {entry.mod}")
            return disk.read_bytes()
        if entry.mod == BASE:
            arch = next((a for a in self._base_archives if a.name == entry.archive), None)
        else:
            mod_dir = self._mod_dir_for(entry.mod)
            arch = (mod_dir / entry.archive) if mod_dir is not None else None
        bsa = self._archive(arch) if arch is not None else None
        if bsa is None:
            raise BsaReadError(f"cannot open {entry.archive or '?'} for {entry.path}")
        return bsa.read(entry.path)

    def _archive(self, path: Path) -> "BsaFile | None":
        with self._lock:
            if path not in self._open:
                try:
                    self._open[path] = BsaFile(path)
                except BsaReadError:
                    self._open[path] = None
            return self._open[path]

    def close(self):
        with self._lock:
            for b in self._open.values():
                if b is not None:
                    b.close()
            self._open.clear()

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
from Utils.nif.nif_reader import NifError, format_label, read_body_slots, sniff_nif_format

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
        expected_nif_format: "tuple[int, int] | None" = None,
        authoritative_slots: "Mapping[str, frozenset] | None" = None,
        slots_need_authority: bool = False,
    ):
        """
        base_archives: the game's own BSAs, lowest priority first.
        mod_order:     enabled mods, LOWEST priority first (load order).
        loose:         {mod: {rel_key: rel_str}} — viewable files only.
        bsas:          {mod: [(archive_key, paths)]} — viewable paths only.
        loose_winner:  rel_key → winning mod for loose files (filemap.txt).
        bsa_winner:    rel_key → winning mod among BSAs (engine plugin order).
        expected_nif_format: the game's (NIF version, BS version); meshes in any
                       other format are reported as incompatible by scan_formats().
        authoritative_slots: {mesh rel_key: slots} straight from the active
                       plugins' own ARMA records (Utils.plugins.armor_records) —
                       ground truth where a mesh is known, taking priority over
                       slots_of()'s mesh-partition guess.
        slots_need_authority: when a mesh isn't in authoritative_slots, return
                       None (unknown) from slots_of() instead of falling back
                       to the mesh's own partition guess. Skyrim's mesh
                       partitions are reliable enough to guess from (the
                       default, False); Fallout 4's dismemberment segments are
                       not — verified on a real mesh whose own segments claim
                       slot 60 (Pip-Boy) alongside meaningless small "tier"
                       numbers, which put an unrelated full outfit in the
                       Pip-Boy picker. catalog_loader sets this True for
                       Fallout 4, so an un-authored mesh is simply excluded
                       from every slot picker rather than mis-sorted into one.
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
        self._contested: "frozenset[str] | None" = None
        self._expected_format = expected_nif_format
        self._authoritative_slots = dict(authoritative_slots or {})
        self._slots_need_authority = slots_need_authority
        self._bad: dict[tuple, str] = {}       # entry key → format label
        self._slots: dict[tuple, "frozenset | None"] = {}   # entry key → body slots (None: unreadable)

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

    def contested_keys(self) -> frozenset[str]:
        """Paths that more than one layer provides — the base game plus every mod
        counts once each, however many of its own archives hold the file. These
        are the files where an override happens. Reads the base game's archive
        tables on first use."""
        if self._contested is None:
            count: dict[str, int] = {}
            for m in self.mod_order:
                keys = set(self._loose.get(m, ()))
                for _a, ps in self._bsas.get(m, ()):
                    keys.update(ps)
                for k in keys:
                    count[k] = count.get(k, 0) + 1
            for k in self._base_map():
                count[k] = count.get(k, 0) + 1
            self._contested = frozenset(k for k, c in count.items() if c >= 2)
        return self._contested

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

    def resolve_readable(self, path: str) -> "AssetEntry | None":
        """Like resolve(), but a winning copy that turns out to be unreadable —
        a mod's index disagreeing with what's actually on disk, e.g. a mod
        shipping its files under an unstripped wrapper folder its own install
        never flattened (verified on a real profile: a face-texture mod's
        index claimed a path it ships only under an extra ``Data/`` prefix,
        so the real game engine would silently fall through to the base
        game's own copy of that exact texture, which is right there and
        reads fine) — is skipped in favour of the next lower-priority
        provider of the same path, all the way down to the base game. This is
        exactly what the engine itself does for a loose override that isn't
        really on disk; it never happens for a genuinely present file, so the
        extra read attempts are rare in practice.

        Keeps the same "loose beats bsa, whatever the mod order" engine rule
        resolve() itself follows: every mod's loose copy is tried (highest
        priority first) before any mod's bsa copy, not just a per-mod walk —
        otherwise a lower-priority mod's bsa copy could jump ahead of a
        higher-priority mod's (also broken) loose copy."""
        key = norm_key(path)
        tried: set = set()

        def try_entry(e: "AssetEntry | None") -> "AssetEntry | None":
            if e is None:
                return None
            ident = (e.mod, e.kind, e.archive)
            if ident in tried:
                return None
            tried.add(ident)
            try:
                self.read(e)
            except (OSError, BsaReadError):
                return None
            return e

        found = try_entry(self.resolve(path))
        if found is not None:
            return found
        for m in reversed(self.mod_order):
            if key in self._loose.get(m, ()):
                found = try_entry(AssetEntry(key, m, "loose", "", False))
                if found is not None:
                    return found
        for m in reversed(self.mod_order):
            for arch, ps in reversed(self._bsas.get(m, [])):
                if key in ps:
                    found = try_entry(AssetEntry(key, m, "bsa", arch, False))
                    if found is not None:
                        return found
        arch = self._base_map().get(key)
        if arch is not None:
            found = try_entry(AssetEntry(key, BASE, "bsa", arch.name, False))
            if found is not None:
                return found
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

    def entry_in_layer(self, mod: str, path: str) -> "AssetEntry | None":
        """*mod*'s own copy of *path* (BASE for the base game) — the file that
        layer ships, whether or not it wins — or None if that layer lacks it."""
        key = norm_key(path)
        if mod == BASE:
            arch = self._base_map().get(key)
            if arch is None:
                return None
            return AssetEntry(key, BASE, "bsa", arch.name, self._mod_winner(key) is None)
        if key in self._loose.get(mod, ()):
            return AssetEntry(key, mod, "loose", "", self._mod_winner(key) == (mod, "loose", ""))
        for arch, ps in reversed(self._bsas.get(mod, [])):
            if key in ps:
                return AssetEntry(key, mod, "bsa", arch, self._mod_winner(key) == (mod, "bsa", arch))
        return None

    def slots_of(self, entry: AssetEntry) -> "frozenset | None":
        """Body slots the mesh *entry* covers (empty for props/unskinned models),
        or None if it can't be read (or, when slots_need_authority is set and
        no plugin covers this exact path, unknown by design — see that flag).
        An active plugin's own ARMA record for this exact path is ground truth
        and is used whenever there is one; otherwise falls back to the mesh's
        own partitions (read_body_slots), unless slots_need_authority says not
        to. Cached — the first call reads the file (skipped entirely when the
        authoritative map already has an answer, or is required and absent)."""
        auth = self._authoritative_slots.get(entry.path)
        if auth is not None:
            return auth
        if self._slots_need_authority:
            return None
        key = self._ekey(entry)
        if key not in self._slots:
            try:
                self._slots[key] = read_body_slots(self.read(entry))
            except (NifError, BsaReadError, OSError):
                self._slots[key] = None
        return self._slots[key]

    def siblings(self, entry: AssetEntry) -> list[AssetEntry]:
        """Other files in the same folder from the same layer (base game or mod),
        sorted by path — e.g. the worn versions of an item's display model."""
        folder = entry.path.rsplit("/", 1)[0]
        pool = self.base_entries() if entry.mod == BASE else self.mod_entries(entry.mod)
        return sorted((e for e in pool
                       if e.path != entry.path and e.path.rsplit("/", 1)[0] == folder),
                      key=lambda e: e.path)

    # -- format compatibility -----------------------------------------------------------
    @staticmethod
    def _ekey(entry: AssetEntry) -> tuple:
        return (entry.mod, entry.kind, entry.archive, entry.path)

    def incompatible_label(self, entry: AssetEntry) -> "str | None":
        """Name of the NIF format (e.g. "Skyrim LE") if *entry* is a mesh in a
        format other than the game's — known from scan_formats() or from having
        been opened; None if compatible or not yet checked."""
        return self._bad.get(self._ekey(entry))

    def mark_incompatible(self, entry: AssetEntry, label: str):
        self._bad[self._ekey(entry)] = label

    def incompatible_count(self) -> int:
        return len(self._bad)

    def read_head(self, entry: AssetEntry, n: int = 128) -> bytes:
        """The first *n* bytes of *entry*'s file (cheap for archived files)."""
        return self._head(self._ekey(entry), n)

    def _head(self, key: tuple, n: int) -> bytes:
        mod, kind, archive, path = key
        if kind == "loose":
            rel = self._loose.get(mod, {}).get(path, path)
            disk = resolve_on_disk(self._mod_dir_for(mod), rel, self._strips_for(mod))
            if disk is None:
                return b""
            with disk.open("rb") as f:
                return f.read(n)
        mod_dir = self._mod_dir_for(mod) if mod != BASE else None
        arch = (mod_dir / archive) if mod_dir is not None else next(
            (a for a in self._base_archives if a.name == archive), None)
        bsa = self._archive(arch) if arch is not None else None
        return bsa.read_head(path, n) if bsa is not None else b""

    def scan_formats(self, progress: "Callable[[int, int], None] | None" = None,
                     cancel: "Callable[[], bool] | None" = None) -> int:
        """Classify every mesh the MODS provide by sniffing its header, marking
        those not in the game's format. (The base game's own files are trusted.)
        Returns how many meshes are incompatible. Safe to run on a worker thread;
        *progress* gets (done, total) every few hundred files."""
        exp = self._expected_format
        if exp is None:
            return 0
        work: list[tuple] = []
        for m in self.mod_order:
            work += [(m, "loose", "", k) for k in self._loose.get(m, ()) if k.endswith(".nif")]
            for arch, ps in self._bsas.get(m, []):
                work += [(m, "bsa", arch, k) for k in ps if k.endswith(".nif")]
        work.sort(key=lambda t: (t[0], t[2]))          # keep each archive's reads together
        total = len(work)
        for i, key in enumerate(work):
            if cancel is not None and cancel():
                break
            if progress is not None and i % 250 == 0:
                progress(i, total)
            try:
                fmt = sniff_nif_format(self._head(key, 128))
            except (OSError, BsaReadError):
                continue
            if fmt is not None and fmt != exp:
                self._bad[key] = format_label(*fmt)
        if progress is not None:
            progress(total, total)
        return len(self._bad)

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

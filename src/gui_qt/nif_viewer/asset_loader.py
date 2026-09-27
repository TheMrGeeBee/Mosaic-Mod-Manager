"""Loading helpers shared by the NIF Viewer and Character tabs: decoded-texture
cache, the game's winning copy of a mesh, and skeletons. Safe to call from
worker threads."""

from __future__ import annotations

import threading

from Utils.archives.bsa_file_reader import BsaFile, BsaReadError  # noqa: F401  (BsaReadError re-exported for callers)
from Utils.nif.asset_catalog import AssetCatalog, AssetEntry
from Utils.nif.character import SKELETONS
from Utils.nif.nif_reader import NifError, NifScene, read_nif
from gui_qt.image_preview import load_qimage_bytes

_IMG_CACHE_MAX = 300
_NIF_CACHE_MAX = 96


class AssetLoader:
    def __init__(self):
        self._img_cache: dict[str, object] = {}      # texture path → decoded QImage (or None)
        self._skel_cache: dict[str, object] = {}     # gender → skeleton nodes (or None)
        self._nif_cache: dict[tuple, "NifScene | None"] = {}   # entry key → parsed scene
        self._lock = threading.Lock()

    def image(self, cat: AssetCatalog, path: str):
        """The decoded QImage of texture *path* (through the catalog's winners), or None."""
        with self._lock:
            if path in self._img_cache:
                return self._img_cache[path]
        e = cat.resolve(path)
        try:
            img = load_qimage_bytes(cat.read(e)) if e else None
        except (OSError, BsaReadError):
            img = None
        with self._lock:
            self._img_cache[path] = img
            while len(self._img_cache) > _IMG_CACHE_MAX:
                self._img_cache.pop(next(iter(self._img_cache)))
        return img

    def nif(self, cat: AssetCatalog, path: str, include_nodes: bool = False) -> "NifScene | None":
        """Parse the game's winning copy of *path*; None if it isn't there or unreadable."""
        e = cat.resolve(path)
        if e is None:
            return None
        try:
            return read_nif(cat.read(e), include_nodes=include_nodes)
        except (NifError, BsaReadError, OSError):
            return None

    def nif_entry(self, cat: AssetCatalog, entry: "AssetEntry | None") -> "NifScene | None":
        """Parse *entry* (that exact copy), cached: dragging the weight slider
        rebuilds the character many times over the same few files. The scenes are
        never modified in place (blending/skinning/hiding all return new shapes),
        so sharing them is safe."""
        if entry is None:
            return None
        key = (entry.mod, entry.kind, entry.archive, entry.path)
        with self._lock:
            if key in self._nif_cache:
                return self._nif_cache[key]
        try:
            scene = read_nif(cat.read(entry))
        except (NifError, BsaReadError, OSError):
            scene = None
        with self._lock:
            self._nif_cache[key] = scene
            while len(self._nif_cache) > _NIF_CACHE_MAX:
                self._nif_cache.pop(next(iter(self._nif_cache)))
        return scene

    def skeleton(self, cat: AssetCatalog, gender: str):
        """The game's skeleton nodes for *gender* (cached), or None."""
        with self._lock:
            if gender in self._skel_cache:
                return self._skel_cache[gender]
        sc = self.nif(cat, SKELETONS[gender], include_nodes=True)
        nodes = sc.nodes if sc is not None and sc.nodes else None
        with self._lock:
            self._skel_cache[gender] = nodes
        return nodes

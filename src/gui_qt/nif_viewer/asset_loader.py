"""Loading helpers shared by the NIF Viewer and Character tabs: decoded-texture
cache, the game's winning copy of a mesh, and skeletons. Safe to call from
worker threads."""

from __future__ import annotations

import threading
from dataclasses import replace

from Utils.archives.bsa_file_reader import BsaFile, BsaReadError  # noqa: F401  (BsaReadError re-exported for callers)
from Utils.nif.asset_catalog import AssetCatalog, AssetEntry
from Utils.nif.character import SKELETONS as _DEFAULT_SKELETONS
from Utils.nif.material_reader import MaterialTextures, read_material
from Utils.nif.nif_reader import NifError, NifScene, read_nif
from gui_qt.image_preview import load_qimage_bytes

_IMG_CACHE_MAX = 300
_NIF_CACHE_MAX = 96
_MAT_CACHE_MAX = 300


class AssetLoader:
    def __init__(self):
        self._img_cache: dict[str, object] = {}      # texture path → decoded QImage (or None)
        self._skel_cache: dict[str, object] = {}     # gender → skeleton nodes (or None)
        self._nif_cache: dict[tuple, "NifScene | None"] = {}   # entry key → parsed scene
        self._mat_cache: dict[str, "MaterialTextures | None"] = {}   # material path → parsed textures
        self._lock = threading.Lock()

    def _material(self, cat: AssetCatalog, name: str) -> "MaterialTextures | None":
        """*name*'s (a shape's material_name) parsed textures, cached — the
        same .bgsm is typically shared by many shapes (a mod's whole line of
        colour/camo variants), so this is read once, not once per shape."""
        with self._lock:
            if name in self._mat_cache:
                return self._mat_cache[name]
        e = cat.resolve_readable(name)
        try:
            mt = read_material(cat.read(e)) if e else None
        except (OSError, BsaReadError):
            mt = None
        with self._lock:
            self._mat_cache[name] = mt
            while len(self._mat_cache) > _MAT_CACHE_MAX:
                self._mat_cache.pop(next(iter(self._mat_cache)))
        return mt

    def apply_materials(self, cat: AssetCatalog, scene: "NifScene | None") -> "NifScene | None":
        """*scene* with every shape's diffuse slot replaced by its own
        material_name's diffuse, when it has one — verified on real Fallout 4
        armor (CROSS Collection): a shape with a material reference always
        has this take priority over its own embedded BSShaderTextureSet, the
        same way the real engine does, even when that embedded set isn't
        empty — real files were found with a stray normal-map path sitting in
        the (would-be) diffuse slot, a leftover from the authoring tool that
        the engine never actually reads once a material is referenced. Only
        the diffuse slot (index 0) is touched — it's the only one anything in
        this viewer reads (see character_view.py/nif_viewer_view.py's own
        `sh.textures[0]`). A material with no diffuse of its own (a handful
        of real decal/neon materials, verified) leaves the shape's embedded
        value alone rather than blanking it.

        Public: every direct `read_nif()` call site in the NIF Viewer/
        Character tab must route its scene through this before displaying it
        — a real gap found live (`nif_viewer_view.py`'s main selected-mesh
        scene used to call `read_nif()` straight, bypassing this entirely,
        so any mesh relying on a material showed no texture there even
        though the Character tab's equivalent path was already fixed)."""
        if scene is None:
            return None
        changed = False
        shapes = []
        for sh in scene.shapes:
            if sh.material_name:
                mt = self._material(cat, sh.material_name)
                if mt is not None and mt.diffuse:
                    textures = [mt.diffuse, *sh.textures[1:]] if sh.textures else [mt.diffuse]
                    sh = replace(sh, textures=textures)
                    changed = True
            shapes.append(sh)
        return NifScene(shapes, scene.nodes) if changed else scene

    def image(self, cat: AssetCatalog, path: str):
        """The decoded QImage of texture *path* (through the catalog's winners,
        falling back to a lower-priority provider — down to the base game —
        if the winner's own file turns out not to really be there, e.g. a
        mod's index disagreeing with what it actually shipped on disk), or
        None."""
        with self._lock:
            if path in self._img_cache:
                return self._img_cache[path]
        e = cat.resolve_readable(path)
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
            scene = read_nif(cat.read(e), include_nodes=include_nodes)
        except (NifError, BsaReadError, OSError):
            return None
        return self.apply_materials(cat, scene)

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
            scene = self.apply_materials(cat, read_nif(cat.read(entry)))
        except (NifError, BsaReadError, OSError):
            scene = None
        with self._lock:
            self._nif_cache[key] = scene
            while len(self._nif_cache) > _NIF_CACHE_MAX:
                self._nif_cache.pop(next(iter(self._nif_cache)))
        return scene

    def skeleton(self, cat: AssetCatalog, gender: str, skeletons: dict = _DEFAULT_SKELETONS):
        """The game's skeleton nodes for *gender* (cached), or None. *skeletons*
        is a GameProfile.skeletons dict — Skyrim's by default, so existing
        callers that don't pass one keep working unchanged."""
        with self._lock:
            if gender in self._skel_cache:
                return self._skel_cache[gender]
        sc = self.nif(cat, skeletons[gender], include_nodes=True)
        nodes = sc.nodes if sc is not None and sc.nodes else None
        with self._lock:
            self._skel_cache[gender] = nodes
        return nodes

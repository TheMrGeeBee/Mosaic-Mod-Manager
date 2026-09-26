"""NIF Viewer tab: a tree of every mesh and texture the game would load (base
game, then each mod in load order) beside a 3D / image viewer.

Layout (see the project's design sketch):

    ┌ tree ─────────────┬ [Search meshes and mods]  [Sources ▾]  [Wireframe] ┐
    │ Skyrim SE (base)  │                                                    │
    │  ▸ meshes …       │              render area                           │
    │ ModA              │                                                    │
    │  ▸ meshes …       ├────────────────────────────────────────────────────┤
    └───────────────────┴ file · shapes · triangles · texture status          ┘

The Sources dropdown picks what a selected mesh shows: the mesh alone, its
diffuse texture alone, or the mesh with its textures applied. A selected .dds
always shows the texture. A mesh's texture paths are looked up in the same
layered catalog, so it is drawn with the textures the game would load.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QSplitter, QStackedWidget, QTreeView, QVBoxLayout, QWidget,
)

from Utils.archives.bsa_file_reader import BsaReadError
from Utils.dds_info import parse_dds_info
from Utils.nif.asset_catalog import BASE, AssetCatalog, AssetEntry
from Utils.nif.catalog_loader import build_catalog
from Utils.nif.nif_reader import NifError, read_nif
from gui_qt.image_preview import _ImageCanvas, load_qimage_bytes
from gui_qt.nif_viewer.asset_tree import (
    AssetTreeDelegate, AssetTreeModel, EntryRole,
)
from gui_qt.nif_viewer.gl_viewport import SOLID, TEXTURED, WIRE, MeshViewport
from gui_qt.theme.theme_qt import _c, active_palette
from gui_qt.worker import run_in_worker

_PAGE_MESH, _PAGE_IMAGE, _PAGE_MESSAGE = 0, 1, 2
_SRC_MESH, _SRC_TEXTURE, _SRC_BOTH = "mesh", "texture", "both"


class NifViewerView(QWidget):
    _catalog_ready = Signal(object)
    _asset_ready = Signal(int, object)

    def __init__(self, game, profile_dir, staging_dir, parent=None):
        super().__init__(parent)
        self._catalog: "AssetCatalog | None" = None
        self._gen = 0
        self._entry: "AssetEntry | None" = None
        self._last: "dict | None" = None
        pal = active_palette()

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)

        # -- left: tree ---------------------------------------------------------------
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        self._tree_status = QLabel(self.tr("Indexing meshes and textures…"))
        self._tree_status.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')}; padding:8px;")
        self._tree_status.setWordWrap(True)
        lv.addWidget(self._tree_status)
        self._model = AssetTreeModel(self)
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setItemDelegate(AssetTreeDelegate(self._tree))
        self._tree.setEditTriggers(QTreeView.NoEditTriggers)
        self._tree.setAnimated(False)
        lv.addWidget(self._tree, 1)
        split.addWidget(left)

        # -- right ----------------------------------------------------------------------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        bar = QWidget()
        bar.setStyleSheet(f"background:{_c(pal, 'BG_HEADER')};")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(8, 6, 8, 6)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search meshes and mods"))
        self._search.setClearButtonEnabled(True)
        bh.addWidget(self._search, 1)
        self._only_over = QCheckBox(self.tr("Only overridden"))
        self._only_over.setToolTip(self.tr(
            "Show only files that another mod (or the base game) also provides — "
            "the files where an override happens"))
        bh.addWidget(self._only_over)
        self._sources = QComboBox()
        self._sources.setToolTip(self.tr("What a selected mesh shows"))
        self._sources.addItem(self.tr("Mesh (.nif)"), _SRC_MESH)
        self._sources.addItem(self.tr("Textures (.dds)"), _SRC_TEXTURE)
        self._sources.addItem(self.tr("Mesh + textures (in game)"), _SRC_BOTH)
        self._sources.setCurrentIndex(2)
        bh.addWidget(self._sources)
        self._wire = QPushButton(self.tr("Wireframe"))
        self._wire.setCheckable(True)
        self._wire.setToolTip(self.tr("Draw the mesh as wireframe"))
        self._wire.setStyleSheet(
            f"QPushButton:checked {{ background:{_c(pal, 'ACCENT')};"
            f" color:{_c(pal, 'TEXT_ON_ACCENT')}; }}")
        bh.addWidget(self._wire)
        rv.addWidget(bar)

        self._stack = QStackedWidget()
        self._viewport = MeshViewport()
        self._canvas = _ImageCanvas()
        self._canvas.setStyleSheet(f"background:{_c(pal, 'BG_DEEP')};")
        self._message = QLabel()
        self._message.setAlignment(Qt.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')}; padding:24px;")
        for w in (self._viewport, self._canvas, self._message):
            self._stack.addWidget(w)
        rv.addWidget(self._stack, 1)
        self._info = QLabel()
        self._info.setStyleSheet(
            f"background:{_c(pal, 'BG_HEADER')}; color:{_c(pal, 'TEXT_MAIN')}; padding:4px 10px;")
        self._info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        rv.addWidget(self._info)
        split.addWidget(right)
        split.setSizes([380, 900])
        split.setStretchFactor(1, 1)
        self._show_message(self.tr("Select a mesh (.nif) or texture (.dds) on the left.\n\n"
                                   "Drag to rotate · right-drag to pan · scroll to zoom · "
                                   "double-click to re-frame."))

        # -- wiring -------------------------------------------------------------------------
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_search)
        self._search.textChanged.connect(lambda _t: self._search_timer.start())
        self._tree.selectionModel().currentChanged.connect(self._on_current_changed)
        self._only_over.toggled.connect(self._on_only_overridden)
        self._sources.currentIndexChanged.connect(self._on_source_changed)
        self._wire.toggled.connect(lambda _on: self._apply_mode())
        self._catalog_ready.connect(self._on_catalog_ready)
        self._asset_ready.connect(self._on_asset_ready)
        self.destroyed.connect(lambda *_: self._close_catalog())

        run_in_worker(lambda: build_catalog(game, profile_dir, staging_dir),
                      self._catalog_ready, name="nif-viewer-catalog")

    # -- catalog ---------------------------------------------------------------------------
    def _close_catalog(self):
        cat, self._catalog = self._catalog, None
        if cat is not None:
            cat.close()

    def _on_catalog_ready(self, cat):
        if cat is None:
            self._tree_status.setText(self.tr("Could not index the game's meshes and textures."))
            return
        self._catalog = cat
        self._model.set_catalog(cat)
        n = len(cat.mods())
        self._tree_status.setText(
            self.tr("{0} + {1} mod(s) with meshes or textures").format(cat.base_name, n))
        self._tree.expand(self._model.index(0, 0))          # base game first, lazily

    def _apply_search(self):
        if self._catalog is not None:
            self._model.set_filter(self._search.text())
            self._after_filter()

    def _on_only_overridden(self, on: bool):
        if self._catalog is not None:
            self._model.set_only_overridden(on)
            self._after_filter()

    def _after_filter(self):
        """Status line and expansion after the tree's filter changed."""
        m = self._model
        if not m.filtering():
            self._tree_status.setText(self.tr("{0} + {1} mod(s) with meshes or textures")
                                      .format(self._catalog.base_name, len(self._catalog.mods())))
            return
        found = m.match_count()
        self._tree_status.setText(
            self.tr("{0} overridden file(s)").format(found)
            if self._only_over.isChecked() and not self._search.text().strip()
            else self.tr("{0} match(es)").format(found))
        if 0 < found <= 3000:
            self._tree.expandAll()
        else:
            for r in range(m.rowCount()):
                self._tree.expand(m.index(r, 0))

    # -- selection ----------------------------------------------------------------------------
    def _on_current_changed(self, current: QModelIndex, _prev):
        entry = current.data(EntryRole) if current.isValid() else None
        if entry is not None:
            self._load(entry)

    def _source(self) -> str:
        return self._sources.currentData()

    def _needs_textures(self) -> bool:
        return self._source() in (_SRC_TEXTURE, _SRC_BOTH)

    def _load(self, entry: AssetEntry):
        cat = self._catalog
        if cat is None:
            return
        self._gen += 1
        gen = self._gen
        self._entry, self._last = entry, None
        self._info.setText(self.tr("Loading {0}…").format(entry.path.rsplit("/", 1)[-1]))
        want_tex = self._needs_textures()

        def job():
            try:
                data = cat.read(entry)
                if entry.path.endswith(".dds"):
                    info = parse_dds_info(data[:148], len(data))
                    return gen, {"kind": "texture", "image": load_qimage_bytes(data),
                                 "info": info.summary() if info else ""}
                scene = read_nif(data)
                images: dict[int, object] = {}
                missing: list[str] = []
                if want_tex:
                    cache: dict[str, object] = {}
                    for i, sh in enumerate(scene.shapes):
                        path = sh.textures[0] if sh.textures else ""
                        if not path or sh.is_effect:
                            continue
                        if path not in cache:
                            e = cat.resolve(path)
                            try:
                                cache[path] = load_qimage_bytes(cat.read(e)) if e else None
                            except (OSError, BsaReadError):
                                cache[path] = None
                        if cache[path] is not None:
                            images[i] = cache[path]
                        else:
                            missing.append(path)
                return gen, {"kind": "mesh", "scene": scene, "images": images,
                             "missing": missing, "textures_loaded": want_tex}
            except (NifError, BsaReadError, OSError) as exc:
                return gen, {"kind": "error", "text": str(exc)}

        run_in_worker(job, self._asset_ready, unpack=True, name="nif-viewer-load",
                      error_result=(gen, {"kind": "error", "text": self.tr("Unexpected error")}))

    def _on_asset_ready(self, gen: int, res: dict):
        if gen != self._gen:
            return                                   # a newer selection superseded this
        kind = res["kind"]
        if kind == "error":
            self._show_message(res["text"])
            self._info.setText("")
        elif kind == "texture":
            self._show_image(res["image"])
            self._info.setText(self._describe(res["info"]))
        else:
            self._last = res
            self._show_mesh()

    def _describe(self, tail: str = "") -> str:
        e = self._entry
        if e is None:
            return ""
        owner = self._catalog.base_name if e.mod == BASE else e.mod
        where = e.archive if e.kind == "bsa" else "loose"
        head = f"{e.path.rsplit('/', 1)[-1]} · {owner} ({where})"
        return f"{head} · {tail}" if tail else head

    # -- rendering -------------------------------------------------------------------------------
    def _show_message(self, text: str):
        self._message.setText(text)
        self._stack.setCurrentIndex(_PAGE_MESSAGE)

    def _show_image(self, image):
        if image is None or image.isNull():
            self._show_message(self.tr("This texture could not be decoded."))
            return
        from PySide6.QtGui import QPixmap
        self._canvas.set_image(QPixmap.fromImage(image))
        self._stack.setCurrentIndex(_PAGE_IMAGE)

    def _show_mesh(self):
        res = self._last
        if res is None:
            return
        scene = res["scene"]
        tris = sum(len(s.indices) // 3 for s in scene.shapes if not s.is_effect)
        parts = [self.tr("{0} shape(s)").format(len(scene.shapes)),
                 self.tr("{0:,} triangles").format(tris)]
        if res["textures_loaded"]:
            miss = sorted(set(res["missing"]))
            parts.append(self.tr("textures: {0} found").format(len(res["images"]))
                         + (self.tr(", {0} missing").format(len(miss)) if miss else ""))
        info = " · ".join(parts)
        if not scene.shapes:
            self._show_message(self.tr("This file has no drawable geometry "
                                       "(skeleton, animation or collision only)."))
            self._info.setText(self._describe(info))
            return
        src = self._source()
        if src == _SRC_TEXTURE:
            first = next((res["images"][i] for i in sorted(res["images"])), None)
            if first is None:
                self._show_message(self.tr("This mesh has no diffuse texture to show."))
            else:
                self._show_image(first)
        else:
            images = res["images"] if src == _SRC_BOTH else {}
            index = {id(s): i for i, s in enumerate(scene.shapes)}
            self._viewport.set_scene(scene, lambda sh: images.get(index[id(sh)]))
            self._apply_mode()
            self._stack.setCurrentIndex(_PAGE_MESH)
            QTimer.singleShot(400, self._check_gl)
        self._info.setText(self._describe(info))

    def _apply_mode(self):
        if self._wire.isChecked():
            self._viewport.set_mode(WIRE)
        else:
            self._viewport.set_mode(TEXTURED if self._source() == _SRC_BOTH else SOLID)

    def _check_gl(self):
        if self._viewport.error() and self._stack.currentIndex() == _PAGE_MESH:
            self._show_message(self.tr("3D view unavailable: {0}").format(self._viewport.error()))

    def _on_source_changed(self, _i):
        if self._entry is None or self._entry.path.endswith(".dds"):
            return
        if self._last is not None and (not self._needs_textures() or self._last["textures_loaded"]):
            self._show_mesh()
        else:
            self._load(self._entry)                   # textures weren't fetched yet

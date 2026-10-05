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
    QCheckBox, QComboBox, QMenu, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QSplitter, QStackedWidget, QTreeView, QVBoxLayout, QWidget,
)

from Utils.archives.bsa_file_reader import BsaReadError
from Utils.dds_info import parse_dds_info
from Utils.nif.asset_catalog import BASE, AssetCatalog, AssetEntry
from Utils.nif.catalog_loader import build_catalog
from Utils.nif.character import (
    auto_gender, body_paths, compose, detect_gender, detect_weight, guess_gender, profile_for_game,
)
from Utils.nif.nif_reader import (
    NifError, NifUnsupported, format_label, read_nif, version_string,
)
from gui_qt.image_preview import _ImageCanvas, load_qimage_bytes
from gui_qt.nif_viewer.asset_loader import AssetLoader
from gui_qt.nif_viewer.asset_tree import (
    AssetTreeDelegate, AssetTreeModel, EntryRole,
)
from gui_qt.nif_viewer.gl_viewport import SOLID, TEXTURED, WIRE, MeshViewport
from gui_qt.nif_viewer.record_info_card import RecordInfoCard
from gui_qt.theme.theme_qt import _c, active_palette
from gui_qt.safe_emit import safe_emit
from gui_qt.worker import run_in_worker

_PAGE_MESH, _PAGE_IMAGE, _PAGE_MESSAGE = 0, 1, 2
_SRC_MESH, _SRC_TEXTURE, _SRC_BOTH = "mesh", "texture", "both"
_BODY_AUTO, _BODY_FEMALE, _BODY_MALE, _BODY_NONE = "auto", "female", "male", "none"


class NifViewerView(QWidget):
    _catalog_ready = Signal(object)
    _asset_ready = Signal(int, object)
    _scan_progress = Signal(int, int)
    _scan_done = Signal(int)
    equip_requested = Signal(object)          # AssetEntry to put on the Character tab

    def __init__(self, game, profile_dir, staging_dir, parent=None):
        super().__init__(parent)
        self._profile = profile_for_game(getattr(game, "game_id", None))
        self._catalog: "AssetCatalog | None" = None
        self._gen = 0
        self._entry: "AssetEntry | None" = None
        self._last: "dict | None" = None
        self._scan_text = ""          # shown in the tree status while formats are being checked
        self._loader = AssetLoader()
        self._closing = False
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
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_menu)
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
        self._hide_bad = QCheckBox(self.tr("Hide incompatible"))
        self._hide_bad.setChecked(True)
        self._hide_bad.setToolTip(self.tr(
            "Hide meshes in another game's NIF format (for example Skyrim LE meshes "
            "in Skyrim SE — they can be viewed, but the game may not load them). "
            "Untick to see them, marked in amber."))
        bh.addWidget(self._hide_bad)
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

        opts = QWidget()
        opts.setStyleSheet(f"background:{_c(pal, 'BG_HEADER')};")
        oh = QHBoxLayout(opts)
        oh.setContentsMargins(8, 0, 8, 6)
        oh.addWidget(QLabel(self.tr("Body:")))
        self._body = QComboBox()
        self._body.setToolTip(self.tr(
            "Wear the selected armour or clothes on the game's own body (parts the "
            "armour covers are hidden, as in game). Auto picks a body for wearable "
            "gear from its path."))
        self._body.addItem(self.tr("Auto"), _BODY_AUTO)
        self._body.addItem(self.tr("Female"), _BODY_FEMALE)
        self._body.addItem(self.tr("Male"), _BODY_MALE)
        self._body.addItem(self.tr("None"), _BODY_NONE)
        oh.addWidget(self._body)
        self._skel = QCheckBox(self.tr("Skeleton"))
        self._skel.setToolTip(self.tr(
            "Draw the game's skeleton (its bones) over the mesh. Selecting a "
            "skeleton .nif shows its bones on their own."))
        oh.addWidget(self._skel)
        oh.addStretch(1)
        self._equip_btn = QPushButton(self.tr("Add to character"))
        self._equip_btn.setToolTip(self.tr(
            "Put the selected mesh on the Character tab, in the slot it belongs to "
            "(armour, clothing, hair…)"))
        self._equip_btn.setEnabled(False)
        self._equip_btn.setMinimumWidth(self._equip_btn.sizeHint().width() + 24)   # room for "✓ Added"
        self._equip_css = (f"QPushButton {{ background:{_c(pal, 'ACCENT')};"
                           f" color:{_c(pal, 'TEXT_ON_ACCENT')}; }}")
        self._equip_flash = QTimer(self)
        self._equip_flash.setSingleShot(True)
        self._equip_flash.setInterval(1400)
        self._equip_flash.timeout.connect(self._reset_equip_button)
        oh.addWidget(self._equip_btn)
        rv.addWidget(opts)

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
        self._record_card = RecordInfoCard(self._stack)
        self._record_card.attach(self._stack)
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
        self._hide_bad.toggled.connect(self._on_hide_bad)
        self._equip_btn.clicked.connect(self._on_equip_clicked)
        self._body.currentIndexChanged.connect(self._on_char_option)
        self._skel.toggled.connect(self._on_char_option)
        self._scan_progress.connect(self._on_scan_progress)
        self._scan_done.connect(self._on_scan_done)
        self._sources.currentIndexChanged.connect(self._on_source_changed)
        self._wire.toggled.connect(lambda _on: self._apply_mode())
        self._catalog_ready.connect(self._on_catalog_ready)
        self._asset_ready.connect(self._on_asset_ready)
        self.destroyed.connect(lambda *_: self._close_catalog())

        run_in_worker(lambda: build_catalog(game, profile_dir, staging_dir),
                      self._catalog_ready, name="nif-viewer-catalog")

    # -- catalog ---------------------------------------------------------------------------
    def _close_catalog(self):
        self._closing = True                       # stops a running format scan
        cat, self._catalog = self._catalog, None
        if cat is not None:
            cat.close()

    def _on_catalog_ready(self, cat):
        if cat is None:
            self._tree_status.setText(self.tr("Could not index the game's meshes and textures."))
            return
        self._catalog = cat
        self._model.set_catalog(cat)
        self._update_status()
        self._tree.expand(self._model.index(0, 0))          # base game first, lazily
        # Classify the mods' meshes in the background (cheap: headers only).
        run_in_worker(
            lambda: cat.scan_formats(
                progress=lambda i, n: safe_emit(self._scan_progress, i, n),
                cancel=lambda: self._closing),
            self._scan_done, name="nif-viewer-scan", error_result=0)

    # -- tree status / filters -------------------------------------------------------------
    def _update_status(self):
        cat, m = self._catalog, self._model
        if cat is None:
            return
        if m.filtering():
            found = m.match_count()
            text = (self.tr("{0} overridden file(s)").format(found)
                    if self._only_over.isChecked() and not self._search.text().strip()
                    else self.tr("{0} match(es)").format(found))
        else:
            text = self.tr("{0} + {1} mod(s) with meshes or textures").format(
                cat.base_name, len(cat.mods()))
        bad = cat.incompatible_count()
        if self._scan_text:
            text += "\n" + self._scan_text
        elif bad:
            text += "\n" + (self.tr("{0} incompatible mesh(es) hidden") if self._hide_bad.isChecked()
                            else self.tr("{0} incompatible mesh(es) marked in amber")).format(bad)
        self._tree_status.setText(text)

    def _on_scan_progress(self, done: int, total: int):
        if self._closing or total <= 0:
            return
        self._scan_text = self.tr("Checking mesh formats… {0}%").format(done * 100 // total)
        self._update_status()

    def _on_scan_done(self, _count):
        if self._closing or self._catalog is None:
            return
        self._scan_text = ""
        self._model.refresh_incompatible()
        self._after_rebuild()

    def _on_hide_bad(self, on: bool):
        if self._catalog is not None:
            self._model.set_hide_incompatible(on)
            self._after_rebuild()

    def _after_rebuild(self):
        """Status line and expansion after the tree was rebuilt (filter, scan…)."""
        m = self._model
        self._update_status()
        if m.filtering():
            found = m.match_count()
            if 0 < found <= 3000:
                self._tree.expandAll()
                return
            for r in range(m.rowCount()):
                self._tree.expand(m.index(r, 0))
        elif m.rowCount():
            self._tree.expand(m.index(0, 0))

    def _apply_search(self):
        if self._catalog is not None:
            self._model.set_filter(self._search.text())
            self._after_rebuild()

    def _on_only_overridden(self, on: bool):
        if self._catalog is not None:
            self._model.set_only_overridden(on)
            self._after_rebuild()

    # -- selection ----------------------------------------------------------------------------
    def _on_current_changed(self, current: QModelIndex, _prev):
        entry = current.data(EntryRole) if current.isValid() else None
        self._equip_btn.setEnabled(entry is not None and entry.path.endswith(".nif"))
        if entry is not None:
            self._load(entry)

    def _on_equip_clicked(self):
        e = self._entry
        if e is not None and e.path.endswith(".nif"):
            self._request_equip(e)

    def _request_equip(self, entry: AssetEntry):
        """Send *entry* to the Character tab and acknowledge it right away; the
        Character tab reports the outcome (slot, or why it was refused) itself."""
        self.equip_requested.emit(entry)
        name = entry.path.rsplit("/", 1)[-1]
        self._info.setText(self.tr("Sent {0} to the Character tab.").format(name))
        if entry == self._entry:                      # the button belongs to the selection
            self._equip_btn.setText(self.tr("✓ Added"))
            self._equip_btn.setStyleSheet(self._equip_css)
            self._equip_flash.start()

    def _reset_equip_button(self):
        self._equip_btn.setText(self.tr("Add to character"))
        self._equip_btn.setStyleSheet("")

    def _build_tree_menu(self, index: QModelIndex) -> "QMenu | None":
        """Right-click menu for the tree row at *index*: "Add to character" for
        a wearable mesh, plus "Jump to override" when this copy loses to
        another layer — jumping straight to whichever mod/base copy actually
        wins, instead of hunting for it by hand (the tooltip already names it,
        this just gets you there)."""
        entry = index.data(EntryRole) if index.isValid() else None
        if entry is None:
            return None
        menu = QMenu(self)
        if entry.path.endswith(".nif"):
            act = menu.addAction(self.tr("Add to character"))
            act.setToolTip(self.tr("Put this mesh on the Character tab, in the slot it belongs to"))
            act.triggered.connect(lambda _=False, e=entry: self._request_equip(e))
        if not entry.is_winner and self._catalog is not None:
            winner = self._catalog.resolve(entry.path)
            if winner is not None:
                owner = self._catalog.base_name if winner.mod == BASE else winner.mod
                act = menu.addAction(self.tr("Jump to override ({0})").format(owner))
                act.triggered.connect(lambda _=False, w=winner: self._jump_to_entry(w))
        return menu if menu.actions() else None

    def _on_tree_menu(self, pos):
        # The row under the cursor, not the selection: right-clicking must not
        # trigger the (slow) load of whatever it lands on.
        menu = self._build_tree_menu(self._tree.indexAt(pos))
        if menu is not None:
            menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _jump_to_entry(self, entry: AssetEntry):
        """Select and reveal *entry* in the tree — clearing any active
        text/only-overridden filter first, since the winning copy may live in
        a mod the current filter hides."""
        if self._catalog is None:
            return
        if self._search.text():
            self._search.blockSignals(True)
            self._search.setText("")
            self._search.blockSignals(False)
            self._search_timer.stop()
            self._apply_search()
        if self._only_over.isChecked():
            self._only_over.setChecked(False)
        idx = self._model.index_for_entry(entry)
        if not idx.isValid():
            self._info.setText(self.tr("Could not find that file in the tree."))
            return
        chain = []
        p = idx.parent()
        while p.isValid():
            chain.append(p)
            p = p.parent()
        for anc in reversed(chain):
            self._tree.expand(anc)
        self._tree.setCurrentIndex(idx)
        self._tree.scrollTo(idx)

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
        body_mode = self._body.currentData()
        want_skel = self._skel.isChecked() and self._source() != _SRC_TEXTURE

        def job():
            try:
                data = cat.read(entry)
                if entry.path.endswith(".dds"):
                    info = parse_dds_info(data[:148], len(data))
                    return gen, {"kind": "texture", "image": load_qimage_bytes(data),
                                 "info": info.summary() if info else ""}
                scene = self._loader.apply_materials(cat, read_nif(data, include_nodes=True))
                skeleton, worn_on = None, None
                if not scene.shapes and len(scene.nodes) >= 5:
                    skeleton = scene.nodes                    # a skeleton NIF: show its bones
                else:
                    # The f-suffix pair (bladesboots / bladesbootsf) needs the folder's
                    # other files, so look them up only when the path alone is silent.
                    sib = ([e.path for e in cat.siblings(entry)]
                           if detect_gender(entry.path) is None and body_mode != _BODY_NONE else [])
                    gender = (auto_gender(entry.path, scene, sib, self._profile) if body_mode == _BODY_AUTO
                              else None if body_mode == _BODY_NONE else body_mode)
                    if gender:
                        # Always the base GAME's own body, never a mod's replacer —
                        # this is a neutral backdrop for previewing *entry* itself,
                        # matching the Character tab's own base-body resolution.
                        bodies = [b for b in
                                  (self._loader.nif_entry(cat, cat.entry_in_layer(BASE, p))
                                   for p in body_paths(gender, detect_weight(entry.path),
                                                       profile=self._profile))
                                  if b is not None]
                        if bodies:
                            scene, worn_on = compose(scene, bodies), gender
                    if want_skel:
                        skeleton = self._loader.skeleton(
                            cat, gender or guess_gender(entry.path, sib) or "female",
                            self._profile.skeletons)
                images: dict[int, object] = {}
                missing: list[str] = []
                if want_tex:
                    for i, sh in enumerate(scene.shapes):
                        path = sh.textures[0] if sh.textures else ""
                        if not path or sh.is_effect:
                            continue
                        img = self._loader.image(cat, path)
                        if img is not None:
                            images[i] = img
                        else:
                            missing.append(path)
                return gen, {"kind": "mesh", "scene": scene, "images": images,
                             "missing": missing, "textures_loaded": want_tex,
                             "skeleton": skeleton, "worn_on": worn_on}
            except NifUnsupported as exc:
                label = format_label(exc.version, exc.bsver)
                cat.mark_incompatible(entry, label)
                text = self.tr(
                    "This mesh is in {0} format (NIF {1}, BS {2}).\n\nThe viewer can only "
                    "draw Skyrim SE meshes so far. Meshes in another game's format are "
                    "not converted for this game and can cause crashes."
                ).format(label, version_string(exc.version), exc.bsver)
                return gen, {"kind": "error", "text": text, "incompatible": True}
            except (NifError, BsaReadError, OSError) as exc:
                return gen, {"kind": "error", "text": str(exc)}

        run_in_worker(job, self._asset_ready, unpack=True, name="nif-viewer-load",
                      error_result=(gen, {"kind": "error", "text": self.tr("Unexpected error")}))

    def _on_char_option(self, *_args):
        """Body / Skeleton changed: rebuild the current mesh."""
        if self._entry is not None and not self._entry.path.endswith(".dds"):
            self._load(self._entry)

    def _on_asset_ready(self, gen: int, res: dict):
        if gen != self._gen:
            return                                   # a newer selection superseded this
        kind = res["kind"]
        if kind == "error":
            self._show_message(res["text"])
            self._info.setText("")
            self._record_card.set_data([])
            if res.get("incompatible"):
                self._tree.viewport().update()          # show its amber mark now
                self._update_status()
        elif kind == "texture":
            self._show_image(res["image"])
            self._info.setText(self._describe(res["info"]))
            self._record_card.set_data([])
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

    def _update_record_card(self, res: dict):
        infos = (self._catalog.record_info(self._entry)
                 if self._catalog is not None and self._entry is not None else [])
        self._record_card.set_data(
            infos, len(res.get("images", {})), len(set(res.get("missing", ()))))

    def _show_mesh(self):
        res = self._last
        if res is None:
            return
        scene, skeleton = res["scene"], res["skeleton"]
        tris = sum(len(s.indices) // 3 for s in scene.shapes)
        parts = []
        if scene.shapes:
            parts += [self.tr("{0} shape(s)").format(len(scene.shapes)),
                      self.tr("{0:,} triangles").format(tris)]
        if res["worn_on"]:
            parts.append(self.tr("worn on the {0} body").format(
                self.tr("female") if res["worn_on"] == "female" else self.tr("male")))
        if skeleton:
            from Utils.nif.skeleton import is_bone
            parts.append(self.tr("skeleton: {0} bones").format(sum(is_bone(n) for n in skeleton)))
        if res["textures_loaded"] and scene.shapes:
            miss = sorted(set(res["missing"]))
            parts.append(self.tr("textures: {0} found").format(len(res["images"]))
                         + (self.tr(", {0} missing").format(len(miss)) if miss else ""))
        info = " · ".join(parts)
        if not scene.shapes and not skeleton:
            self._show_message(self.tr("This file has no drawable geometry "
                                       "(animation or collision only)."))
            self._info.setText(self._describe(info))
            self._record_card.set_data([])
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
            self._viewport.set_scene(scene, lambda sh: images.get(index[id(sh)]), skeleton)
            self._apply_mode()
            self._stack.setCurrentIndex(_PAGE_MESH)
            QTimer.singleShot(400, self._check_gl)
        # Must run AFTER the setCurrentIndex() calls above: QStackedWidget
        # raises its newly-current page when switching, which buries this
        # overlay if it was already raised beforehand -- only matters the
        # FIRST time the stack ever leaves the initial placeholder page
        # (later selections don't change the current index, so no re-raise
        # happens there and the ordering doesn't matter) -- but always
        # updating last is simplest and correct in every case.
        self._update_record_card(res)
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
        last = self._last
        reusable = (last is not None
                    and (not self._needs_textures() or last["textures_loaded"])
                    and (not self._skel.isChecked() or self._source() == _SRC_TEXTURE
                         or last["skeleton"] is not None))
        if reusable:
            self._show_mesh()
        else:
            self._load(self._entry)                   # textures / bones weren't fetched yet

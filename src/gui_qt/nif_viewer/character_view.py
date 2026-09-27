"""Character tab: a whole character on the game's skeleton.

The base body (body, hands, feet, head) is shown wearing whatever you equip;
each piece goes into its slot group (Head, Hair, Body, Hands, Feet…) worked out
from the mesh's own body slots, replacing what was there, and hides the base
parts it covers — the way the game builds an actor. Pieces are added from the
NIF Viewer ("Add to character") or chosen here. Bodies, heads and the skeleton
are the game's *winning* files, so replacer mods show up. Skinned meshes are in
their rest pose.

Slot handling is approximate: the slots come from the mesh files, whereas the
game reads them from the armour records in the plugins.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from Utils.archives.bsa_file_reader import BsaReadError
from Utils.nif.asset_catalog import BASE, AssetCatalog, AssetEntry
from Utils.nif.catalog_loader import build_catalog
from Utils.nif.character import (
    GROUP_LABELS, GROUPS, assemble, body_paths, bone_transforms, covered_slots, detect_gender,
    guess_gender, slot_group, weight_variant,
)
from Utils.nif.nif_reader import NifError, NifUnsupported, format_label, read_nif
from gui_qt.nif_viewer.asset_loader import AssetLoader
from gui_qt.nif_viewer.gl_viewport import TEXTURED, MeshViewport
from gui_qt.theme.theme_qt import _c, active_palette
from gui_qt.worker import run_in_worker

_WEARABLE_PREFIXES = ("meshes/armor/", "meshes/clothes/", "meshes/actors/character/character assets/")
_MAX_LISTED = 400


class PickMeshDialog(QDialog):
    """Search the game's wearable meshes (armour, clothes, hair…) and pick one."""

    def __init__(self, catalog: AssetCatalog, group: "str | None", parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Choose a mesh") if group is None
                            else self.tr("Choose: {0}").format(GROUP_LABELS.get(group, group)))
        self.resize(720, 520)
        self._entries: list[AssetEntry] = []
        self._catalog = catalog
        self._chosen: "AssetEntry | None" = None

        v = QVBoxLayout(self)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search by file name or mod…"))
        self._search.setClearButtonEnabled(True)
        v.addWidget(self._search)
        self._list = QListWidget()
        v.addWidget(self._list, 1)
        self._count = QLabel()
        v.addWidget(self._count)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self._ok = buttons.button(QDialogButtonBox.Ok)

        want_hair = group == "hair"
        for e in self._candidates():
            is_hair = "/hair/" in e.path
            if group is None or want_hair == is_hair:
                self._entries.append(e)
        self._entries.sort(key=lambda e: e.path)
        self._search.textChanged.connect(self._refill)
        self._list.itemDoubleClicked.connect(lambda _i: self._accept())
        self._list.currentItemChanged.connect(lambda *_: self._ok.setEnabled(self._list.currentItem() is not None))
        self._refill()

    def _candidates(self):
        cat = self._catalog
        for e in cat.base_entries():
            if e.path.endswith(".nif") and e.path.startswith(_WEARABLE_PREFIXES) and e.is_winner:
                yield e
        for m in cat.mods():
            for e in cat.mod_entries(m):
                if (e.path.endswith(".nif") and e.path.startswith(_WEARABLE_PREFIXES)
                        and e.is_winner and cat.incompatible_label(e) is None):
                    yield e

    def _label(self, e: AssetEntry) -> str:
        owner = self._catalog.base_name if e.mod == BASE else e.mod
        return f"{e.path[len('meshes/'):]}    [{owner}]"

    def _refill(self):
        text = self._search.text().strip().lower()
        self._list.clear()
        shown = 0
        for e in self._entries:
            if text and text not in e.path and text not in e.mod.lower():
                continue
            if shown >= _MAX_LISTED:
                break
            item = QListWidgetItem(self._label(e))
            item.setData(Qt.UserRole, e)
            self._list.addItem(item)
            shown += 1
        total = sum(1 for e in self._entries if not text or text in e.path or text in e.mod.lower())
        self._count.setText(self.tr("{0} match(es){1}").format(
            total, self.tr(" — showing the first {0}").format(_MAX_LISTED) if total > shown else ""))
        if shown:
            self._list.setCurrentRow(0)
        self._ok.setEnabled(bool(shown))

    def _accept(self):
        item = self._list.currentItem()
        if item is not None:
            self._chosen = item.data(Qt.UserRole)
            self.accept()

    def chosen(self) -> "AssetEntry | None":
        return self._chosen


class CharacterView(QWidget):
    _catalog_ready = Signal(object)
    _build_ready = Signal(int, object)
    _equip_ready = Signal(object, object, str, object)   # entry, group | None, message, gender | None
    equip_result = Signal(str, bool)              # message, ok — for a toast when equipping from elsewhere

    def __init__(self, game, profile_dir, staging_dir, parent=None):
        super().__init__(parent)
        self._catalog: "AssetCatalog | None" = None
        self._loader = AssetLoader()
        self._pieces: dict[str, AssetEntry] = {}
        self._piece_gender: dict[str, "str | None"] = {}   # group → the gender its mesh is made for
        self._pending: list[AssetEntry] = []          # equips requested before the catalog was ready
        self._gen = 0
        self._first_build = True
        self._closing = False
        pal = active_palette()

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- left: settings + slots -------------------------------------------------------------
        left = QWidget()
        left.setFixedWidth(360)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 8, 8)
        form = QGridLayout()
        form.addWidget(QLabel(self.tr("Gender")), 0, 0)
        self._gender = QComboBox()
        self._gender.addItem(self.tr("Female"), "female")
        self._gender.addItem(self.tr("Male"), "male")
        form.addWidget(self._gender, 0, 1)
        form.addWidget(QLabel(self.tr("Body weight")), 1, 0)
        self._weight = QComboBox()
        self._weight.addItem(self.tr("Heavy (_1)"), 1)
        self._weight.addItem(self.tr("Light (_0)"), 0)
        form.addWidget(self._weight, 1, 1)
        self._skel = QCheckBox(self.tr("Show skeleton"))
        form.addWidget(self._skel, 2, 0, 1, 2)
        lv.addLayout(form)

        hdr = QLabel(self.tr("Equipment"))
        hdr.setStyleSheet("font-weight:600; padding-top:8px;")
        lv.addWidget(hdr)
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self._slot_labels: dict[str, QLabel] = {}
        self._slot_clear: dict[str, QPushButton] = {}
        for row, (group, label) in enumerate(GROUPS):
            grid.addWidget(QLabel(label), row, 0)
            name = QLabel(self.tr("— none —"))
            name.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')};")
            name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            grid.addWidget(name, row, 1)
            choose = QPushButton(self.tr("Choose…"))
            choose.clicked.connect(lambda _=False, g=group: self._choose(g))
            grid.addWidget(choose, row, 2)
            clear = QPushButton("✕")
            clear.setFixedWidth(28)
            clear.setToolTip(self.tr("Remove this piece"))
            clear.setEnabled(False)
            clear.clicked.connect(lambda _=False, g=group: self._unequip(g))
            grid.addWidget(clear, row, 3)
            self._slot_labels[group], self._slot_clear[group] = name, clear
        lv.addLayout(grid)
        self._remove_all = QPushButton(self.tr("Remove all"))
        self._remove_all.setEnabled(False)
        self._remove_all.clicked.connect(self._clear_all)
        lv.addWidget(self._remove_all)
        hint = QLabel(self.tr(
            "Add pieces from the NIF Viewer (select an armour, clothing or hair mesh and press "
            "“Add to character”), or choose them here. A piece goes into the slot its "
            "mesh belongs to and hides the body parts it covers.\n\n"
            "Drag to rotate · right-drag to pan · scroll to zoom · double-click to re-frame.\n\n"
            "The head is not welded to the body the way the game does it (it builds each "
            "character's head at runtime), so a seam can show at the neck, most at Light weight."))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')}; padding-top:8px;")
        lv.addWidget(hint)
        lv.addStretch(1)
        root.addWidget(left)

        # -- right: viewport ---------------------------------------------------------------------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        self._stack = QStackedWidget()
        self._viewport = MeshViewport()
        self._message = QLabel(self.tr("Building the character…"))
        self._message.setAlignment(Qt.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')}; padding:24px;")
        self._stack.addWidget(self._viewport)
        self._stack.addWidget(self._message)
        self._stack.setCurrentIndex(1)
        rv.addWidget(self._stack, 1)
        self._info = QLabel()
        self._info.setStyleSheet(
            f"background:{_c(pal, 'BG_HEADER')}; color:{_c(pal, 'TEXT_MAIN')}; padding:4px 10px;")
        self._info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        rv.addWidget(self._info)
        root.addWidget(right, 1)

        # -- wiring ---------------------------------------------------------------------------------
        self._gender.currentIndexChanged.connect(lambda _i: self._rebuild(reframe=False))
        self._weight.currentIndexChanged.connect(lambda _i: self._rebuild(reframe=False))
        self._skel.toggled.connect(lambda _on: self._rebuild(reframe=self._first_build))
        self._catalog_ready.connect(self._on_catalog_ready)
        self._build_ready.connect(self._on_build_ready)
        self._equip_ready.connect(self._on_equip_ready)
        self.destroyed.connect(lambda *_: self._close())
        run_in_worker(lambda: build_catalog(game, profile_dir, staging_dir),
                      self._catalog_ready, name="character-catalog")

    # -- lifecycle ---------------------------------------------------------------------------------
    def _close(self):
        self._closing = True
        cat, self._catalog = self._catalog, None
        if cat is not None:
            cat.close()

    def _on_catalog_ready(self, cat):
        if cat is None:
            self._show_message(self.tr("Could not index the game's meshes."))
            return
        self._catalog = cat
        pending, self._pending = self._pending, []
        for e in pending:
            self.equip(e)
        if not pending:
            self._rebuild(reframe=True)

    # -- equipping ---------------------------------------------------------------------------------
    def equip(self, entry: AssetEntry):
        """Put *entry* (a mesh) on the character, in the slot group its body slots
        say. Meshes without body slots (props, weapons) are refused."""
        cat = self._catalog
        if cat is None:
            self._pending.append(entry)
            return
        name = entry.path.rsplit("/", 1)[-1]
        self._info.setText(self.tr("Reading {0}…").format(name))

        def job():
            try:
                sc = read_nif(cat.read(entry))
            except NifUnsupported as exc:
                return entry, None, self.tr("{0} is in {1} format — only Skyrim SE meshes can be worn.").format(
                    name, format_label(exc.version, exc.bsver)), None
            except (NifError, BsaReadError, OSError) as exc:
                return entry, None, self.tr("Could not read {0}: {1}").format(name, exc), None
            group = slot_group(entry.path, covered_slots(sc))
            if group is None:
                # Usually an item's display/inventory model (unskinned); the worn
                # meshes sit beside it, named after it (bladesarmor.nif →
                # bladesarmor_1.nif, bladesarmorf_1.nif). Point at them.
                stem = name.rsplit(".", 1)[0]
                worn = [e.path.rsplit("/", 1)[-1] for e in cat.siblings(entry)
                        if e.path.endswith(".nif") and e.path.rsplit("/", 1)[-1].startswith(stem)][:8]
                if worn:
                    return entry, None, self.tr(
                        "{0} has no body slots — it looks like an item's display model, "
                        "not a worn mesh. Worn versions in the same folder: {1}."
                    ).format(name, ", ".join(worn)), None
                return entry, None, self.tr(
                    "{0} has no body slots, so it can't be worn (props, weapons and shields "
                    "aren't supported yet).").format(name), None
            # Which body it is made for: folder/word markers, else the f-suffix pair
            # (bladesboots_1 / bladesbootsf_1) read from the folder's other files.
            sib = [] if detect_gender(entry.path) else [e.path for e in cat.siblings(entry)]
            return entry, group, "", guess_gender(entry.path, sib)

        run_in_worker(job, self._equip_ready, unpack=True, name="character-equip",
                      error_result=(entry, None, self.tr("Unexpected error"), None))

    def _on_equip_ready(self, entry, group, message, gender):
        if self._closing:
            return
        if group is None:
            self._info.setText(message)
            self.equip_result.emit(message, False)
            return
        # First piece: match the character to it (male armour on a male body).
        if not self._pieces:
            if gender and gender != self._gender.currentData():
                self._gender.blockSignals(True)
                self._gender.setCurrentIndex(self._gender.findData(gender))
                self._gender.blockSignals(False)
        self._pieces[group] = entry
        self._piece_gender[group] = gender
        self.equip_result.emit(self.tr("Added {0} to the character ({1})").format(
            entry.path.rsplit("/", 1)[-1], GROUP_LABELS.get(group, group)), True)
        self._refresh_slots()
        self._rebuild(reframe=False)

    def _unequip(self, group: str):
        if self._pieces.pop(group, None) is not None:
            self._piece_gender.pop(group, None)
            self._refresh_slots()
            self._rebuild(reframe=False)

    def _clear_all(self):
        if self._pieces:
            self._pieces.clear()
            self._piece_gender.clear()
            self._refresh_slots()
            self._rebuild(reframe=False)

    def _choose(self, group: str):
        if self._catalog is None:
            return
        dlg = PickMeshDialog(self._catalog, group, self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen() is not None:
            self.equip(dlg.chosen())

    def _refresh_slots(self):
        pal = active_palette()
        for group, _label in GROUPS:
            e = self._pieces.get(group)
            lab = self._slot_labels[group]
            if e is None:
                lab.setText(self.tr("— none —"))
                lab.setToolTip("")
                lab.setStyleSheet(f"color:{_c(pal, 'TEXT_DIM')};")
            else:
                owner = self._catalog.base_name if e.mod == BASE else e.mod
                lab.setText(e.path.rsplit("/", 1)[-1])
                lab.setToolTip(f"{e.path}\n{owner}" + (f" ({e.archive})" if e.archive else ""))
                lab.setStyleSheet(f"color:{_c(pal, 'TEXT_MAIN')};")
            self._slot_clear[group].setEnabled(e is not None)
        self._remove_all.setEnabled(bool(self._pieces))

    # -- building the scene ---------------------------------------------------------------------------
    def _rebuild(self, reframe: bool):
        cat = self._catalog
        if cat is None:
            return
        self._gen += 1
        gen = self._gen
        gender, weight = self._gender.currentData(), self._weight.currentData()
        want_skel = self._skel.isChecked()
        pieces = dict(self._pieces)
        reframe = reframe or self._first_build
        self._info.setText(self.tr("Building…"))

        def job():
            problems: list[str] = []
            base = [b for b in (self._loader.nif(cat, p) for p in body_paths(gender, weight, head=True))
                    if b is not None]
            scenes = {}
            matched: list[str] = []
            for group, entry in pieces.items():
                # Use the worn mesh's slim/heavy version that matches the body: the
                # copy from the same mod (or the base game) if it ships one, else
                # whichever copy wins.
                vpath = weight_variant(entry.path, weight)
                if vpath and vpath != entry.path:
                    variant = cat.entry_in_layer(entry.mod, vpath) or cat.resolve(vpath)
                    if variant is not None:
                        matched.append(variant.path.rsplit("/", 1)[-1])
                        entry = variant
                try:
                    scenes[group] = read_nif(cat.read(entry))
                except (NifError, BsaReadError, OSError) as exc:
                    problems.append(f"{entry.path.rsplit('/', 1)[-1]}: {exc}")
            # The skeleton poses every piece (hair and eyes are stored relative to a
            # bone and only land on the head that way); it is drawn only if asked.
            skel_nodes = self._loader.skeleton(cat, gender)
            scene = assemble(base, scenes, bone_transforms(skel_nodes) if skel_nodes else None)
            images: dict[int, object] = {}
            missing: list[str] = []
            for i, sh in enumerate(scene.shapes):
                path = sh.textures[0] if sh.textures else ""
                if not path or sh.is_effect:
                    continue
                img = self._loader.image(cat, path)
                if img is not None:
                    images[i] = img
                else:
                    missing.append(path)
            skeleton = skel_nodes if want_skel else None
            return gen, {"scene": scene, "images": images, "missing": missing,
                         "skeleton": skeleton, "problems": problems, "reframe": reframe,
                         "base_found": len(base), "pieces": len(scenes), "matched": matched}

        run_in_worker(job, self._build_ready, unpack=True, name="character-build",
                      error_result=(gen, {"error": self.tr("Unexpected error")}))

    def _on_build_ready(self, gen: int, res: dict):
        if gen != self._gen or self._closing:
            return
        if "error" in res:
            self._show_message(res["error"])
            return
        scene = res["scene"]
        if not scene.shapes:
            self._show_message(self.tr("The game's base body could not be found."))
            return
        images, index = res["images"], {id(s): i for i, s in enumerate(scene.shapes)}
        self._viewport.set_scene(scene, lambda sh: images.get(index[id(sh)]), res["skeleton"],
                                 reframe=res["reframe"])
        self._viewport.set_mode(TEXTURED)
        self._stack.setCurrentIndex(0)
        if res["reframe"]:
            self._first_build = False
        QTimer.singleShot(400, self._check_gl)
        tris = sum(len(s.indices) // 3 for s in scene.shapes)
        parts = [self.tr("{0} piece(s) worn").format(res["pieces"]),
                 self.tr("{0:,} triangles").format(tris)]
        if res["skeleton"]:
            from Utils.nif.skeleton import is_bone
            parts.append(self.tr("skeleton: {0} bones").format(sum(is_bone(n) for n in res["skeleton"])))
        miss = sorted(set(res["missing"]))
        parts.append(self.tr("textures: {0} found").format(len(images))
                     + (self.tr(", {0} missing").format(len(miss)) if miss else ""))
        if res["matched"]:
            parts.append(self.tr("matched to body weight: {0}").format(", ".join(res["matched"])))
        current = self._gender.currentData()
        other = [self._pieces[g].path.rsplit("/", 1)[-1] for g, pg in self._piece_gender.items()
                 if pg and pg != current and g in self._pieces]
        if other:
            parts.append(self.tr("⚠ made for the {0} body: {1}").format(
                self.tr("female") if current == "male" else self.tr("male"), ", ".join(other)))
        if res["problems"]:
            parts.append(self.tr("could not load: {0}").format("; ".join(res["problems"])))
        self._info.setText(" · ".join(parts))

    def _check_gl(self):
        if self._viewport.error() and self._stack.currentIndex() == 0:
            self._show_message(self.tr("3D view unavailable: {0}").format(self._viewport.error()))

    def _show_message(self, text: str):
        self._message.setText(text)
        self._stack.setCurrentIndex(1)

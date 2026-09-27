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

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QSlider, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from Utils.archives.bsa_file_reader import BsaReadError
from Utils.nif.asset_catalog import BASE, AssetCatalog, AssetEntry
from Utils.nif.catalog_loader import build_catalog
from Utils.nif.character import (
    SKYRIM_PROFILE, GameProfile, assemble, blend_scene, body_paths, bone_transforms, covered_slots,
    detect_gender, fits_slot, gender_fits, guess_gender, is_race_variant, is_wearable_path,
    profile_for_game, slot_group, weight_variant,
)
from Utils.nif.nif_reader import NifError, NifUnsupported, format_label, read_nif
from gui_qt.nif_viewer.asset_loader import AssetLoader
from gui_qt.nif_viewer.gl_viewport import TEXTURED, MeshViewport
from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import _c, active_palette
from gui_qt.worker import run_in_worker

_MAX_LISTED = 400


class PickMeshDialog(QDialog):
    """Pick a mesh that fits a slot.

    Only meshes that really belong in *group* are listed: they must sit in a
    wearable folder (armour, clothes, hairstyles, the base body parts a body mod
    replaces) and their own body slots must place them in that group — which means
    opening each candidate, on a worker thread with a progress line (a slots-only
    read, ~0.2 ms a file, cached for next time). Meshes made for the other gender
    are left out unless you untick the box, and a slim/heavy pair (_0/_1) is one
    entry, since the character picks the matching weight itself."""

    _progress = Signal(int, int)
    _ready = Signal(object)

    def __init__(self, catalog: AssetCatalog, group: "str | None", gender: "str | None" = None,
                 parent=None, profile: GameProfile = SKYRIM_PROFILE):
        super().__init__(parent)
        group_labels = dict(profile.groups)
        self.setWindowTitle(self.tr("Choose a mesh") if group is None
                            else self.tr("Choose: {0}").format(group_labels.get(group, group)))
        self.resize(720, 540)
        self._catalog = catalog
        self._group = group
        self._profile = profile
        self._gender = gender
        self._fitting: list[AssetEntry] = []          # everything that fits the slot
        self._entries: list[AssetEntry] = []          # …after the gender filter and weight pairs
        self._chosen: "AssetEntry | None" = None
        self._closing = False
        self._gender_ok: dict = {}
        self.scanned = False                          # True once the fit check has finished

        v = QVBoxLayout(self)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search by file name or mod…"))
        self._search.setClearButtonEnabled(True)
        v.addWidget(self._search)
        self._only_gender = QCheckBox()
        if gender:
            self._only_gender.setText(self.tr("Only meshes for a {0} body (or not specified)").format(
                self.tr("female") if gender == "female" else self.tr("male")))
            self._only_gender.setChecked(True)
            v.addWidget(self._only_gender)
        self._list = QListWidget()
        v.addWidget(self._list, 1)
        self._count = QLabel(self.tr("Checking which meshes fit…"))
        v.addWidget(self._count)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self._ok = buttons.button(QDialogButtonBox.Ok)
        self._ok.setEnabled(False)

        self._search.textChanged.connect(self._refill)
        self._only_gender.toggled.connect(lambda _on: self._apply_filters())
        self._list.itemDoubleClicked.connect(lambda _i: self._accept())
        self._list.currentItemChanged.connect(
            lambda *_: self._ok.setEnabled(self._list.currentItem() is not None))
        self._progress.connect(self._on_progress)
        self._ready.connect(self._on_ready)
        run_in_worker(self._scan, self._ready, name="character-picker", error_result=[])

    # -- worker ---------------------------------------------------------------------------------
    def _scan(self):
        cat, group, profile = self._catalog, self._group, self._profile
        cands: list[AssetEntry] = []
        for e in cat.base_entries():
            if e.is_winner and is_wearable_path(e.path, group, profile):
                cands.append(e)
        for m in cat.mods():
            for e in cat.mod_entries(m):
                if (e.is_winner and is_wearable_path(e.path, group, profile)
                        and cat.incompatible_label(e) is None):
                    cands.append(e)
        fitting: list[AssetEntry] = []
        total = len(cands)
        for i, e in enumerate(cands):
            if self._closing:
                return []
            if i % 150 == 0:
                safe_emit(self._progress, i, total)
            if fits_slot(e.path, cat.slots_of(e), group, profile):
                fitting.append(e)
        return fitting

    def _on_progress(self, done: int, total: int):
        if total:
            self._count.setText(self.tr("Checking which meshes fit… {0}%").format(done * 100 // total))

    def _on_ready(self, fitting):
        self.scanned = True
        # Gender and race variants are read from the folder's other files
        # (bladesboots / bladesbootsf, hatf / hatfk).
        by_folder: dict[str, list[str]] = {}
        for e in fitting or []:
            by_folder.setdefault(e.path.rsplit("/", 1)[0], []).append(e.path)
        self._fitting = [e for e in (fitting or [])
                         if not is_race_variant(e.path, by_folder[e.path.rsplit("/", 1)[0]])]
        self._gender_ok = {e: gender_fits(e.path, self._gender, by_folder[e.path.rsplit("/", 1)[0]])
                           for e in self._fitting}
        self._apply_filters()

    # -- list ---------------------------------------------------------------------------------------
    def _apply_filters(self):
        only = self._gender is not None and self._only_gender.isChecked()
        pool = [e for e in self._fitting if not only or self._gender_ok.get(e, True)]
        # One entry per slim/heavy pair: the heavy (_1) one when both exist.
        best: dict[tuple, AssetEntry] = {}
        for e in pool:
            key = (e.mod, e.path[:-6] if e.path.endswith(("_0.nif", "_1.nif")) else e.path)
            cur = best.get(key)
            if cur is None or e.path.endswith("_1.nif"):
                best[key] = e
        self._entries = sorted(best.values(), key=lambda e: e.path)
        self._refill()

    def _label(self, e: AssetEntry) -> str:
        owner = self._catalog.base_name if e.mod == BASE else e.mod
        return f"{e.path[len('meshes/'):]}    [{owner}]"

    def _refill(self):
        text = self._search.text().strip().lower()
        self._list.clear()
        matching = [e for e in self._entries
                    if not text or text in e.path or text in e.mod.lower()]
        for e in matching[:_MAX_LISTED]:
            item = QListWidgetItem(self._label(e))
            item.setData(Qt.UserRole, e)
            self._list.addItem(item)
        note = self.tr(" — showing the first {0}").format(_MAX_LISTED) if len(matching) > _MAX_LISTED else ""
        self._count.setText(self.tr("{0} mesh(es) fit{1}").format(len(matching), note))
        if self._list.count():
            self._list.setCurrentRow(0)
        self._ok.setEnabled(self._list.count() > 0)

    def _accept(self):
        item = self._list.currentItem()
        if item is not None:
            self._chosen = item.data(Qt.UserRole)
            self.accept()

    def done(self, result):
        self._closing = True                          # stops a running scan
        super().done(result)

    def chosen(self) -> "AssetEntry | None":
        return self._chosen


class CharacterView(QWidget):
    _catalog_ready = Signal(object)
    _build_ready = Signal(int, object)
    _equip_ready = Signal(object, object, str, object)   # entry, group | None, message, gender | None
    equip_result = Signal(str, bool)              # message, ok — for a toast when equipping from elsewhere

    def __init__(self, game, profile_dir, staging_dir, parent=None):
        super().__init__(parent)
        self._profile = profile_for_game(getattr(game, "game_id", None))
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
        self._weight_label = QLabel()
        form.addWidget(self._weight_label, 1, 0)
        self._weight = QSlider(Qt.Horizontal)
        self._weight.setRange(0, 100)
        self._weight.setValue(100)
        self._weight.setToolTip(self.tr(
            "Body weight, 0 (slim) to 100 (heavy). The game morphs the body and worn "
            "pieces between their _0 and _1 meshes in the same way."))
        form.addWidget(self._weight, 1, 1)
        self._show_weight()
        if not self._profile.has_weight_suffix:
            # This game has no per-actor body-weight morph at all (verified on
            # Fallout 4: base body/hands ship as a single file, no _0/_1 pair) —
            # the slider would just be dead weight in the UI.
            self._weight_label.setVisible(False)
            self._weight.setVisible(False)
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
        for row, (group, label) in enumerate(self._profile.groups):
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
        hint_text = (self.tr(
            "Add pieces from the NIF Viewer (select an armour, clothing or hair mesh and press "
            "“Add to character”), or choose them here. A piece goes into the slot its "
            "mesh belongs to and hides the body parts it covers.\n\n"
            "Drag to rotate · right-drag to pan · scroll to zoom · double-click to re-frame.\n\n")
            + (self.tr(
                "The head is not welded to the body the way the game does it (it builds each "
                "character's head at runtime), so a seam can show at the neck, most at Light weight.")
               if self._profile.has_weight_suffix else self.tr(
                "The head is not welded to the body the way the game does it (it builds each "
                "character's head at runtime), so a seam can show at the neck.")))
        hint = QLabel(hint_text)
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
        # Dragging the slider updates the label at once; the (slow) rebuild waits
        # until it has been still for a moment.
        self._weight_timer = QTimer(self)
        self._weight_timer.setSingleShot(True)
        self._weight_timer.setInterval(250)
        self._weight_timer.timeout.connect(lambda: self._rebuild(reframe=False))
        self._weight.valueChanged.connect(self._on_weight_changed)
        self._skel.toggled.connect(lambda _on: self._rebuild(reframe=self._first_build))
        self._catalog_ready.connect(self._on_catalog_ready)
        self._build_ready.connect(self._on_build_ready)
        self._equip_ready.connect(self._on_equip_ready)
        self.destroyed.connect(lambda *_: self._close())
        run_in_worker(lambda: build_catalog(game, profile_dir, staging_dir),
                      self._catalog_ready, name="character-catalog")

    def weight(self) -> float:
        """Body weight 0.0 (slim, the _0 meshes) to 1.0 (heavy, the _1 meshes)."""
        return self._weight.value() / 100.0

    def _show_weight(self):
        self._weight_label.setText(self.tr("Body weight {0}%").format(self._weight.value()))

    def _on_weight_changed(self, _value: int):
        self._show_weight()
        self._weight_timer.start()

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
        run_in_worker(self._prewarm, None, name="character-prewarm")

    def _prewarm(self):
        """Read the body slots of every candidate mesh in the background, so a
        slot picker opens instantly (the results are cached in the catalog)."""
        cat = self._catalog
        if cat is None:
            return
        n = 0
        for m in [BASE] + cat.mods():
            entries = cat.base_entries() if m == BASE else cat.mod_entries(m)
            for e in entries:
                if self._closing or self._catalog is not cat:
                    return
                if e.is_winner and is_wearable_path(e.path, profile=self._profile) and cat.incompatible_label(e) is None:
                    cat.slots_of(e)
                    n += 1
                    if n % 500 == 0:
                        time.sleep(0.001)              # stay out of the UI thread's way

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
                return entry, None, self.tr("{0} is in {1} format — only Skyrim/Fallout 4 meshes can be worn.").format(
                    name, format_label(exc.version, exc.bsver)), None
            except (NifError, BsaReadError, OSError) as exc:
                return entry, None, self.tr("Could not read {0}: {1}").format(name, exc), None
            # cat.slots_of() prefers an active plugin's own ARMA record (ground
            # truth) over the mesh's own embedded slots — the two agree for
            # Skyrim (Bethesda kept the numbering aligned there) but not for
            # Fallout 4, where a mesh's own segment data uses a different,
            # non-equip-slot numbering (verified: a real vault suit's own
            # segments report {1..6}, not the {33, 41...} its ARMA record
            # actually declares) — so the raw mesh-parsed covered_slots(sc)
            # would put every FO4 piece in the wrong group, or none at all.
            group = slot_group(entry.path, cat.slots_of(entry) or covered_slots(sc), self._profile)
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
            entry.path.rsplit("/", 1)[-1], dict(self._profile.groups).get(group, group)), True)
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
        dlg = PickMeshDialog(self._catalog, group, self._gender.currentData(), self, profile=self._profile)
        if dlg.exec() == QDialog.Accepted and dlg.chosen() is not None:
            self.equip(dlg.chosen())

    def _refresh_slots(self):
        pal = active_palette()
        for group, _label in self._profile.groups:
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
        gender, weight = self._gender.currentData(), self.weight()
        want_skel = self._skel.isChecked()
        pieces = dict(self._pieces)
        reframe = reframe or self._first_build
        self._info.setText(self.tr("Building…"))

        def load_weighted(slim_entry, heavy_entry):
            """The scene at the current weight from a slim/heavy pair of files: at the
            ends only the matching file is read; in between both are blended (vertex by
            vertex, like the game). A missing partner just means the other is used."""
            if weight <= 0.0:
                order = (slim_entry, heavy_entry)
            elif weight >= 1.0:
                order = (heavy_entry, slim_entry)
            else:
                slim = self._loader.nif_entry(cat, slim_entry)
                heavy = self._loader.nif_entry(cat, heavy_entry)
                if slim is not None and heavy is not None:
                    return blend_scene(slim, heavy, weight), True
                return (heavy or slim), False
            for e in order:                                   # the wanted end, else the other
                if e is not None:
                    sc = self._loader.nif_entry(cat, e)
                    if sc is not None:
                        return sc, False
            return None, False

        def job():
            problems: list[str] = []
            base = []
            # Base body/hands/feet come in slim and heavy files (Fallout 4: a
            # single file, so p0 == p1 below and no blend is attempted); head
            # and eyes in one.
            for p0, p1 in zip(body_paths(gender, 0, head=True, profile=self._profile),
                              body_paths(gender, 1, head=True, profile=self._profile)):
                e0, e1 = cat.resolve(p0), cat.resolve(p1)
                sc, _blended = load_weighted(e0, e1) if p0 != p1 else (self._loader.nif_entry(cat, e1), False)
                if sc is not None:
                    base.append(sc)
            scenes = {}
            matched: list[str] = []                         # a different weight file than equipped was used
            blended: list[str] = []                         # slim and heavy files blended
            for group, entry in pieces.items():
                # A worn mesh comes as a slim (_0) and a heavy (_1) file: the copies from
                # the same mod (or the base game) are used when it ships them, else
                # whichever copy wins.
                lo, hi = weight_variant(entry.path, 0), weight_variant(entry.path, 1)
                if lo is None:
                    slim_e = heavy_e = entry
                else:
                    def find(p, entry=entry):
                        return entry if p == entry.path else (
                            cat.entry_in_layer(entry.mod, p) or cat.resolve(p))
                    slim_e, heavy_e = find(lo), find(hi)
                sc, was_blended = load_weighted(slim_e, heavy_e)
                if sc is None:
                    problems.append(f"{entry.path.rsplit('/', 1)[-1]}: could not be read")
                    continue
                scenes[group] = sc
                name = entry.path.rsplit("/", 1)[-1]
                if was_blended:
                    blended.append(name)
                elif lo is not None:
                    used = slim_e if weight <= 0.0 else heavy_e
                    if used is not None and used.path != entry.path:
                        matched.append(used.path.rsplit("/", 1)[-1])
            # The skeleton poses every piece (hair and eyes are stored relative to a
            # bone and only land on the head that way); it is drawn only if asked.
            skel_nodes = self._loader.skeleton(cat, gender, self._profile.skeletons)
            scene = assemble(base, scenes, bone_transforms(skel_nodes) if skel_nodes else None,
                             self._profile)
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
                         "base_found": len(base), "pieces": len(scenes), "matched": matched,
                         "blended": blended, "weight": weight}

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
        if res["blended"]:
            parts.append(self.tr("blended at weight {0}%: {1}").format(
                round(res["weight"] * 100), ", ".join(res["blended"])))
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

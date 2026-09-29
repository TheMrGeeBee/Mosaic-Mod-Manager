"""Create/Publish Collection — a modlist-scoped tab (like Export Profile)
that packages the current profile's enabled mods into a real Nexus/Vortex
collection archive, and can publish it as a draft revision on Nexus.

Collection-level info (name/description/instructions/category/adult
content/recommend-new-profile/exclude-plugin-rules) + a choice of "new
collection" or "update one you already own", a full per-mod table (Source /
File / Optional / Fomod / Update Policy / Instructions, plus adding extra
optional "variant" rows for the same mod's other Nexus files — e.g. offering
several resolution options the way real curated collections do), then either
"Export to file" (local .7z, for manual Vortex import) or "Publish to Nexus"
(upload + create/revise a draft revision — the final "Publish" listing
action still happens on nexusmods.com, matching Vortex's own flow: Nexus's
API has no endpoint to flip a draft straight to published).

The per-mod table reuses the same overlay widgets as Export Profile
(gui_qt.views.mod_row_widgets) over the same Utils.profile.profile_export
row schema, with a fifth Source option ("Browse") and two new per-row
fields (update_policy / instructions) collection_export.py already knows
how to write into the manifest. All packaging logic lives in the neutral
collection_export module.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QComboBox, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QListWidget,
    QListWidgetItem,
)

from gui_qt.theme.theme_qt import active_palette, _c
from gui_qt.views.mod_row_widgets import (
    SOURCE_LABELS, source_button_qss, CardOverlay, card_title,
    card_button_bar, SourceOverlay, VersionOverlay, center_checkbox,
)
from Utils.collections import collection_export
from Utils.profile import profile_export


# Column indices for the per-mod table.
(_COL_NAME, _COL_SOURCE, _COL_VERSION, _COL_OPTIONAL, _COL_FOMOD,
 _COL_POLICY, _COL_INSTRUCTIONS, _COL_ACTIONS) = range(8)

_POLICY_ORDER = ("exact", "prefer", "latest")
_POLICY_LABELS = {
    "exact":  "Exact only",
    "prefer": "Prefer this, accept newer",
    "latest": "Always latest",
}


# ---------------------------------------------------------------------------
# Per-mod instructions overlay (not shared with Export Profile — collection
# manifests have a real source.instructions field, .mosaic manifests don't).
# ---------------------------------------------------------------------------

class _InstructionsOverlay(CardOverlay):
    CARD_W = 460
    CARD_H = 320

    def __init__(self, host, mod_name, current_text, on_pick):
        super().__init__(host)
        self._on_pick = on_pick
        self._body.addWidget(card_title(self.tr("Instructions — {0}").format(mod_name)))
        sub = QLabel(self.tr(
            'Shown to the user during install (e.g. "install only if you '
            'have both X and Y", or "pick one of the resolution variants"):'))
        sub.setObjectName("CardSub")
        sub.setWordWrap(True)
        self._body.addWidget(sub)
        self._text = QTextEdit()
        self._text.setPlainText(current_text)
        self._body.addWidget(self._text, 1)
        self._body.addLayout(card_button_bar(self, self.tr("Save"), self._apply))
        self._show_over()

    def _apply(self):
        text = self._text.toPlainText().strip()
        cb = self._on_pick
        self._finish()
        if cb:
            cb(text)

    def _cancel(self):
        self._finish()


# ---------------------------------------------------------------------------
# Variant/add-file picker — one or more additional files from a mod's Nexus
# file list, added as new separate optional export rows.
# ---------------------------------------------------------------------------

class _VariantPickerOverlay(CardOverlay):
    CARD_W = 480
    CARD_H = 440

    def __init__(self, host, mod_name, files, on_pick):
        super().__init__(host)
        self._on_pick = on_pick
        self._body.addWidget(card_title(self.tr("Add variant files — {0}").format(mod_name)))
        sub = QLabel(self.tr(
            "Select one or more files to add as separate optional entries "
            "(e.g. offer several resolutions and let the installer choose):"))
        sub.setObjectName("CardSub")
        sub.setWordWrap(True)
        self._body.addWidget(sub)

        from gui_qt.nexus.nexus_file_chooser import (
            installable_files, _fmt_size_bytes, _CATEGORY_HEADER, _CATEGORY_LABEL,
        )
        self._list = QListWidget()
        current_cat = None
        for f in installable_files(files):
            up = (f.category_name or "").upper()
            if up != current_cat:
                header = QListWidgetItem(
                    self.tr(_CATEGORY_HEADER.get(up, _CATEGORY_LABEL.get(up, up))))
                header.setFlags(Qt.NoItemFlags)
                self._list.addItem(header)
                current_cat = up
            size = (f.size_in_bytes if f.size_in_bytes is not None
                   else (f.size_kb or 0) * 1024)
            label = f"{f.name or f.version}  ({_fmt_size_bytes(size)})"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, f)
            self._list.addItem(item)
        self._body.addWidget(self._list, 1)
        self._body.addLayout(card_button_bar(self, self.tr("Add"), self._apply))
        self._show_over()

    def _apply(self):
        picked = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if (item.flags() & Qt.ItemIsUserCheckable) and item.checkState() == Qt.Checked:
                picked.append(item.data(Qt.UserRole))
        cb = self._on_pick
        self._finish()
        if cb and picked:
            cb(picked)

    def _cancel(self):
        self._finish()


class CreateCollectionView(QWidget):
    _my_collections_ready = Signal(object)
    _categories_ready = Signal(object)
    _build_done = Signal(bool, str, object)   # ok, message, extra (path or url)
    _progress = Signal(object, object, str)
    # (data_idx, options) from the version-fetch worker → UI thread.
    _versions_ready = Signal(int, object)
    # (data_idx, files) from the variant-file-fetch worker → UI thread.
    _variant_files_ready = Signal(int, object)

    def __init__(self, window, game, api, log_fn=None):
        super().__init__()
        self._window = window
        self._game = game
        self._api = api
        self._log = log_fn or (lambda _m: None)
        self._game_domain = getattr(game, "nexus_game_domain", "") or ""
        self._my_collections: list = []

        self._all_rows: list[dict] = []
        self._rows: list[dict] = []   # filtered/sorted view
        self._search_text = ""

        self.setObjectName("CreateCollectionView")
        self._my_collections_ready.connect(self._on_my_collections_ready)
        self._categories_ready.connect(self._on_categories_ready)
        self._build_done.connect(self._on_build_done)
        self._progress.connect(self._on_progress)
        self._versions_ready.connect(self._on_versions_ready)
        self._variant_files_ready.connect(self._on_variant_files_ready)
        self._build()
        self._load_rows()
        self._apply_filter()
        if self._api is not None:
            threading.Thread(target=self._fetch_my_collections,
                             daemon=True, name="collection-list").start()
            threading.Thread(target=self._fetch_categories,
                             daemon=True, name="collection-categories").start()

    # -- construction ---------------------------------------------------
    def _qss(self) -> str:
        p = active_palette()
        c = lambda k: _c(p, k)
        return f"""
        #CreateCollectionView {{ background: {c('BG_DEEP')}; }}
        #CCTitleBar {{ background: {c('BG_HEADER')};
                       border-bottom: 1px solid {c('BORDER')}; }}
        #CCTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 15px; }}
        #CCToolbar {{ background: {c('BG_HEADER')}; }}
        QLabel {{ color: {c('TEXT_MAIN')}; }}
        QTableWidget {{ background: {c('BG_DEEP')}; color: {c('TEXT_MAIN')};
                        gridline-color: {c('BORDER')}; border: none; }}
        QHeaderView::section {{ background: {c('BG_HEADER')};
                        color: {c('TEXT_MAIN')}; border: none;
                        border-bottom: 1px solid {c('BORDER')}; padding: 4px; }}
        """

    def _build(self):
        self.setStyleSheet(self._qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QWidget(); bar.setObjectName("CCTitleBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self.tr("Create / Publish Collection"))
        title.setObjectName("CCTitle")
        hb.addWidget(title); hb.addStretch(1)
        close = QPushButton(self.tr("✕ Close")); close.setObjectName("FormButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._close)
        hb.addWidget(close)
        root.addWidget(bar)

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(16, 16, 16, 8)
        form.setSpacing(10)

        self._target = QComboBox()
        self._target.addItem(self.tr("New collection"), None)
        self._target.currentIndexChanged.connect(self._on_target_changed)
        form.addRow(self.tr("Publish to:"), self._target)

        self._name = QLineEdit()
        self._name.setPlaceholderText(self.tr("3–36 characters"))
        form.addRow(self.tr("Name"), self._name)

        self._description = QTextEdit()
        self._description.setFixedHeight(70)
        form.addRow(self.tr("Description"), self._description)

        self._instructions = QTextEdit()
        self._instructions.setFixedHeight(70)
        self._instructions.setPlaceholderText(
            self.tr("Shown to the user before installation starts (Markdown supported)"))
        form.addRow(self.tr("Instructions"), self._instructions)

        self._category = QComboBox()
        self._category.addItem(self.tr("(none)"), 0)
        form.addRow(self.tr("Category"), self._category)

        self._adult = QCheckBox(self.tr("Contains adult content"))
        form.addRow("", self._adult)

        self._listed = QCheckBox(self.tr("Publish as listed (public)"))
        self._listed.setChecked(True)
        form.addRow("", self._listed)

        self._recommend_new_profile = QCheckBox(self.tr("Recommend new profile"))
        self._recommend_new_profile.setChecked(True)
        form.addRow("", self._recommend_new_profile)

        self._exclude_plugin_rules = QCheckBox(self.tr("Exclude plugin rules"))
        form.addRow("", self._exclude_plugin_rules)

        root.addWidget(form_host)

        # Toolbar: search + mod count, above the per-mod table.
        tb = QWidget(); tb.setObjectName("CCToolbar")
        tl = QHBoxLayout(tb); tl.setContentsMargins(12, 6, 12, 6); tl.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search mods…"))
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(self._on_search)
        tl.addWidget(self._search)
        tl.addStretch(1)
        self._count_label = QLabel("")
        tl.addWidget(self._count_label)
        root.addWidget(tb)

        # Per-mod table.
        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels([
            self.tr("Mod Name"), self.tr("Source"), self.tr("File"),
            self.tr("Optional"), self.tr("Fomod"), self.tr("Update Policy"),
            self.tr("Instructions"), self.tr(""),
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_NAME, QHeaderView.Stretch)
        for col in (_COL_SOURCE, _COL_VERSION, _COL_OPTIONAL, _COL_FOMOD,
                   _COL_POLICY, _COL_INSTRUCTIONS, _COL_ACTIONS):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        root.addWidget(self._table, 1)

        btn_row = QWidget()
        bl = QHBoxLayout(btn_row); bl.setContentsMargins(16, 8, 16, 16)
        bl.addStretch(1)
        export_btn = QPushButton(self.tr("Export to file…"))
        export_btn.setObjectName("FormButton")
        export_btn.setCursor(Qt.PointingHandCursor)
        export_btn.clicked.connect(self._on_export_file)
        bl.addWidget(export_btn)
        publish_btn = QPushButton(self.tr("Publish to Nexus"))
        publish_btn.setObjectName("PrimaryButton")
        publish_btn.setCursor(Qt.PointingHandCursor)
        publish_btn.clicked.connect(self._on_publish)
        bl.addWidget(publish_btn)
        root.addWidget(btn_row)

    # -- data -------------------------------------------------------------
    def _profile_dir(self) -> "Path | None":
        pd = getattr(self._game, "_active_profile_dir", None) if self._game else None
        return Path(pd) if pd else None

    def _load_rows(self):
        """Export rows from the active profile's modlist, highest-priority
        first — same order/contract as ExportProfileView._load_rows. Seeds
        collection-level fields from <profile>/collection.json when present."""
        from Utils.mods.modlist import read_modlist
        pd = self._profile_dir()
        modlist_path = (pd / "modlist.txt") if pd else None
        if not self._game or not modlist_path or not modlist_path.is_file():
            self._all_rows = []
            return
        entries = [e for e in reversed(read_modlist(modlist_path))
                  if not e.is_separator]
        rows = profile_export.load_rows(entries, self._game)
        for row in rows:
            row.setdefault("update_policy", "exact")
            row.setdefault("instructions", "")
            row.setdefault("is_variant", False)
        self._all_rows = rows

        seeded = collection_export.read_profile_manifest(pd)
        if seeded:
            info = seeded.get("info") or {}
            if info.get("description"):
                self._description.setPlainText(info["description"])
            if info.get("installInstructions"):
                self._instructions.setPlainText(info["installInstructions"])
            cc = seeded.get("collectionConfig") or {}
            if "recommendNewProfile" in cc:
                self._recommend_new_profile.setChecked(bool(cc["recommendNewProfile"]))
            if "excludePluginRules" in cc:
                self._exclude_plugin_rules.setChecked(bool(cc["excludePluginRules"]))

    # -- filter / render ----------------------------------------------------
    def _apply_filter(self):
        if self._search_text:
            q = self._search_text
            rows = [r for r in self._all_rows if q in r["name"].lower()]
        else:
            rows = list(self._all_rows)
        self._rows = rows
        exportable = sum(1 for r in self._all_rows if r.get("enabled") is not False)
        self._count_label.setText(
            self.tr("{0} (of {1} in the modlist)").format(exportable, len(self._all_rows)))
        self._rebuild_table()

    def _on_search(self, text: str):
        self._search_text = (text or "").lower()
        self._apply_filter()

    def _rebuild_table(self):
        t = self._table
        t.setRowCount(0)
        t.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            data_idx = self._all_rows.index(row)

            name_item = QTableWidgetItem(row["name"])
            name_item.setFlags(Qt.ItemIsEnabled)
            t.setItem(i, _COL_NAME, name_item)

            src = row.get("source", "nexus")
            src_btn = QPushButton(self.tr(SOURCE_LABELS.get(src, "Nexus")))
            src_btn.setCursor(Qt.PointingHandCursor)
            src_btn.setStyleSheet(source_button_qss(src))
            src_btn.clicked.connect(lambda _=False, di=data_idx: self._pick_source(di))
            t.setCellWidget(i, _COL_SOURCE, src_btn)

            ver_btn = QPushButton(row.get("ver_label", "—"))
            ver_btn.setCursor(Qt.PointingHandCursor)
            ver_btn.clicked.connect(lambda _=False, di=data_idx: self._pick_version(di))
            t.setCellWidget(i, _COL_VERSION, ver_btn)

            opt_chk = center_checkbox(
                row.get("optional", False),
                lambda ch, di=data_idx: self._set_optional(di, ch))
            t.setCellWidget(i, _COL_OPTIONAL, opt_chk)

            if row.get("has_fomod"):
                fomod_chk = center_checkbox(
                    row.get("fomod_export", True),
                    lambda ch, di=data_idx: self._set_fomod(di, ch))
                t.setCellWidget(i, _COL_FOMOD, fomod_chk)
            else:
                dash = QTableWidgetItem("—")
                dash.setFlags(Qt.ItemIsEnabled)
                dash.setTextAlignment(Qt.AlignCenter)
                t.setItem(i, _COL_FOMOD, dash)

            policy_combo = QComboBox()
            for value in _POLICY_ORDER:
                policy_combo.addItem(self.tr(_POLICY_LABELS[value]), value)
            cur_idx = policy_combo.findData(row.get("update_policy") or "exact")
            policy_combo.setCurrentIndex(cur_idx if cur_idx >= 0 else 0)
            policy_combo.currentIndexChanged.connect(
                lambda _i, di=data_idx, cb=policy_combo: self._set_update_policy(di, cb.currentData()))
            t.setCellWidget(i, _COL_POLICY, policy_combo)

            instr_btn = QPushButton(self.tr("Edit…") if row.get("instructions") else self.tr("+ Add"))
            instr_btn.setObjectName("FormButton")
            instr_btn.setCursor(Qt.PointingHandCursor)
            instr_btn.clicked.connect(lambda _=False, di=data_idx: self._edit_instructions(di))
            t.setCellWidget(i, _COL_INSTRUCTIONS, instr_btn)

            if row.get("is_variant"):
                rm_btn = QPushButton(self.tr("Remove"))
                rm_btn.setCursor(Qt.PointingHandCursor)
                rm_btn.clicked.connect(lambda _=False, di=data_idx: self._remove_variant(di))
                t.setCellWidget(i, _COL_ACTIONS, rm_btn)
            elif row.get("mod_id") and src == "nexus":
                add_btn = QPushButton(self.tr("+ Variant"))
                add_btn.setObjectName("FormButton")
                add_btn.setCursor(Qt.PointingHandCursor)
                add_btn.clicked.connect(lambda _=False, di=data_idx: self._add_variant(di))
                t.setCellWidget(i, _COL_ACTIONS, add_btn)
            else:
                dash2 = QTableWidgetItem("—")
                dash2.setFlags(Qt.ItemIsEnabled)
                dash2.setTextAlignment(Qt.AlignCenter)
                t.setItem(i, _COL_ACTIONS, dash2)

    # -- cell actions -------------------------------------------------------
    def _set_optional(self, data_idx: int, checked: bool):
        self._all_rows[data_idx]["optional"] = bool(checked)

    def _set_fomod(self, data_idx: int, checked: bool):
        self._all_rows[data_idx]["fomod_export"] = bool(checked)

    def _set_update_policy(self, data_idx: int, value):
        if value:
            self._all_rows[data_idx]["update_policy"] = value

    def _pick_source(self, data_idx: int):
        row = self._all_rows[data_idx]

        def _picked(source, url, di=data_idx):
            r = self._all_rows[di]
            r["source"] = source
            r["direct_url"] = url
            self._apply_filter()

        allowed = ("nexus", "modio", "direct", "browse", "bundle", "ignore") if row.get("is_modio") \
            else ("nexus", "direct", "browse", "bundle", "ignore")
        SourceOverlay(self.window(), row["name"], row.get("source", "nexus"),
                      row.get("direct_url", ""), _picked, allowed_sources=allowed)

    def _pick_version(self, data_idx: int):
        row = self._all_rows[data_idx]
        if (not row.get("versions_fetched") and row.get("mod_id")
                and self._api is not None and row.get("source", "nexus") == "nexus"):
            row["versions_fetched"] = True
            threading.Thread(
                target=self._fetch_versions, args=(data_idx,),
                daemon=True, name="collection-versions").start()
        self._open_version_dialog(data_idx)

    def _open_version_dialog(self, data_idx: int):
        row = self._all_rows[data_idx]
        options = row.get("ver_options") or [
            {"label": row.get("ver_label", "—"), "name": "", "size_bytes": 0}]

        def _picked(sel, di=data_idx):
            r = self._all_rows[di]
            r["ver_label"] = sel.get("label", r["ver_label"])
            r["size_bytes"] = sel.get("size_bytes", 0)
            try:
                r["file_id"] = int(r["ver_label"].split(" — ")[0])
            except (ValueError, IndexError):
                pass
            self._apply_filter()

        VersionOverlay(self.window(), row["name"], options,
                       row.get("ver_label", "—"), _picked)

    def _fetch_versions(self, data_idx: int):
        row = self._all_rows[data_idx]
        try:
            result = self._api.get_mod_files(self._game_domain, row["mod_id"])
            files = result.files if result else []
        except Exception:
            files = []
        sorted_files = sorted(files, key=lambda f: f.uploaded_timestamp, reverse=True)
        options = [
            {
                "label": f"{f.file_id} — {f.version}",
                "name": f.name,
                "size_bytes": (f.size_in_bytes if f.size_in_bytes is not None
                               else (f.size_kb or 0) * 1024),
            }
            for f in sorted_files if f.file_id
        ]
        self._versions_ready.emit(data_idx, options)

    def _on_versions_ready(self, data_idx: int, options):
        if not options:
            return
        row = self._all_rows[data_idx]
        row["ver_options"] = options
        cur_label = row["ver_label"]
        is_placeholder = (not cur_label or cur_label == "—" or " — " not in cur_label)
        if is_placeholder:
            preferred = str(row["file_id"])
            matched = next(
                (o for o in options if o["label"].startswith(preferred + " —")), None)
            selected = matched or options[0]
            row["ver_label"] = selected["label"]
            row["size_bytes"] = selected["size_bytes"]
            try:
                row["file_id"] = int(selected["label"].split(" — ")[0])
            except (ValueError, IndexError):
                pass
            self._apply_filter()

    def _edit_instructions(self, data_idx: int):
        row = self._all_rows[data_idx]

        def _picked(text, di=data_idx):
            self._all_rows[di]["instructions"] = text
            self._apply_filter()

        _InstructionsOverlay(self.window(), row["name"], row.get("instructions", ""), _picked)

    # -- variant files --------------------------------------------------
    def _add_variant(self, data_idx: int):
        row = self._all_rows[data_idx]
        if not row.get("mod_id") or self._api is None:
            self._notify(self.tr("No Nexus mod id for this row."), "warning")
            return
        threading.Thread(
            target=self._fetch_variant_files, args=(data_idx,),
            daemon=True, name="collection-variant-files").start()

    def _fetch_variant_files(self, data_idx: int):
        row = self._all_rows[data_idx]
        try:
            result = self._api.get_mod_files(self._game_domain, row["mod_id"])
            files = result.files if result else []
        except Exception as exc:
            self._log(f"[collection] could not fetch files for '{row['name']}': {exc}")
            files = []
        self._variant_files_ready.emit(data_idx, files)

    def _on_variant_files_ready(self, data_idx: int, files):
        if not files:
            self._notify(self.tr("No files found for this mod."), "warning")
            return
        row = self._all_rows[data_idx]

        def _picked(chosen_files, di=data_idx):
            base = self._all_rows[di]
            for f in chosen_files:
                self._add_variant_row(base, f)
            self._apply_filter()

        _VariantPickerOverlay(self.window(), row["name"], files, _picked)

    def _add_variant_row(self, base: dict, f) -> None:
        size = f.size_in_bytes if f.size_in_bytes is not None else (f.size_kb or 0) * 1024
        label = (f.version or f.name or "").strip() or str(f.file_id)
        self._all_rows.append({
            "name": f"{base['name']} ({label})",
            "mod_id": base.get("mod_id", 0),
            "file_id": f.file_id,
            "version": f.version or "",
            "category_id": base.get("category_id", 0),
            "category_name": base.get("category_name", ""),
            "ver_label": f"{f.file_id} — {f.version}",
            "ver_options": [],
            "optional": True,
            "has_fomod": False,
            "has_bain": False,
            "fomod_export": False,
            "versions_fetched": False,
            "size_bytes": size,
            "root_folder": base.get("root_folder", False),
            "enabled": True,
            "locked": False,
            "source": "nexus",
            "direct_url": "",
            "update_policy": "exact",
            "instructions": "",
            "is_variant": True,
        })

    def _remove_variant(self, data_idx: int):
        row = self._all_rows[data_idx]
        if not row.get("is_variant"):
            return
        self._all_rows.remove(row)
        # Deferred: this runs from the row's own "Remove" button click, and a
        # synchronous rebuild here would delete that button mid-signal.
        QTimer.singleShot(0, self._apply_filter)

    # -- collections/categories -------------------------------------------
    def _fetch_my_collections(self):
        try:
            cols = self._api.get_all_my_collections()
        except Exception as exc:
            self._log(f"[collection] could not list your collections: {exc}")
            cols = []
        self._my_collections_ready.emit(cols)

    def _fetch_categories(self):
        try:
            cats = self._api.get_collection_categories()
        except Exception as exc:
            self._log(f"[collection] could not list categories: {exc}")
            cats = []
        self._categories_ready.emit(cats)

    def _on_my_collections_ready(self, cols):
        self._my_collections = list(cols or [])
        for col in self._my_collections:
            if col.game_domain and self._game_domain and col.game_domain != self._game_domain:
                continue
            self._target.addItem(f"{col.name} ({col.slug})", col)

    def _on_categories_ready(self, cats):
        for cat_id, name in cats or []:
            self._category.addItem(name, cat_id)

    def _on_target_changed(self, _idx: int):
        col = self._target.currentData()
        if col is None:
            return
        self._name.setText(col.name)
        self._description.setPlainText(col.description or col.summary or "")
        if col.category_id:
            idx = self._category.findData(col.category_id)
            if idx >= 0:
                self._category.setCurrentIndex(idx)

    def _collect_info(self) -> "dict | None":
        name = self._name.text().strip()
        err = collection_export.validate_collection_name(name)
        if err:
            self._notify(err, "warning")
            return None
        return {
            "name": name,
            "description": self._description.toPlainText().strip(),
            "installInstructions": self._instructions.toPlainText().strip(),
            "recommendNewProfile": self._recommend_new_profile.isChecked(),
            "excludePluginRules": self._exclude_plugin_rules.isChecked(),
            "gameVersions": [],
        }

    # -- export to file -----------------------------------------------------
    def _on_export_file(self):
        info = self._collect_info()
        if info is None:
            return
        from Utils.wine_proton.portal_filechooser import pick_save_file
        default_name = f"{info['name'] or 'collection'}.7z"
        pick_save_file(
            self.tr("Export Collection"),
            lambda path: self._start_worker(self._export_worker, (str(path), info))
            if path else None,
            current_name=default_name,
            filters=[(self.tr("7-Zip Archive (*.7z)"), ["*.7z"]),
                     (self.tr("All files"), ["*"])])

    def _export_worker(self, out_path: str, info: dict):
        try:
            final, warnings = collection_export.export_collection(
                out_path, self._all_rows, self._game, info,
                progress_cb=self._make_progress_cb(), log_fn=self._log)
            for w in warnings:
                self._log(f"[collection] {w}")
            self._build_done.emit(True, str(final), None)
        except Exception as exc:
            self._build_done.emit(False, str(exc), None)

    # -- publish --------------------------------------------------------
    def _on_publish(self):
        if self._api is None:
            self._notify(self.tr("Sign in to Nexus first."), "warning")
            return
        info = self._collect_info()
        if info is None:
            return
        col = self._target.currentData()
        self._start_worker(self._publish_worker, (info, col))

    def _start_worker(self, target, args):
        self._progress.emit(0, 0, self.tr("Preparing…"))
        threading.Thread(target=target, args=args, daemon=True,
                         name="collection-publish").start()

    def _publish_worker(self, info: dict, existing):
        import tempfile
        try:
            manifest, bundle_jobs, warnings = collection_export.build_collection_manifest(
                self._all_rows, self._game, info, progress_cb=self._make_progress_cb())
            if not manifest["mods"]:
                raise ValueError("No exportable mods.")
            for w in warnings:
                self._log(f"[collection] {w}")

            tmp_path = Path(tempfile.gettempdir()) / f"{info['name']}.7z"
            archive = collection_export.pack_collection(
                tmp_path, manifest, bundle_jobs,
                progress_cb=self._make_progress_cb(), log_fn=self._log)

            ok, size_msg = collection_export.check_upload_size(archive, bundle_jobs)
            if size_msg:
                self._log(f"[collection] {size_msg}")
            if not ok:
                raise ValueError(size_msg)

            self._progress.emit(0, 0, self.tr("Requesting upload URL…"))
            upload = self._api.get_collection_upload_url()
            if upload is None:
                raise ValueError("Could not get an upload URL from Nexus.")

            self._progress.emit(0, 0, self.tr("Uploading…"))
            up_ok, up_detail = self._api.upload_collection_archive(
                upload["url"], archive,
                progress_cb=lambda done, total: self._progress.emit(
                    done, total, self.tr("Uploading…")))
            if not up_ok:
                raise ValueError(f"Upload failed: {up_detail}")

            self._progress.emit(0, 0, self.tr("Creating revision…"))
            if existing is not None:
                result = self._api.create_or_update_revision(
                    manifest, existing.id, upload["uuid"])
            else:
                result = self._api.create_collection(manifest, upload["uuid"])
            if result is None:
                raise ValueError("Nexus rejected the collection.")

            revision = result.get("revision") or {}
            collection = result.get("collection") or {}
            revision_id = revision.get("id")
            slug = collection.get("slug", "")

            if revision_id:
                self._progress.emit(0, 0, self.tr("Publishing…"))
                self._api.publish_revision(
                    revision_id, listed=self._listed.isChecked(),
                    adult_content=self._adult.isChecked())

            try:
                Path(archive).unlink(missing_ok=True)
            except OSError:
                pass

            url = f"https://www.nexusmods.com/{self._game_domain}/collections/{slug}" if slug else ""
            self._build_done.emit(True, url or "Collection uploaded.", url)
        except Exception as exc:
            self._build_done.emit(False, str(exc), None)

    def _make_progress_cb(self):
        import time
        last_emit = [0.0]

        def _cb(done, total, phase):
            now = time.monotonic()
            if total and done < total and now - last_emit[0] < 0.1:
                return
            last_emit[0] = now
            self._progress.emit(done, total, str(phase))
        return _cb

    def _progress_popup(self):
        win = self._window
        ensure = getattr(win, "_ensure_feedback", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:
                pass
        return getattr(win, "_progress_popup", None)

    def _on_progress(self, done, total, phase: str):
        done, total = int(done or 0), int(total or 0)
        popup = self._progress_popup()
        if popup is None:
            return
        popup.set_progress(done, total, phase,
                           title=self.tr("Collection"),
                           bytes_mode=total > 0, key="collection-publish")

    def _on_build_done(self, ok: bool, message: str, extra):
        popup = self._progress_popup()
        if popup is not None:
            if ok:
                QTimer.singleShot(900, lambda: popup.clear(key="collection-publish"))
            else:
                popup.clear(key="collection-publish")
        if ok:
            self._notify(message, "info")
            self._log(f"[collection] {message}")
        else:
            self._notify(self.tr("Collection failed: {0}").format(message), "error")
            self._log(f"[collection] failed: {message}")

    # -- misc -------------------------------------------------------------
    def _close(self):
        tabs = getattr(self._window, "_tabs", None)
        if tabs is not None:
            try:
                tabs.close_tab("create_collection")
                if getattr(self._window, "_create_collection_view", None) is self:
                    self._window._create_collection_view = None
                return
            except Exception:
                pass
        self.hide()

    def _notify(self, text: str, state: str = "info"):
        n = getattr(self._window, "_notify", None)
        if callable(n):
            n(text, state)
        else:
            self._log(text)

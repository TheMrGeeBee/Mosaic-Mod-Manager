"""Create/Publish Collection — a modlist-scoped tab (like Export Profile)
that packages the current profile's enabled mods into a real Nexus/Vortex
collection archive, and can publish it as a draft revision on Nexus.

Collection info (name/description/category/adult content) + a choice of
"new collection" or "update one you already own", then either "Export to
file" (local .7z, for manual Vortex import) or "Publish to Nexus" (upload +
create/revise a draft revision — the final "Publish" listing action still
happens on nexusmods.com, matching Vortex's own flow: Nexus's API has no
endpoint to flip a draft straight to published).

Deliberately v1-scoped: no per-mod source/version editing table (everything
enabled in the modlist exports as its Nexus/bundle source); see
Utils.collections.collection_export's module docstring for what else is
deferred. All packaging logic lives in the neutral collection_export module.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QComboBox, QTextEdit,
)

from gui_qt.theme.theme_qt import active_palette, _c
from Utils.collections import collection_export
from Utils.profile import profile_export


class CreateCollectionView(QWidget):
    _my_collections_ready = Signal(object)
    _categories_ready = Signal(object)
    _build_done = Signal(bool, str, object)   # ok, message, extra (path or url)
    _progress = Signal(object, object, str)

    def __init__(self, window, game, api, log_fn=None):
        super().__init__()
        self._window = window
        self._game = game
        self._api = api
        self._log = log_fn or (lambda _m: None)
        self._game_domain = getattr(game, "nexus_game_domain", "") or ""
        self._my_collections: list = []

        self.setObjectName("CreateCollectionView")
        self._my_collections_ready.connect(self._on_my_collections_ready)
        self._categories_ready.connect(self._on_categories_ready)
        self._build_done.connect(self._on_build_done)
        self._progress.connect(self._on_progress)
        self._build()
        self._load_counts()
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
        QLabel {{ color: {c('TEXT_MAIN')}; }}
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
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)

        self._count_label = QLabel("")
        form.addRow(self.tr("Mods to export:"), self._count_label)

        self._target = QComboBox()
        self._target.addItem(self.tr("New collection"), None)
        self._target.currentIndexChanged.connect(self._on_target_changed)
        form.addRow(self.tr("Publish to:"), self._target)

        self._name = QLineEdit()
        self._name.setPlaceholderText(self.tr("3–36 characters"))
        form.addRow(self.tr("Name"), self._name)

        self._description = QTextEdit()
        self._description.setFixedHeight(90)
        form.addRow(self.tr("Description"), self._description)

        self._category = QComboBox()
        self._category.addItem(self.tr("(none)"), 0)
        form.addRow(self.tr("Category"), self._category)

        self._adult = QCheckBox(self.tr("Contains adult content"))
        form.addRow("", self._adult)

        self._listed = QCheckBox(self.tr("Publish as listed (public)"))
        self._listed.setChecked(True)
        form.addRow("", self._listed)

        root.addWidget(form_host)
        root.addStretch(1)

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

    def _load_counts(self):
        rows = self._load_rows()
        self._rows = rows
        exportable = sum(1 for r in rows if r.get("enabled") is not False)
        self._count_label.setText(
            self.tr("{0} (of {1} in the modlist)").format(exportable, len(rows)))
        seeded = collection_export.read_profile_manifest(self._profile_dir())
        if seeded:
            info = seeded.get("info") or {}
            if info.get("description"):
                self._description.setPlainText(info["description"])

    def _load_rows(self) -> list:
        """Export rows from the active profile's modlist, highest-priority
        first — same order/contract as ExportProfileView._load_rows."""
        from Utils.mods.modlist import read_modlist
        pd = self._profile_dir()
        modlist_path = (pd / "modlist.txt") if pd else None
        if not self._game or not modlist_path or not modlist_path.is_file():
            return []
        entries = [e for e in reversed(read_modlist(modlist_path))
                  if not e.is_separator]
        return profile_export.load_rows(entries, self._game)

    def _fetch_my_collections(self):
        try:
            cols = self._api.get_my_collections()
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
                out_path, self._rows, self._game, info,
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
                self._rows, self._game, info, progress_cb=self._make_progress_cb())
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

"""Change Version overlay for mod.io mods — lists a mod's released files on
mod.io so the user can review and pick a version to install, mirroring the
Nexus ``ChangeVersionView`` (see change_version_view.py). Opens as a
plugins-panel-scoped tab.

Simpler than the Nexus version: mod.io has no premium/free download fork
(files are always public, pre-signed URLs) and no "does this file's name
match the installed mod" logic — ``ModioAPI.get_mod_files(mod_id)`` returns
one continuous version history for the one mod, already sorted newest-first.
mod.io files also carry a per-version changelog, surfaced here as a tooltip.

The file list is fetched on a daemon thread (a Signal marshals the result
back to the UI thread — never a QThread). Installing a chosen version reuses
the app's mod.io install pipeline via the *install_fn* callback (app.py owns
the ``_install_paths`` + meta-stamping sequencing — this view only downloads
the archive and hands it off).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal, QT_TRANSLATE_NOOP
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from gui_qt.theme.theme_qt import active_palette, _c, danger_close_button, button_qss, qc
from gui_qt.safe_emit import safe_emit
from Utils.mods.mod_files_versions import fmt_size

# Translated at display time (setHorizontalHeaderLabels); register for lupdate.
_COLS = [
    QT_TRANSLATE_NOOP("ModioChangeVersionView", "File"),
    QT_TRANSLATE_NOOP("ModioChangeVersionView", "Version"),
    QT_TRANSLATE_NOOP("ModioChangeVersionView", "Date Added"),
    QT_TRANSLATE_NOOP("ModioChangeVersionView", "Size"),
    "",
]


def _hl_colors(p: dict | None = None) -> dict:
    p = p or active_palette()
    return {
        "installed_bg": qc(p, "BG_GREEN_DEEP"),
        "installed_fg": qc(p, "TEXT_OK_BRIGHT"),
        "latest_bg":    qc(p, "BG_ORANGE_DEEP"),
        "latest_fg":    qc(p, "STATUS_QUEUED"),
    }


def _fmt_date(unix_ts: int) -> str:
    if not unix_ts:
        return ""
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


class ModioChangeVersionView(QWidget):
    """Scoped-tab body for picking a mod.io mod version to install."""

    # (files | None, error_msg) from the fetch worker → UI thread.
    _files_ready = Signal(object, object)
    # (archive | None, file | None) from the download worker → UI thread.
    _download_done = Signal(object, object)
    # Download progress → shared popup card (key, name, done, total).
    _dl_progress = Signal(str, str, "qlonglong", "qlonglong")

    def __init__(self, api, mod_name, meta, meta_path, modio_meta_mod,
                 game_name, install_fn, on_close, log_fn=None, progress_fn=None):
        super().__init__()
        self._api = api
        self._mod_name = mod_name
        self._meta = meta
        self._meta_path = meta_path
        self._modio_meta_mod = modio_meta_mod
        self._game_name = game_name or ""
        self._install_fn = install_fn or (lambda *a, **k: None)
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        # progress_fn(key, name, downloaded, total) drives the shared download
        # popup card; total<0 marks this key finished. No-op default.
        self._progress_fn = progress_fn or (lambda key, name, d, t: None)
        self._dl_key = None
        self._installing = False
        self._key_holder: dict = {}

        def _stop(*_, kh=self._key_holder, pf=self._progress_fn):
            k = kh.pop("k", None)
            if k is not None:
                pf(k, "", 0, -1)
        self.destroyed.connect(_stop)

        self.setObjectName("ModioChangeVersionView")
        self._files_ready.connect(self._on_files_ready)
        self._download_done.connect(self._on_download_done)
        self._dl_progress.connect(
            lambda k, n, d, t: self._progress_fn(k, n, int(d), int(t)))

        self._build()
        self._start_fetch()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Change Version (mod.io) — {0}").format(self._mod_name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)

        self._ignore_cb = QCheckBox(self.tr("Ignore Update"))
        self._ignore_cb.setToolTip(
            self.tr("Stop flagging this mod as having an update until a newer version "
            "than the current latest appears."))
        self._ignore_cb.setChecked(bool(getattr(self._meta, "ignore_update", False)))
        self._ignore_cb.toggled.connect(self._on_ignore_toggled)
        hb.addWidget(self._ignore_cb)

        close = danger_close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(
            [self.tr(c) if c else "" for c in _COLS])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setFocusPolicy(Qt.NoFocus)
        self._table.setShowGrid(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)        # File
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # buttons
        v.addWidget(self._table, 1)

        self._status = QLabel(self.tr("Loading files…"))
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:8px 12px;")
        v.addWidget(self._status)

    # ---- fetch ------------------------------------------------------------
    def _start_fetch(self):
        mod_id = int(getattr(self._meta, "mod_id", 0) or 0)

        def worker():
            try:
                files = self._api.get_mod_files(mod_id)
                safe_emit(self._files_ready, list(files), None)
            except Exception as exc:
                safe_emit(self._files_ready, None, str(exc))

        threading.Thread(target=worker, daemon=True,
                         name="change-version-modio-fetch").start()

    def _on_files_ready(self, files, error):
        if error is not None:
            self._status.setText(self.tr("Could not load files: {0}").format(error))
            self._status.setVisible(True)
            return
        if not files:
            self._status.setText(self.tr("No files found."))
            self._status.setVisible(True)
            return
        self._status.setVisible(False)
        self._populate(files)

    # ---- table population + highlight ------------------------------------
    def _populate(self, files):
        installed_id = int(getattr(self._meta, "file_id", 0) or 0)
        latest_id = int(getattr(self._meta, "latest_file_id", 0) or 0)
        # Every row is a version of the SAME mod (mod.io has no per-file
        # display name like Nexus does) — show the mod's name, not the raw
        # archive filename, which is unreadable (e.g. a hash-suffixed zip).
        mod_display_name = (getattr(self._meta, "name", "") or "").strip() \
            or self._mod_name

        hl = _hl_colors()
        self._table.setRowCount(len(files))
        for row, f in enumerate(files):
            is_installed = installed_id > 0 and f.file_id == installed_id
            is_latest = not is_installed and latest_id > 0 and f.file_id == latest_id
            if is_installed:
                bg, name_fg = hl["installed_bg"], hl["installed_fg"]
            elif is_latest:
                bg, name_fg = hl["latest_bg"], hl["latest_fg"]
            else:
                bg = name_fg = None

            name_text = mod_display_name + ("  ✓" if is_installed else "")
            cells = [name_text, f.version or "",
                     _fmt_date(f.date_added), fmt_size(f.filesize)]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if bg is not None:
                    it.setBackground(bg)
                if col == 0 and name_fg is not None:
                    it.setForeground(name_fg)
                if col == 0:
                    # Archive filename stays discoverable via tooltip even
                    # though it's no longer the visible label.
                    tip = f.filename or ""
                    if f.changelog:
                        tip = f"{tip}\n\n{f.changelog}" if tip else f.changelog
                    if tip:
                        it.setToolTip(tip)
                elif col == 1 and f.changelog:
                    it.setToolTip(f.changelog)
                self._table.setItem(row, col, it)

            cell = QWidget()
            if bg is not None:
                cell.setAutoFillBackground(True)
                cell.setStyleSheet(f"background:{bg.name()};")
            cb = QHBoxLayout(cell); cb.setContentsMargins(8, 4, 8, 4); cb.setSpacing(6)
            profile_url = getattr(self._meta, "profile_url", "") or ""
            if profile_url:
                view_btn = QPushButton(self.tr("View")); view_btn.setCursor(Qt.PointingHandCursor)
                view_btn.setStyleSheet(button_qss("BTN_GREY", padding="4px 10px"))
                view_btn.clicked.connect(lambda _=False, u=profile_url: self._open_url(u))
                cb.addWidget(view_btn)
            inst_btn = QPushButton(self.tr("Install")); inst_btn.setCursor(Qt.PointingHandCursor)
            inst_btn.setStyleSheet(button_qss("BTN_SUCCESS", padding="4px 10px"))
            inst_btn.clicked.connect(
                lambda _=False, ff=f: self._install_file(ff))
            cb.addWidget(inst_btn)
            cb.addStretch(1)
            self._table.setCellWidget(row, 4, cell)

    # ---- actions ----------------------------------------------------------
    def _open_url(self, url):
        try:
            from Utils.xdg import open_url
            open_url(url)
        except Exception:
            pass

    def _on_ignore_toggled(self, state):
        """Write ignore_update (+ ignored_version) to the mod's meta.ini
        (mirrors ChangeVersionView._on_ignore_toggled). The modlist flag
        refresh happens when the overlay closes."""
        try:
            self._meta.ignore_update = bool(state)
            self._meta.ignored_version = (
                self._meta.latest_version if state else "")
            self._modio_meta_mod.write_modio_meta(self._meta_path, self._meta)
        except Exception as exc:
            self._log(f"mod.io: could not save ignore flag — {exc}")

    def _install_file(self, f):
        if self._installing:
            return
        self._installing = True
        mod_id = int(getattr(self._meta, "mod_id", 0) or 0)
        dl_label = f.filename or self._mod_name
        self._log(f"mod.io: downloading {dl_label}…")
        self._dl_key = f"chvm-{mod_id}-{f.file_id}"
        self._key_holder["k"] = self._dl_key
        dl_key = self._dl_key
        self._progress_fn(dl_key, dl_label, 0, 0)

        def worker():
            archive = None
            file = None
            try:
                from Utils.config_paths import get_download_cache_dir_for_game
                full_file = self._api.get_file(mod_id, f.file_id)
                if full_file is None or not full_file.binary_url:
                    self._log(f"mod.io: could not resolve download URL for {dl_label}.")
                else:
                    dest = get_download_cache_dir_for_game(self._game_name)
                    path = self._api.download_file(
                        full_file, dest,
                        progress_cb=lambda d, t: safe_emit(
                            self._dl_progress, dl_key, dl_label, int(d), int(t)))
                    archive = str(path)
                    file = full_file
            except Exception as exc:
                self._log(f"mod.io: download error: {exc}")
            safe_emit(self._download_done, archive, file)

        threading.Thread(target=worker, daemon=True,
                         name="change-version-modio-dl").start()

    def _on_download_done(self, archive, file):
        self._installing = False
        if self._dl_key is not None:
            self._progress_fn(self._dl_key, "", 0, -1)
            self._dl_key = None
            self._key_holder.pop("k", None)
        if not archive or file is None:
            return
        self._log(f"mod.io: downloaded → {archive}; installing…")
        self._install_fn(self._mod_name, self._meta, file, archive)
        self._on_close()

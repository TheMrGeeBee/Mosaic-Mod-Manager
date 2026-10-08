"""Game version (Steam) wizard — switch a Steam game to another build with
Valve's own depots, and back.

Pages: Check → Choose → Download → Apply. The pure logic (version tables,
planning, log parsing, apply/revert with backup and rollback) lives in
``Utils.steam.depot_versions``. Mosaic never talks to Steam's servers or
holds credentials: the user pastes one ``download_depot`` line at a time into
Steam's own console (it only runs the first line of a multi-line paste), and
a watcher ticks each depot off from Steam's log.
"""

from __future__ import annotations

import html
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import err_text, ok_text
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_CHECK, _PG_CHOOSE, _PG_DOWNLOAD, _PG_APPLY = range(4)
_WATCH_MS = 2500


def _row(ok: bool, text: str) -> str:
    color = ok_text() if ok else err_text()
    return f'<span style="color:{color}">{"✓" if ok else "✗"}</span> {html.escape(text)}'


class GameVersionView(WizardViewBase):
    """Switch a Steam game's build via ``download_depot``, with a backup to
    switch back from."""

    _downloads_sig = Signal(object)      # {depot: folder or None}
    _done_enable_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None, **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Game version (Steam) — {0}").format(game.name))
        from Utils.config_paths import get_game_config_dir
        from Utils.steam import depot_versions as dv
        from Utils.wine_proton.steam_finder import game_steam_id
        self._dv = dv
        self._game_root = game.get_game_path()
        self._app_id = str(game_steam_id(game) or getattr(game, "steam_id", "") or "")
        self._state_dir = get_game_config_dir(game.name)
        self._user_file = self._state_dir / dv.USER_VERSIONS_FILENAME
        self._table = dv.load_table(self._app_id, self._user_file)
        self._exe_file = self._table.exe_file
        self._current = None
        self._entry = None
        self._plan = None
        self._depot_rows: "dict[str, tuple[QLabel, QPushButton]]" = {}
        self._found: "dict[str, object]" = {}
        self._watching = False

        self._downloads_sig.connect(self._guard(self._on_downloads))
        self._done_enable_sig.connect(self._guard(lambda: self._done_btn.setEnabled(True)))
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(_WATCH_MS)
        self._watch_timer.timeout.connect(self._poll_downloads)

        self._stack.addWidget(self._build_check_page())
        self._stack.addWidget(self._build_choose_page())
        self._stack.addWidget(self._build_download_page())
        self._stack.addWidget(self._build_run_page(self.tr("Step 4: Switch")))
        self._goto(_PG_CHECK)

    # ---- helpers ------------------------------------------------------------------
    def _button_row(self, lay, *buttons):
        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 8, 0, 0)
        rh.setSpacing(8)
        rh.addStretch(1)
        for b in buttons:
            rh.addWidget(b)
        rh.addStretch(1)
        lay.addWidget(row)

    def _deploy_active(self) -> bool:
        try:
            return bool(self._game.get_deploy_active())
        except Exception:
            return False

    def _game_running(self) -> bool:
        from Utils.exe_launch.exe_launch import _wine_process_alive
        exe = self._exe_file or getattr(self._game, "exe_name", "") or ""
        return bool(exe) and _wine_process_alive(exe)

    def _goto(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_CHECK:
            self._refresh_check()
        elif idx == _PG_CHOOSE:
            self._refresh_versions()
        if idx == _PG_DOWNLOAD:
            self._watch_timer.start()
            self._poll_downloads()
        else:
            self._watch_timer.stop()

    def _finish(self):
        self._watch_timer.stop()
        super()._finish()

    # ---- page 0: check ----------------------------------------------------------------
    def _build_check_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Check"))
        self._make_note(lay, self.tr(
            "Switch this game to another build using Valve's own downloads. You paste "
            "Steam console commands; Mosaic backs up every file it replaces and can "
            "switch back. Mods must not be deployed while switching."))
        self._check_label = QLabel("")
        self._check_label.setTextFormat(Qt.RichText)
        self._check_label.setWordWrap(True)
        lay.addSpacing(8)
        lay.addWidget(self._check_label)
        self._check_status = self._make_status(lay)
        lay.addStretch(1)
        recheck = QPushButton(self.tr("Re-check"))
        recheck.setCursor(Qt.PointingHandCursor)
        recheck.clicked.connect(self._refresh_check)
        self._restore_btn = self._orange_btn(self.tr("Restore game files"))
        self._restore_btn.clicked.connect(self._on_restore_clicked)
        self._back_btn = self._orange_btn(self.tr("Switch back"))
        self._back_btn.clicked.connect(self._on_switch_back)
        self._check_next = self._accent_btn(self.tr("Choose a version →"))
        self._check_next.clicked.connect(lambda: self._goto(_PG_CHOOSE))
        self._button_row(lay, recheck, self._restore_btn, self._back_btn, self._check_next)
        return page

    def _refresh_check(self):
        dv = self._dv
        rows, blocked = [], False
        if self._game_root is None or not self._app_id:
            self._check_label.setText(_row(False, self.tr(
                "This game needs a configured game folder and a Steam app id.")))
            for b in (self._restore_btn, self._back_btn, self._check_next):
                b.setVisible(False)
            return
        installed = dv.read_installed_depots(self._game_root, self._app_id)
        self._current = dv.capture_current(self._game_root, self._app_id, self._exe_file)
        if not installed:
            rows.append(_row(False, self.tr(
                "Couldn't read Steam's appmanifest for app {0} next to the game folder. "
                "Is this the Steam copy of the game?").format(self._app_id)))
            blocked = True
        else:
            rows.append(_row(True, self.tr("Steam app {0}: {1} — {2} installed depot(s).")
                             .format(self._app_id, self._current.label, len(installed))))
            if self._current.title:
                rows.append(_row(True, self._current.title))
        traces = dv.deployment_traces(self._game_root, deploy_active=self._deploy_active())
        deployed = bool(traces)
        rows.append(_row(not deployed, self.tr("Mods are deployed: restore first ({0}).")
                         .format("; ".join(traces)) if deployed
                         else self.tr("Mods aren't deployed.")))
        running = self._game_running()
        if running:
            rows.append(_row(False, self.tr("The game is running: close it first.")))
        state = dv.load_state(self._state_dir)
        if state:
            rows.append(_row(True, self.tr("Switched to {0} on {1} (from {2}). Switch back to "
                                           "undo it before switching again.")
                             .format(state.get("current"), (state.get("at") or "")[:10],
                                     state.get("from"))))
        self._check_label.setText("<br>".join(rows))
        self._restore_btn.setVisible(deployed)
        self._back_btn.setVisible(bool(state))
        self._back_btn.setEnabled(not deployed and not running)
        self._check_next.setVisible(not state)
        self._check_next.setEnabled(not (blocked or deployed or running))

    def _on_restore_clicked(self):
        run_restore = getattr(self._ctx, "run_restore", None)
        if run_restore is None:
            self._set_status(self._check_status, self.tr(
                "Use Restore in the main window, then press Re-check."), RED)
            return
        self._restore_btn.setEnabled(False)
        self._set_status(self._check_status, self.tr("Restoring…"))

        def _done(ok: bool):
            if self._closing:
                return
            self._restore_btn.setEnabled(True)
            self._refresh_check()
            # Always replace "Restoring…", or it reads as still running.
            self._set_status(self._check_status,
                             self.tr("Restore finished.") if ok
                             else self.tr("Restore failed — see the log."),
                             GREEN if ok else RED)

        if not run_restore(_done):
            self._restore_btn.setEnabled(True)
            self._set_status(self._check_status, self.tr(
                "Couldn't start Restore right now. Try again in a moment."), RED)

    def _on_switch_back(self):
        self._goto(_PG_APPLY)
        self._set_status(self._run_status, self.tr("Switching back…"))
        game_root, state_dir, running = self._game_root, self._state_dir, self._game_running()
        active = self._deploy_active()

        def worker():
            try:
                label = self._dv.revert_version(game_root, state_dir, game_running=running,
                                                deploy_active=active, log_fn=self._log)
                safe_emit(self._run_status_sig, self.tr("Switched back to {0}.").format(label), GREEN)
            except self._dv.VersionSwitchError as exc:
                safe_emit(self._run_status_sig, str(exc), RED)
            safe_emit(self._done_enable_sig)

        self._ran = True
        threading.Thread(target=worker, daemon=True, name="game-version-revert").start()

    # ---- page 1: choose -----------------------------------------------------------------
    def _build_choose_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Choose a version"))
        self._versions = QListWidget()
        self._versions.itemSelectionChanged.connect(self._on_version_selected)
        lay.addWidget(self._versions, 1)
        self._plan_label = QLabel("")
        self._plan_label.setWordWrap(True)
        self._plan_label.setStyleSheet(self._dim)
        lay.addWidget(self._plan_label)

        self._add_box = QWidget()
        al = QHBoxLayout(self._add_box)
        al.setContentsMargins(0, 0, 0, 0)
        self._add_label = QLineEdit()
        self._add_label.setPlaceholderText(self.tr("Version label, e.g. 1.11.221"))
        self._add_exe = QLineEdit()
        self._add_exe.setPlaceholderText(self.tr("Exe version (optional), e.g. 1.11.221.0"))
        al.addWidget(self._add_label)
        al.addWidget(self._add_exe)
        self._add_paste = QPlainTextEdit()
        self._add_paste.setPlaceholderText(self.tr(
            "Paste depot and manifest ids from SteamDB, one depot per line:\n"
            "377162   5847529232406005096"))
        self._add_paste.setMaximumHeight(110)
        lay.addWidget(self._add_box)
        lay.addWidget(self._add_paste)
        self._add_status = self._make_status(lay)

        steamdb = QPushButton(self.tr("Open SteamDB depots"))
        steamdb.setCursor(Qt.PointingHandCursor)
        steamdb.clicked.connect(lambda: self._open_url(
            f"https://steamdb.info/app/{self._app_id}/depots/"))
        save = QPushButton(self.tr("Add this version"))
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._on_add_version)
        back = QPushButton(self.tr("← Back"))
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(lambda: self._goto(_PG_CHECK))
        self._choose_next = self._accent_btn(self.tr("Next →"))
        self._choose_next.setEnabled(False)
        self._choose_next.clicked.connect(lambda: self._goto(_PG_DOWNLOAD))
        self._button_row(lay, back, steamdb, save, self._choose_next)
        return page

    def _refresh_versions(self):
        self._table = self._dv.load_table(self._app_id, self._user_file)
        self._versions.clear()
        for e in self._table.versions:
            text = e.label + (f" — {e.title}" if e.title else "") + (
                self.tr("  (added by you)") if e.user else "")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, e.label)
            if e.source:
                item.setToolTip(e.source)
            self._versions.addItem(item)
        if not self._table.versions:
            self._plan_label.setText(self.tr(
                "Mosaic has no versions on file for this game yet. Add one from SteamDB below."))

    def _on_version_selected(self):
        items = self._versions.selectedItems()
        if not items:
            self._choose_next.setEnabled(False)
            return
        dv = self._dv
        self._entry = self._table.get(items[0].data(Qt.UserRole))
        installed = dv.read_installed_depots(self._game_root, self._app_id)
        self._plan = dv.plan_switch(self._entry, installed)
        sizes = self._installed_sizes()
        gb = sum(sizes.get(d, 0) for d, _ in self._plan.download) / 1e9
        parts = [self.tr("{0} depot(s) to download (about {1:.1f} GB at today's sizes)")
                 .format(len(self._plan.download), gb)]
        if self._plan.unchanged:
            parts.append(self.tr("{0} already match").format(len(self._plan.unchanged)))
        if self._plan.not_in_version:
            parts.append(self.tr("{0} of yours aren't in this version and stay as they are: {1}")
                         .format(len(self._plan.not_in_version), ", ".join(self._plan.not_in_version)))
        if not self._plan.download:
            parts = [self.tr("Your installed depots already match this version.")]
        self._plan_label.setText("; ".join(parts) + ".")
        self._choose_next.setEnabled(bool(self._plan.download))

    def _installed_sizes(self) -> "dict[str, int]":
        from Utils.steam.acf import get_ci, read_vdf
        path = self._dv.appmanifest_path(self._game_root, self._app_id)
        if path is None:
            return {}
        depots = get_ci(read_vdf(path), "AppState", "InstalledDepots", default={}) or {}
        return {d: int(get_ci(v, "size", default=0) or 0) for d, v in depots.items()
                if isinstance(v, dict)}

    def _on_add_version(self):
        label = self._add_label.text().strip()
        depots = self._dv.parse_depot_paste(self._add_paste.toPlainText())
        if not label or not depots:
            self._set_status(self._add_status, self.tr(
                "Enter a label and at least one line with a depot id and a manifest id."), RED)
            return
        entry = self._dv.VersionEntry(label=label, depots=depots,
                                      exe_version=self._add_exe.text().strip(),
                                      source=self.tr("Added by you from SteamDB"))
        self._dv.save_user_entry(self._user_file, entry, exe_file=self._exe_file)
        self._set_status(self._add_status, self.tr("Added {0} ({1} depot(s)).")
                         .format(label, len(depots)), GREEN)
        self._add_paste.clear()
        self._refresh_versions()

    # ---- page 2: download ------------------------------------------------------------
    def _build_download_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Download in Steam"))
        self._make_note(lay, self.tr(
            "Open Steam's console, then copy and paste the commands below one at a time "
            "(Steam only runs the first line of a paste). Each line ticks off when Steam "
            "has finished it. Your game folder isn't touched until the next step."))
        open_console = self._accent_btn(self.tr("Open Steam console"))
        open_console.clicked.connect(lambda: self._open_url("steam://open/console"))
        self._button_row(lay, open_console)
        self._depot_host = QWidget()
        self._depot_vlay = QVBoxLayout(self._depot_host)
        self._depot_vlay.setContentsMargins(0, 8, 0, 8)
        lay.addWidget(self._depot_host)
        self._dl_status = self._make_status(lay)
        lay.addStretch(1)
        back = QPushButton(self.tr("← Back"))
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(lambda: self._goto(_PG_CHOOSE))
        self._apply_btn = self._green_btn(self.tr("Switch to this version"))
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply)
        self._button_row(lay, back, self._apply_btn)
        return page

    def _build_depot_rows(self):
        while self._depot_vlay.count():
            w = self._depot_vlay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
        self._depot_rows.clear()
        self._found = {}
        for depot, manifest in self._plan.download:
            row = QWidget()
            rh = QHBoxLayout(row)
            rh.setContentsMargins(0, 0, 0, 0)
            cmd = QLineEdit(self._dv.console_command(self._app_id, depot, manifest))
            cmd.setReadOnly(True)
            copy = QPushButton(self.tr("Copy"))
            copy.setCursor(Qt.PointingHandCursor)
            copy.clicked.connect(lambda _=False, t=cmd.text(): QGuiApplication.clipboard().setText(t))
            state = QLabel(self.tr("waiting"))
            state.setMinimumWidth(110)
            rh.addWidget(cmd, 1)
            rh.addWidget(copy)
            rh.addWidget(state)
            self._depot_vlay.addWidget(row)
            self._depot_rows[depot] = (state, copy)

    def _poll_downloads(self):
        if self._plan is None or self._watching:
            return
        if not self._depot_rows:
            self._build_depot_rows()
        self._watching = True
        plan, app = list(self._plan.download), self._app_id

        def worker():
            found = {}
            try:
                log_text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                                     for p in self._dv.content_log_paths())
                for depot, manifest in plan:
                    found[depot] = self._dv.find_downloaded_depot(app, depot, manifest,
                                                                 log_text=log_text)
            finally:
                safe_emit(self._downloads_sig, found)

        threading.Thread(target=worker, daemon=True, name="game-version-watch").start()

    def _on_downloads(self, found):
        self._watching = False
        self._found = found
        done = 0
        for depot, (label, _copy) in self._depot_rows.items():
            if found.get(depot) is not None:
                done += 1
                label.setText(f'<span style="color:{ok_text()}">✓ {self.tr("downloaded")}</span>')
            else:
                label.setText(self.tr("waiting"))
        total = len(self._depot_rows)
        self._set_status(self._dl_status, self.tr("{0} of {1} depot(s) downloaded.")
                         .format(done, total), GREEN if done == total else "")
        self._apply_btn.setEnabled(total > 0 and done == total)

    # ---- page 3: apply -----------------------------------------------------------------
    def _on_apply(self):
        self._watch_timer.stop()
        self._goto(_PG_APPLY)
        self._set_status(self._run_status, self.tr("Backing up and switching… (large files take a while)"))
        entry, dirs = self._entry, {d: self._found[d] for d, _ in self._plan.download}
        from_label = self._current.label if self._current else "previous"
        game_root, state_dir, exe = self._game_root, self._state_dir, self._exe_file
        running, active = self._game_running(), self._deploy_active()

        def worker():
            try:
                self._dv.apply_version(game_root, entry, dirs, state_dir, from_label=from_label,
                                       exe_file=exe, game_running=running, deploy_active=active,
                                       log_fn=self._log)
                safe_emit(self._run_status_sig, self.tr(
                    "Switched to {0}. Launch the game from Mosaic (for Fallout 4: the "
                    "f4se_loader.exe entry), not with Play in Steam: Steam may update it back. "
                    "In Steam, set this game to \"Only update this game when I launch it\". "
                    "Switch back any time from this wizard.").format(entry.label), GREEN)
            except self._dv.VersionSwitchError as exc:
                safe_emit(self._run_status_sig, str(exc), RED)
            safe_emit(self._done_enable_sig)

        self._ran = True
        threading.Thread(target=worker, daemon=True, name="game-version-apply").start()

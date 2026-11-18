"""Qt main window — header / body / footer rows + bottom log panel.

  header row : top bar (game/profile + actions) | play bar      (fixed split)
  body row   : modlist ║ plugins                                (draggable)
  footer row : mod tools | plugin tools                         (fixed split)
  log panel  : drag-resizable log text area + control bar
"""

from __future__ import annotations

import os
import sys
import threading
import time
import time as _time
from pathlib import Path
from pathlib import Path as _P

from PySide6.QtCore import QT_TRANSLATE_NOOP, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui_qt import glue
from gui_qt.app_parts.app_bsa import MainWindow as _AppBsaMixin
from gui_qt.app_parts.app_collections import MainWindow as _AppCollectionsMixin
from gui_qt.app_parts.app_deploy import MainWindow as _AppDeployMixin
from gui_qt.app_parts.app_footers import MainWindow as _AppFootersMixin
from gui_qt.app_parts.app_game_switch import MainWindow as _AppGameSwitchMixin
from gui_qt.app_parts.app_install import MainWindow as _AppInstallMixin
from gui_qt.app_parts.app_log import MainWindow as _AppLogMixin
from gui_qt.app_parts.app_modlist import MainWindow as _AppModlistMixin
from gui_qt.app_parts.app_modlist_actions import MainWindow as _AppModlistActionsMixin
from gui_qt.app_parts.app_nexus import MainWindow as _AppNexusMixin
from gui_qt.app_parts.app_play import MainWindow as _AppPlayMixin
from gui_qt.app_parts.app_plugins import MainWindow as _AppPluginsMixin
from gui_qt.app_parts.app_profiles import MainWindow as _AppProfilesMixin
from gui_qt.app_parts.app_proton import MainWindow as _AppProtonMixin
from gui_qt.app_parts.app_tabs import MainWindow as _AppTabsMixin
from gui_qt.app_parts.app_update import MainWindow as _AppUpdateMixin
from gui_qt.app_parts.app_updates import MainWindow as _AppUpdatesMixin
from gui_qt.app_parts.app_updates import _load_bg3_modio  # noqa: F401  (re-export; tests import it from here)
from gui_qt.app_parts.app_widgets import MainWindow as _AppWidgetsMixin
from gui_qt.detachable_tabs import DetachableTabWidget
from gui_qt.game_state import GameState
from gui_qt.theme.theme_qt import active_palette, apply_theme

# Diagnostic prints here run on worker threads and use flush=True. Under
# Flatpak/AppImage stdout often has no reader, so a full pipe buffer makes
# print() raise BrokenPipeError, which killed the worker (e.g. the play/deploy
# build). safe_print swallows stream errors; the text is mirrored to the GUI
# log anyway. Shadowing the builtin makes every print(...) below crash-proof.
from Utils.app_log import safe_print as print  # noqa: A004

# Set when the user changes language: run() re-execs the process after the event
# loop exits so the whole UI rebuilds in the new language.
_RESTART_REQUESTED = False

# Quick-configure submenu labels come from the GUI-free Utils.exe_launch.quick_configure and
# are shown via self.tr(opt["label"]) / self.tr(clabel) in MainWindow — but
# lupdate can't see through that variable, so the source literals are registered
# here (context "MainWindow", matching the tr() call site) for extraction. Keep
# in sync with build_quick_configure_options().
_QUICK_CONFIGURE_TR = (
    QT_TRANSLATE_NOOP("MainWindow", "Deploy Method"),
    QT_TRANSLATE_NOOP("MainWindow", "Symlink"),
    QT_TRANSLATE_NOOP("MainWindow", "Symlink (Recommended)"),
    QT_TRANSLATE_NOOP("MainWindow", "Hardlink"),
    QT_TRANSLATE_NOOP("MainWindow", "Hardlink (Recommended)"),
    QT_TRANSLATE_NOOP("MainWindow", "Swap launcher with script extender on deploy"),
    QT_TRANSLATE_NOOP("MainWindow", "Auto deploy (on enable/disable/reorder)"),
    QT_TRANSLATE_NOOP("MainWindow", "Automatic archive invalidation (prefer loose files over BSAs)"),
    QT_TRANSLATE_NOOP("MainWindow", "Use profile-specific INI files"),
    QT_TRANSLATE_NOOP("MainWindow", "Use profile-specific saves"),
    QT_TRANSLATE_NOOP("MainWindow", "Prepend load-order numbers to mod folders"),
    QT_TRANSLATE_NOOP("MainWindow", "Game Patch Version"),
    QT_TRANSLATE_NOOP("MainWindow", "Patch 8"),
    QT_TRANSLATE_NOOP("MainWindow", "Patch 7"),
    QT_TRANSLATE_NOOP("MainWindow", "Patch 6"),
    QT_TRANSLATE_NOOP("MainWindow", "Plugins file name"),
)


class _FooterBar(QWidget):
    """A footer container whose reported height follows its FlowLayout's
    *wrapped* height at the current width. Qt evaluates a widget's sizeHint
    height at its MINIMUM width (every button on its own row), so a footer whose
    buttons only wrap when narrow otherwise reserves that fully-wrapped tall
    height — a blank gap above the search box. We report heightForWidth for the
    sizeHint/minimumSizeHint heights so the parent layout fits us exactly."""

    def _hfw(self) -> int:
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth():
            w = self.width() or self.sizeHint().width()
            return lay.heightForWidth(w)
        return -1

    def sizeHint(self) -> QSize:            # noqa: N802
        s = super().sizeHint()
        h = self._hfw()
        return QSize(s.width(), h) if h >= 0 else s

    def minimumSizeHint(self) -> QSize:     # noqa: N802
        s = super().minimumSizeHint()
        h = self._hfw()
        return QSize(s.width(), h) if h >= 0 else s

    def resizeEvent(self, e):               # noqa: N802
        super().resizeEvent(e)
        # Width changed → our wrapped height may have changed; re-fit.
        self.updateGeometry()


class _CurrentPageStack(QStackedWidget):
    """A QStackedWidget that sizes to its *currently visible* page instead of
    the tallest one.

    QStackedLayout keeps every page laid out and reserves height for the tallest
    one, so a taller hidden page (e.g. the Downloads footer wrapping its buttons
    onto a second row at narrow widths) leaves a blank row above shorter visible
    pages. Reporting the current page's size hints isn't enough — the layout's
    own minimum still wins — so we also clamp our maximum height to the current
    page's wrapped (heightForWidth) height, refreshed on resize and page swap."""

    def _current_height(self) -> int:
        w = self.currentWidget()
        if w is None:
            return -1
        if w.hasHeightForWidth():
            return w.heightForWidth(self.width() or w.sizeHint().width())
        return w.sizeHint().height()

    def clamp_to_current(self) -> None:
        """Clamp our max height to the current page so hidden (taller) pages
        don't reserve extra rows. Safe to call after a page swap or resize."""
        h = self._current_height()
        if h >= 0:
            # A max (not fixed) height is enough to drop the extra rows the
            # hidden pages reserve, without fighting the parent layout.
            self.setMaximumHeight(h)
            self.updateGeometry()

    def sizeHint(self) -> QSize:            # noqa: N802
        w = self.currentWidget()
        return w.sizeHint() if w is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:     # noqa: N802
        w = self.currentWidget()
        return w.minimumSizeHint() if w is not None else super().minimumSizeHint()

    def hasHeightForWidth(self) -> bool:    # noqa: N802
        w = self.currentWidget()
        return w.hasHeightForWidth() if w is not None else False

    def heightForWidth(self, width: int) -> int:   # noqa: N802
        w = self.currentWidget()
        if w is not None and w.hasHeightForWidth():
            return w.heightForWidth(width)
        return super().heightForWidth(width)

    def resizeEvent(self, e):               # noqa: N802
        super().resizeEvent(e)
        self.clamp_to_current()


class MainWindow(
    _AppUpdateMixin,
    _AppLogMixin,
    _AppNexusMixin,
    _AppCollectionsMixin,
    _AppPluginsMixin,
    _AppBsaMixin,
    _AppProfilesMixin,
    _AppWidgetsMixin,
    _AppProtonMixin,
    _AppPlayMixin,
    _AppUpdatesMixin,
    _AppModlistActionsMixin,
    _AppTabsMixin,
    _AppInstallMixin,
    _AppModlistMixin,
    _AppFootersMixin,
    _AppGameSwitchMixin,
    _AppDeployMixin,
    QMainWindow,
):
    # Carries (generation, ConflictData) from a worker thread to the UI thread
    # (queued connection — thread-safe). See _rebuild_conflicts_async.
    _conflicts_ready = Signal(int, object)
    # (generation, bsa_codes, bsa_overrides, bsa_overridden_by) — BSA-only
    # recompute after a plugin toggle/reorder (no filemap rebuild).
    _bsa_conflicts_ready = Signal(int, object, object, object)
    # (generation) — filemap-only rebuild finished (disable fast path: the
    # conflict scan was provably redundant). See _rebuild_filemap_light_async.
    _filemap_light_done = Signal(int)
    # (generation, list[FrameworkStatus]) from the framework-detect worker —
    # detect_frameworks reads filemap.txt + the mod index, too slow for the UI
    # thread on a big modlist. See _refresh_framework_banner.
    _framework_statuses_ready = Signal(int, object)
    # Deploy/restore worker → UI thread (thread-safe queued connections).
    _op_progress = Signal(int, int, object)   # (done, total, phase|None)
    _op_log = Signal(str)
    _op_done = Signal(str, bool, object)       # (kind, success, warnings-list)
    # Install worker → UI thread.
    _install_done = Signal(int, int, object)   # (ok_count, total, names-list)
    _prepared_ready = Signal(object)           # (PreparedInstall|None)
    _one_install_done = Signal(object)         # (installed name|None)
    # A detached FOMOD/BAIN wizard finished staging on a worker thread → UI
    # thread reports the toast / rename prompt / modlist reload. Payload:
    # (installed name|None, PreparedInstall, prev_name|None).
    _wizard_finish_done = Signal(object, object, object)
    # Parallel phase of a multi-archive install finished → UI thread starts the
    # deferred FOMOD/BAIN phase. (installed-names list, deferred-paths list)
    _install_batch_stage_done = Signal(object, object)
    # Worker asks the UI to show the Set-Prefix overlay; payload carries a
    # result holder + threading.Event the worker blocks on.
    _need_prefix = Signal(object)              # (dict with required/file_list/...)
    # Worker asks the UI to show the Mod-Already-Exists overlay (same blocking
    # holder+Event handshake as _need_prefix).
    _mod_exists = Signal(object)               # (dict with mod_name/conflict/holder/event)
    # Worker asks the UI to show the Same-Mod-Page-Different-File overlay
    # (same blocking holder+Event handshake as _mod_exists).
    _mod_variant_choice = Signal(object)       # (dict with new_name/existing_name/holder/event)
    # Deploy worker asks the UI to show the Cyberpunk CET symlink warning
    # (same blocking holder+Event handshake as _need_prefix).
    _confirm_cet = Signal(object)              # (dict with holder/event)
    # Proton-tools installer worker → UI thread.
    _proton_done = Signal(str, bool)           # (title, success)
    # Nexus validate() worker → UI thread (username or None).
    _nexus_validated = Signal(object)          # (username str | None)
    # LOOT Sort Plugins worker → UI thread (SortResult | None on error).
    _sort_plugins_ready = Signal(object)
    # LOOT record-overlap worker → UI thread: (target plugin name,
    # list[str] overlaps | None on error). Full libloot load, so off-thread.
    _overlap_ready = Signal(str, object)
    # Plugin reload worker → UI thread: (gen, rows, plugin_paths, userlist
    # state). The disk work (per-plugin header reads) runs off-thread; a
    # generation counter drops results from a superseded reload.
    _plugins_loaded = Signal(int, object, object, object)
    # Deferred ESL-eligibility worker → UI thread (gen, {name_lower: PF bit}).
    _esl_elig_ready = Signal(int, object)
    # Size-column disk walk worker → UI thread (gen, sizes, size_bytes).
    _sizes_ready = Signal(int, object, object)
    # Modlist meta.ini read worker → UI thread (gen, payload dict).
    _modlist_meta_ready = Signal(int, object)
    # Filter-data disk scan worker → UI thread (gen, payload dict | None).
    _filter_data_ready = Signal(int, object)
    # Nexus OAuth client (background thread) → UI thread.
    # (kind, payload): kind in {"token","error","status"}.
    _oauth_event = Signal(str, object)
    # Check-for-updates worker → UI thread ((updates, missing) | None on error).
    _updates_ready = Signal(object)
    # App self-update check worker → UI thread
    # ((current, latest, mode, is_prerelease, is_downgrade)).
    _app_update_found = Signal(object)
    # Manual "Check for Updates" (Settings) worker → UI thread: dismisses the
    # sticky "Checking…" toast with the outcome (text, state) — the update
    # banner itself (_app_update_found) still fires separately when one is found.
    _app_update_checked = Signal(str, str)
    # Endorse/abstain worker → UI thread ({"ok": n, "endorse": bool}).
    _endorse_done = Signal(object)
    # Endorse-Mosaic worker → UI thread ({"endorsed": bool, "notify": bool, "error": str|None}).
    _mosaic_endorse_done = Signal(object)
    # mod.io Like/Unlike worker → UI thread ({"ok": n, "like": bool, "names": [...]}).
    _modio_like_done = Signal(object)
    # "Fetch name from Nexus" worker → UI thread (overlay, fetched_name, error).
    # The overlay instance rides along so a slow fetch can't land in the wrong
    # rename dialog if the user closed it and opened another in the meantime.
    _nexus_name_fetch_done = Signal(object, str, str)
    # Track worker → UI thread ({"ok": n}).
    _track_done = Signal(object)
    # ui_hooks.warn from any backend thread → OK-only popup on the UI thread
    # ((title, message, card_h|None)).
    _warn_popup = Signal(str, str, object)
    # Copy/Move-to-profile worker → UI thread (result dict).
    _copy_done = Signal(object)
    # Collection update: staging meta.ini scan (worker) → apply (UI).
    _col_update_scan_done = Signal(object)
    # Install-a-Nexus-mod-by-id flow (used by Missing Requirements) → UI thread.
    _qu_resolved = Signal(object, object)         # (queue list, skipped list)
    _qu_downloaded = Signal(object, object)       # (dl_items list, failed list)
    _qu_dl_progress = Signal("qlonglong", "qlonglong")  # aggregate (cur_bytes, total_bytes; 64-bit: >2GB)
    # mod.io Quick Update — mirrors _qu_* above (Nexus), simpler (no premium gate).
    _qmu_resolved = Signal(object, object)        # (queue list, skipped list)
    _qmu_downloaded = Signal(object, object)      # (dl_items list, failed list)
    _qmu_dl_progress = Signal("qlonglong", "qlonglong")  # aggregate (cur_bytes, total_bytes; 64-bit: >2GB)
    _reinstall_downloaded = Signal(object, object)  # (dl_items list, failed list)
    _reinstall_dl_progress = Signal("qlonglong", "qlonglong")  # aggregate bytes (64-bit)
    # Non-premium reinstall: file metadata resolved → arm the manual flow.
    # ([(mod_name, domain, mod_id, file_id, NexusModFile-like), …]).
    _reinstall_manual_ready = Signal(object)
    # Non-premium reinstall: a browser download landed (or an existing archive
    # was found). (mod_name, archive_path, prebuilt_meta|None, dl_key).
    _reinstall_manual_found = Signal(object, object, object, object)
    _req_install_files = Signal(object, object)   # (ctx dict, files|None)
    _req_batch_ready = Signal(object, object)     # ([(entry, files|None)]|None, premium)
    _req_install_dl = Signal(object, object, object)  # (archive|None, meta|None, dl_key)
    _req_install_prog = Signal(object, object, "qlonglong", "qlonglong")  # (dl_key, name, downloaded, total bytes; 64-bit: >2GB)
    # Collection reset-load-order worker → UI thread (result dict).
    _reset_done = Signal(object)
    # Collection INSTALL worker → UI thread. Every callback is a single emit; the
    # connected slots (UI thread) update the install overlay. (verb, payload)
    # batches keep the surface small. See _install_collection.
    _col_status = Signal(str)
    _col_progress = Signal(object)             # float | None
    _col_agg = Signal("qlonglong", "qlonglong", float)  # bytes cur, total, MB/s (64-bit: >2GB)
    _col_display_total = Signal("qlonglong")   # true collection size (bytes, 64-bit)
    _col_cache_progress = Signal(int, int)     # cache_hit_done, dl_done
    _col_dl = Signal(str, object)              # ("start"|"update"|"finish", payload)
    _col_extract = Signal(str, object)         # ("queue"|"add"|"update"|"remove", payload)
    _col_row = Signal(int)                     # file_id installed
    _col_manual = Signal(object)               # manual-mode current-mod payload dict
    _col_finished = Signal(str, object)        # ("done"|"paused"|"failed"|"cancelled", payload)
    _preflight_ev = Signal(str, object)        # collection preflight worker -> UI (kind, payload)
    _appended_col_removed = Signal(str, bool)  # appended-collection remove worker → UI
    _col_import_done = Signal(object)          # (profile_name, installed, total, skipped)
    _import_file_picked = Signal(object)       # portal picker result (Path|None) → UI thread
    _export_code_ready = Signal(object, int)   # (code str|None, mod_count) share-code build → UI thread
    _install_files_picked = Signal(object)     # portal picker result (list[Path]) → UI thread
    _custom_exe_picked = Signal(object)        # portal picker result (Path|None) → UI thread
    _shared_exe_picked = Signal(object)        # portal picker result (Path|None) → UI thread
    # Worker blocks on these to show the deferred FOMOD / BAIN pickers (holder+Event).
    _col_fomod = Signal(object)
    _col_bain = Signal(object)
    # BSA/BA2 pack + unpack workers → UI thread (result dict | error str). See
    # _on_pack_bsa / _on_unpack_bsa.
    _bsa_op_done = Signal(object)
    # Custom-handler background sync worker → UI thread (files were written).
    _handlers_synced = Signal()
    # Manual force-update of one repo handler → UI thread: (game name, status).
    _handler_force_updated = Signal(str, str)
    # Language (.qm) background sync worker → UI thread (translations updated).
    _languages_synced = Signal()
    # Flatpak 32-bit extension repair worker → UI thread (install succeeded?).
    _i386_repair_done = Signal(bool)
    # NXM link received from a second instance via the IPC socket (fires on a
    # worker thread → marshal to the UI thread). Also used for --nxm at startup.
    _nxm_received = Signal(str)
    # NXM download worker → UI thread: (DownloadResult, mod_info, file_info).
    _nxm_download_done = Signal(object)

    _PLAY_BAR_W = 380       # play-bar (header right) fixed width
    _BTN_H = 42          # consistent height for all header buttons (~30% bigger)
    _ICON_PX = 24        # header button icon size
    _FOOT_BTN_H = 28     # compact height for footer tool buttons
    _FOOT_ICON_PX = 16   # footer button icon size

    def __init__(self, app, splash=None):
        super().__init__()
        self._app = app
        # Startup splash: held open past show() and dismissed on the first
        # completed conflict rebuild (the last of the heavy first-load work).
        # A watchdog closes it anyway if that signal never arrives.
        self._splash = splash
        self._splash_dismissed = False
        self._splash_shown_at = None
        if splash is not None:
            self._splash_shown_at = _time.monotonic()
        self._pal = active_palette()
        self._gs = GameState()
        self._gs.load()
        self._conflicts_ready.connect(self._on_conflicts_ready)
        self._bsa_conflicts_ready.connect(self._on_bsa_conflicts_ready)
        self._filemap_light_done.connect(self._on_filemap_light_done)
        self._framework_statuses_ready.connect(self._on_framework_statuses)
        # Drops stale framework-detect results (game switched mid-compute).
        self._framework_gen = 0
        # Deploy/restore state + notification host.
        self._deploy_running = False
        self._deploy_rerun_pending = False
        # Auto-deploy guard: a deploy triggers _reload_modlist → conflict rebuild
        # → _on_conflicts_ready; without this flag that would re-fire auto-deploy
        # in an infinite loop (Tk parity: gui.py _auto_deploy_in_progress).
        self._auto_deploy_in_progress = False
        self._op_silent = False   # silent (auto) deploy: suppress progress popup
        self._post_deploy_action = None   # launch closure run after deploy succeeds
        self._deploy_done_hooks: list = []   # wizard on_done(ok) one-shots
        self._restore_done_hooks: list = []  # same, for wizard-triggered Restore
        self._progress_popup = None
        self._notifier = None
        self._op_progress.connect(self._on_op_progress)
        self._op_log.connect(self._append_log)
        self._op_done.connect(self._on_op_done)
        self._warn_popup.connect(self._show_warn_popup)
        self._init_log_file()   # one on-disk log file per session
        self._bsa_op_running = False
        self._bsa_op_done.connect(self._on_bsa_op_done)
        self._install_running = False
        self._pending_install_batches: list[dict] = []
        # Detached FOMOD/BAIN wizards: an open wizard is pure UI wait (no
        # staging), so it must NOT hold the install pipeline — each opens in its
        # own uniquely-keyed tab and stages independently on finish. Counter for
        # unique tab keys + a queue that serialises only the staging step (which
        # mutates the shared game._active_profile_dir and MUST stay one-at-a-time
        # against installs/deploys — the wrong-profile bug _install_paths guards).
        self._wizard_seq = 0
        self._staged_finish_queue: list = []
        self._staged_finish_running = False
        # Deploy/Restore requested while a detached-wizard staging job held the
        # shared game — deferred here and re-run once the staged queue drains
        # (installs have their own _pending_install_batches queue instead).
        self._pending_after_staged: list = []
        self._active_downloads: dict[str, dict] = {}   # key → name/done/total/fin
        self._install_done.connect(self._on_install_done)
        self._prepared_ready.connect(self._on_prepared_ready)
        self._one_install_done.connect(self._on_one_install_done)
        self._wizard_finish_done.connect(self._on_wizard_finish_done)
        self._install_batch_stage_done.connect(self._on_install_batch_stage_done)
        self._need_prefix.connect(self._on_need_prefix_ui)
        self._mod_exists.connect(self._on_mod_exists_ui)
        self._mod_variant_choice.connect(self._on_mod_variant_choice_ui)
        self._confirm_cet.connect(self._on_confirm_cet_ui)
        self._proton_busy = False
        self._proton_done.connect(self._on_proton_done)
        # Game-scoped panel views (lazily built; closed on game change).
        self._profile_settings_view = None
        self._dll_overrides_view = None
        self._sort_running = False
        self._overlap_running = False
        self._sort_plugins_ready.connect(self._on_sort_plugins_ready)
        self._overlap_ready.connect(self._on_overlap_ready)
        self._plugins_gen = 0
        self._plugins_loaded.connect(self._on_plugins_loaded)
        self._esl_elig_ready.connect(self._on_esl_elig_ready)
        self._sizes_gen = 0
        self._sizes_ready.connect(self._on_sizes_ready)
        self._modlist_meta_gen = 0
        self._modlist_meta_ready.connect(self._on_modlist_meta_ready)
        self._filter_data_gen = 0
        self._filter_data_ready.connect(self._on_filter_data_ready)
        try:
            from version import __version__ as _mm_version
        except Exception:
            _mm_version = ""
        self.setWindowTitle(
            self.tr("Mosaic Mod Manager - {0}").format(_mm_version) if _mm_version
            else self.tr("Mosaic Mod Manager")
        )
        self.setMinimumSize(1280, 800)   # Steam Deck is the floor
        # Debounced as-you-go persistence of the window geometry + body
        # splitter, so the state survives exits that never reach closeEvent
        # (Ctrl+C in the launching terminal, SIGKILL, crashes).
        self._win_state_timer = QTimer(self)
        self._win_state_timer.setSingleShot(True)
        self._win_state_timer.setInterval(1000)
        self._win_state_timer.timeout.connect(self._save_window_state)
        self.resize(1280, 800)
        # Restore the last-session window position/size/maximized state.
        # restoreGeometry() validates the saved rect against the current screen
        # layout (a since-unplugged monitor falls back on-screen), so a stale
        # save can't strand the window where the user can't see it.
        restored = False
        try:
            from Utils.ui_config import load_qt_window_state
            geo = load_qt_window_state().get("geometry")
            if geo:
                from PySide6.QtCore import QByteArray
                restored = bool(self.restoreGeometry(
                    QByteArray.fromBase64(geo.encode("ascii"))))
        except Exception:
            restored = False
        # Centre on the primary screen. The WM otherwise defaults the window to
        # (0,0) in GLOBAL coords, which lands OFF-SCREEN on a multi-head / offset
        # layout (e.g. a primary screen whose origin isn't at x=0) — the window
        # then "doesn't open" because it's drawn where you can't see it.
        if not restored:
            try:
                scr = (app or QApplication.instance()).primaryScreen()
                if scr is not None:
                    ag = scr.availableGeometry()
                    self.move(ag.center().x() - 640, ag.center().y() - 400)
            except Exception:
                pass

        # Body+footer go in a vertical splitter with the log text area so the
        # log is drag-resizable; the log control bar stays fixed below.
        main_content = QWidget()
        mc = QVBoxLayout(main_content)
        mc.setContentsMargins(0, 0, 0, 0)
        mc.setSpacing(0)
        mc.addWidget(self._build_body_row(), 1)
        # (The tool footers now live inside each panel — see _build_body_row /
        # _build_modlist_area — not in a separate window-wide row.)

        # The main content is the permanent first tab; overlay-style views (Add
        # Game, Nexus browser, …) open as further tabs that can be detached.
        self._tabs = DetachableTabWidget()
        self._tabs.add_permanent(main_content, self.tr("Mods"))
        # Let the tab bar's right-click "Pin to…" menu move any tab between the
        # modlist panel, the plugins panel, or full screen. The stacks were built
        # in _build_body_row above.
        self._tabs.register_scope_targets(
            self._modlist_panel_stack, self._plugins_panel_stack)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setObjectName("LogView")
        self._log_view.setMinimumHeight(0)   # can collapse fully
        self._log_view.setMaximumBlockCount(20000)   # bound the document too

        self._vsplit = QSplitter(Qt.Vertical)
        self._vsplit.addWidget(self._tabs)
        self._vsplit.addWidget(self._log_view)
        self._vsplit.setStretchFactor(0, 1)
        self._vsplit.setStretchFactor(1, 0)
        self._vsplit.setCollapsible(0, True)     # drag past min → log fills window
        self._vsplit.setCollapsible(1, True)     # log collapses to 0; handle stays
        self._vsplit.setHandleWidth(4)
        self._vsplit.splitterMoved.connect(lambda *_: self._sync_log_controls())

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # The header (game/profile selectors + action buttons) sits above the
        # tab widget itself, not inside the "Mods" tab's own page — it used to
        # be nested in main_content, which meant it vanished on every other
        # tab (NIF Viewer, Character, …) since only the active tab's page is
        # shown. As a true sibling of self._tabs it stays visible everywhere.
        outer.addWidget(self._build_header_row())
        outer.addWidget(self._vsplit, 1)
        outer.addWidget(self._build_log_bar())   # fixed control bar
        self.setCentralWidget(central)

        wired = glue.register_all(
            app, log=self._append_log, parent_window=self,
            # Backend warnings (ui_hooks.warn) become an OK-only popup; the
            # signal hop makes the call safe from any worker thread.
            warn=lambda title, message, height=None, **kw: self._warn_popup.emit(
                str(title), str(message), height),
        )
        print("[gui_qt] glue wired:", ", ".join(wired))

        # Start with the log collapsed (deferred until the layout has real size).
        QTimer.singleShot(0, lambda: (self._vsplit.setSizes(
            [self._vsplit.height(), 0]), self._sync_log_controls()))

        # Populate selectors from discovered games and load the active modlist.
        self._populate_selectors()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()

        # Global keyboard shortcuts (F2/F5/Ctrl+D/… — Tk parity) + last-panel
        # tracking so shortcuts route to the modlist or plugins panel.
        from gui_qt.shortcuts import register_shortcuts
        register_shortcuts(self)

        # Connect to Nexus (if logged in) so the footer can show the username +
        # rate limits; validate runs on a worker so startup isn't blocked.
        self._nexus_api = None
        self._nexus_validated.connect(self._on_nexus_validated)
        # Live OAuth login client while a browser login is in flight (kept so the
        # "Paste login code" fallback can feed its session). None when idle.
        self._oauth_client = None
        self._oauth_event.connect(self._on_oauth_event)
        # Check-for-updates: re-entrancy guard + worker→UI signal.
        self._updates_running = False
        self._updates_ready.connect(self._on_updates_ready)
        # Quick Update: resolve (worker) → download (worker) → install (batch).
        self._quick_updating = False
        self._qu_resolved.connect(self._on_qu_resolved)
        self._qu_downloaded.connect(self._on_qu_downloaded)
        self._qu_dl_progress.connect(self._on_qu_dl_progress)
        # mod.io Quick Update — same shape, no premium gate.
        self._quick_updating_modio = False
        self._qmu_resolved.connect(self._on_qmu_resolved)
        self._qmu_downloaded.connect(self._on_qmu_downloaded)
        self._qmu_dl_progress.connect(self._on_qmu_dl_progress)
        self._reinstall_downloaded.connect(self._on_reinstall_downloaded)
        self._reinstall_dl_progress.connect(self._on_reinstall_dl_progress)
        self._reinstall_manual_ready.connect(self._on_reinstall_manual_ready)
        self._reinstall_manual_found.connect(self._on_reinstall_manual_found)
        self._endorse_done.connect(self._on_endorse_done)
        self._mosaic_endorse_done.connect(self._on_mosaic_endorse_done)
        self._nexus_name_fetch_done.connect(self._on_nexus_name_fetch_done)
        self._modio_like_done.connect(self._on_modio_like_done)
        self._track_done.connect(self._on_track_done)
        self._copy_done.connect(self._on_copy_done)
        self._col_update_scan_done.connect(self._finish_collection_update)
        # App-level non-premium installs (reinstall / missing requirements):
        # mod_id → (ManualDownloadWatcher, dl_key) while waiting for a browser
        # download to land. (The Nexus browser / Change Version tabs keep
        # their own registries.)
        self._app_manual_watchers = {}
        # Install-a-Nexus-mod-by-id (Missing Requirements) flow.
        self._req_installing = False
        self._req_install_files.connect(self._on_req_install_files)
        # "Install selected" in Missing Requirements: non-premium files queue
        # up and open one browser page at a time (_req_batch_key = dl_key of
        # the file being watched now).
        self._req_batch_busy = False
        self._req_batch_queue = []
        self._req_batch_key = ""
        self._req_batch_ready.connect(self._on_req_batch_ready)
        self._req_install_dl.connect(self._on_req_install_dl)
        self._req_install_prog.connect(
            lambda key, name, d, t: self._nexus_download_progress(key, name, d, t))
        self._reset_done.connect(self._on_reset_done)
        self._reset_running = False
        # Collection install state + Signal→UI-thread wiring.
        self._col_install_running = False
        self._col_install_overlay = None
        self._col_install_control = None
        self._col_install_slug = ""
        self._col_bundle_zip = ""      # local .mosaic pending bundle extraction
        self._col_offsite = []         # (name, url) manual mods — reminder on done
        self._col_created_profile = False  # this run created the target profile (GH#278)
        self._col_profile_name = ""
        self._col_status.connect(self._on_col_status)
        self._col_progress.connect(self._on_col_progress)
        self._col_agg.connect(self._on_col_agg)
        self._col_display_total.connect(self._on_col_display_total)
        self._col_cache_progress.connect(self._on_col_cache_progress)
        self._col_dl.connect(self._on_col_dl)
        self._col_extract.connect(self._on_col_extract)
        self._col_row.connect(self._on_col_row)
        self._col_manual.connect(self._on_col_manual)
        self._col_finished.connect(self._on_col_finished)
        self._preflight_ev.connect(self._on_preflight_ev)
        self._appended_col_removed.connect(self._on_appended_col_removed)
        self._col_import_done.connect(self._on_import_bundle_done)
        self._import_file_picked.connect(self._on_import_file_picked)
        self._export_code_ready.connect(self._on_export_code_ready)
        self._install_files_picked.connect(self._on_install_files_picked)
        self._custom_exe_picked.connect(self._on_custom_exe_picked)
        self._shared_exe_picked.connect(self._on_shared_exe_picked)
        self._col_fomod.connect(self._on_col_fomod_ui)
        self._col_bain.connect(self._on_col_bain_ui)
        QTimer.singleShot(0, self._ensure_nexus_api)
        # NXM protocol handling: the IPC callback fires on a worker thread, so
        # it emits _nxm_received to hop onto the UI thread. Process a --nxm URL
        # passed on our own command line once the window has finished building.
        self._nxm_install_queue: list = []
        self._nxm_received.connect(self._receive_nxm)
        self._nxm_download_done.connect(self._on_nxm_download_done)
        self._handle_nxm_argv()
        # Silently sync custom handlers + Qt wizard plugins from the Resources
        # branch on GitHub (background threads). A fresh/updated build re-fetches
        # immediately because the gh_cache is wiped when the app version changes.
        self._handlers_synced.connect(self._on_handlers_synced)
        self._handler_force_updated.connect(self._on_handler_force_updated)
        self._languages_synced.connect(self._on_languages_synced)
        try:
            from Utils.gh_cache import clear_if_version_changed
            clear_if_version_changed(_mm_version)
        except Exception:
            pass
        QTimer.singleShot(3000, self._start_gh_sync)
        # Sweep leftover "<Data>.mm_trash-*" dirs from restores whose deferred
        # background delete was interrupted (crash / app close mid-delete).
        QTimer.singleShot(0, self._sweep_deploy_trash_startup)
        # App self-update check (Tk parity: after(2000, …)). AppImage/flatpak
        # compare against GitHub releases, everything else against the AUR.
        self._update_overlay = None
        self._app_update_found.connect(self._on_app_update_found)
        self._app_update_toast = None
        self._app_update_checked.connect(self._on_app_update_checked)
        QTimer.singleShot(2000, self._check_for_app_update)
        # First-run onboarding: show it (as a fullscreen tab) when the flag is
        # unset/0 OR no games are configured (Tk parity — re-appears after the
        # last game is removed). Deferred so the window finishes building first.
        self._onboarding_view = None
        from Utils.exe_launch.game_helpers import _GAMES
        from Utils.ui_config import load_onboarding_complete
        configured = sum(1 for g in _GAMES.values() if g.is_configured())
        if not load_onboarding_complete() or configured == 0:
            QTimer.singleShot(0, self._open_onboarding_tab)

        # Warn if any game handler failed to load. A broken handler used to make
        # its game silently vanish from the list, which users mistook for the
        # game (and its mods) being deleted — mods actually live untouched in
        # ~/.config and Profiles/. Deferred so the log panel is live first.
        QTimer.singleShot(0, self._warn_handler_load_failures)

        # Flatpak self-heal: bundle installs (release zip / Warehouse) don't
        # pull the Compat.i386 extension the manifest declares, leaving
        # /lib/ld-linux.so.2 dangling — every Proton/wine run in the sandbox
        # then fails with "could not open". Install the refs in the background.
        self._i386_repair_done.connect(self._on_i386_repair_done)
        self._i386_toast = None
        QTimer.singleShot(1000, self._check_flatpak_i386)

        # Splash watchdog: the splash is normally dismissed by the first
        # _on_conflicts_ready, but a game with no profile / empty modlist may
        # never trigger a conflict rebuild. Close it unconditionally after a
        # short grace period so it can never hang on screen.
        if self._splash is not None:
            QTimer.singleShot(4000, self._dismiss_splash)

    # Startup is often fast enough that the splash flashes by unreadably;
    # pad it out to this minimum, but only for the difference — a slow
    # first load that already exceeds this is never held up further.
    _MIN_SPLASH_MS = 900

    def _dismiss_splash(self):
        """Reveal the finished window and close the startup splash, exactly once.

        The window is shown minimised while loading (see run()); un-minimising
        it here makes the now fully-rendered UI appear all at once, then the
        splash closes on top of it."""
        if self._splash_dismissed:
            return
        if self._splash is not None and self._splash_shown_at is not None:
            elapsed_ms = (_time.monotonic() - self._splash_shown_at) * 1000
            remaining = self._MIN_SPLASH_MS - elapsed_ms
            if remaining > 0:
                QTimer.singleShot(int(remaining), self._finish_dismiss_splash)
                return
        self._finish_dismiss_splash()

    def _finish_dismiss_splash(self):
        if self._splash_dismissed:
            return
        self._splash_dismissed = True
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        s, self._splash = self._splash, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        self.raise_()
        self.activateWindow()

    def _warn_handler_load_failures(self):
        """If any game handler failed to load during discovery, log each one and
        show a single toast. Without this a broken handler just removes its game
        from the list with no trace, which reads as "my game/mods got deleted"
        (mods are safe — they live in ~/.config and Profiles/, untouched)."""
        try:
            from Utils.exe_launch.game_loader import get_load_failures
            failures = get_load_failures()
        except Exception:
            return
        if not failures:
            return
        for rel, reason in failures:
            self._append_log(f"[game] handler failed to load: {rel} — {reason}")
        n = len(failures)
        first = failures[0][0]
        detail = first if n == 1 else self.tr("{0} and {1} more").format(first, n - 1)
        self._notify(
            self.tr("A game handler failed to load ({0}). Affected games are "
                    "hidden, but your mods are safe — see the log.").format(detail),
            state="warning",
            sticky=True,
        )

    def _check_flatpak_i386(self):
        """Flatpak startup self-heal: install the 32-bit compat extensions when
        the sandbox lacks them (bundle installs skip the manifest's related
        refs). Without them every in-sandbox Proton/wine run — dtkit-patch,
        vcredist, wizard exes — dies with "/lib/ld-linux.so.2: could not open".
        """
        from Utils.wine_proton.flatpak_i386 import i386_support_missing
        if not i386_support_missing():
            return
        from Utils.ui_config import load_suppress_i386_warning
        if load_suppress_i386_warning():
            self._append_log("[flatpak] 32-bit support missing but the warning "
                             "is suppressed — skipping self-heal.")
            return
        self._append_log("[flatpak] 32-bit support missing (Compat.i386 "
                         "extension not installed) — installing from Flathub …")
        self._i386_toast = self._notify(
            self.tr("Installing 32-bit support (needed to run Windows tools) …"),
            sticky=True,
        )

        def _run():
            from gui_qt.safe_emit import safe_emit
            from Utils.app_log import app_log
            from Utils.wine_proton.flatpak_i386 import install_i386_extensions
            ok = install_i386_extensions(log_fn=lambda m: app_log(f"[flatpak] {m}"))
            safe_emit(self._i386_repair_done, ok)

        threading.Thread(target=_run, daemon=True).start()

    def _on_i386_repair_done(self, ok: bool):
        """UI thread: 32-bit extension install finished — swap the toast."""
        toast, self._i386_toast = self._i386_toast, None
        if toast is not None:
            try:
                toast.dismiss()
            except Exception:
                pass
        if ok:
            # Extensions mount when the sandbox is (re)created, not live.
            self._notify(
                self.tr("32-bit support installed — restart the app before "
                        "running Windows tools."),
                state="success", sticky=True,
            )
        else:
            from Utils.wine_proton.flatpak_i386 import MANUAL_INSTALL_CMD
            self._append_log(f"[flatpak] install manually: {MANUAL_INSTALL_CMD}")
            from gui_qt.overlays.confirm_overlay import ConfirmOverlay

            def _done(dont_show_again: bool):
                if dont_show_again:
                    from Utils.ui_config import save_suppress_i386_warning
                    save_suppress_i386_warning(True)
                    self._append_log("[flatpak] 32-bit support warning suppressed "
                                     "at the user's request.")

            ConfirmOverlay.show_over(
                self,
                self.tr("32-bit support could not be installed"),
                self.tr(
                    "Mosaic could not install 32-bit support automatically. "
                    "Windows tools (and some games) may fail to run until it is "
                    "installed. Run this on a terminal, then restart the app:\n\n"
                    "{0}"
                ).format(MANUAL_INSTALL_CMD),
                _done,
                confirm_label=self.tr("Don't show again"),
                cancel_label=self.tr("OK"),
                danger=False,
            )

    def _populate_selectors(self):
        """Fill the game/profile selectors from the current GameState."""
        gs = self._gs
        if gs.game_name:
            self._game_selector.set_items(gs.game_names, current=gs.game_name)
        else:
            # No games configured — the button invites the user to add one
            # instead of showing stale placeholder names.
            self._game_selector.set_items([], current=self.tr("Add game"))
        self._refresh_play_selector()
        # The 'Edit custom game…' entry depends on the active game.
        self._refresh_game_actions()
        profs = gs.profiles()
        if profs:
            self._profile_selector.set_items(profs, current=gs.profile)
        self._refresh_profile_actions()
        self._update_profile_collection_tooltip()

    # ---------------------------------------------------------- header row
    def _build_header_row(self) -> QWidget:
        """Full-width top bar (selectors + action buttons) — spans the whole
        window so the buttons have room. The Play section now lives above the
        plugins panel (see _build_body_row), not here."""
        return self._left_header()

    # ---------------------------------------------------------- body row
    def _build_body_row(self) -> QWidget:
        # Each side is a self-contained column: the modlist column owns its tool
        # footer (buttons + search), and the plugins column owns the Play bar on
        # top plus the plugins tool footer at the bottom. The footers live INSIDE
        # the panels (not a separate window-wide row) so they move/resize with
        # their panel. The splitter divides the two columns.
        right_col = QWidget()
        rc = QVBoxLayout(right_col)
        rc.setContentsMargins(0, 0, 0, 0)
        rc.setSpacing(0)
        play = self._play_bar()
        self._play_bar_widget = play
        rc.addWidget(play)
        # Build the plugins panel FIRST — it creates the sub-tab views (incl.
        # _text_files_view) that the footers below reference.
        plugins_body = self._build_plugins()
        # The plugins column footer is a stack: it swaps to the active sub-tab's
        # tools (Plugins tools ↔ Mod Files Pack/Unpack + search).
        # _CurrentPageStack (not a plain QStackedWidget): sizes to the visible
        # footer only, so the Downloads footer wrapping its buttons to a second
        # row at min width doesn't add a blank row to the Plugins/etc. footers.
        self._plugin_footer_stack = _CurrentPageStack()
        for _page in (self._plugins_footer(), self._mod_files_footer(),
                      self._data_footer(), self._downloads_footer(),
                      self._text_files_footer()):
            self._enable_height_for_width(_page)
            self._plugin_footer_stack.addWidget(_page)
        # The stack must also report height via heightForWidth so its parent
        # (plugins_panel's QVBoxLayout) fits it to the visible page's wrapped
        # height at the actual width, not the inflated min-width sizeHint.
        self._enable_height_for_width(self._plugin_footer_stack)
        # Re-fit the stack (and thus the panel) whenever the visible page swaps.
        self._plugin_footer_stack.currentChanged.connect(
            lambda _i: self._plugin_footer_stack.clamp_to_current())
        # The whole plugins panel (sub-tab strip + content + footer, but NOT the
        # Play bar) lives in a stack so a panel-scoped tab (Change Version) can
        # take it over entirely. Page 0 = the plugins panel + its footer.
        plugins_panel = QWidget()
        pp = QVBoxLayout(plugins_panel)
        pp.setContentsMargins(0, 0, 0, 0)
        pp.setSpacing(0)
        pp.addWidget(plugins_body, 1)
        pp.addWidget(self._plugin_footer_stack)
        # Inline userlist edit bars (hidden until opened from the plugins
        # context menu — Tk parity: rows 5/6 at the bottom of the plugins tab).
        from gui_qt.userlist_bars import GroupBar, UserlistBar
        self._ul_bar = UserlistBar(self._userlist_path,
                                   self._on_userlist_bar_saved)
        self._grp_bar = GroupBar(self._userlist_path,
                                 self._on_userlist_bar_saved)
        pp.addWidget(self._ul_bar)
        pp.addWidget(self._grp_bar)
        self._plugins_panel_stack = QStackedWidget()
        self._plugins_panel_stack.addWidget(plugins_panel)               # page 0
        rc.addWidget(self._plugins_panel_stack, 1)
        self._right_col = right_col

        # The modlist column lives in a stack so a panel-scoped tab (e.g. an
        # image preview) can take over JUST the modlist region while the plugins
        # panel (with the Mod Files tree) stays live. Page 0 = the real modlist.
        self._modlist_panel_stack = QStackedWidget()
        self._modlist_panel_stack.addWidget(self._build_modlist_area())

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._modlist_panel_stack)
        split.addWidget(right_col)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        # Wider handle so a panel collapsed to one edge still has an easy-to-grab
        # grip (the default 1px line is nearly invisible / hard to click).
        split.setHandleWidth(8)
        split.setChildrenCollapsible(True)
        # Last-session splitter position, else the default split. Saved sizes
        # are absolute pixels at the saved window size; QSplitter rescales them
        # proportionally when the restored window size differs.
        saved_sizes = None
        try:
            from Utils.ui_config import load_qt_window_state
            saved_sizes = load_qt_window_state().get("body_split")
        except Exception:
            saved_sizes = None
        split.setSizes(saved_sizes or [620, 480])
        split.splitterMoved.connect(
            lambda *_: self._schedule_window_state_save())
        self._body_split = split
        self._wire_cross_panel()
        return split

    def _wire_cross_panel(self):
        """Connect modlist ↔ plugins selection so picking a mod highlights its
        plugins (+ conflict-tinted ones) and picking a plugin highlights its
        owning mod (Tk parity)."""
        self._conflict_data = None
        self._conflict_maps_current = False
        mv, pv = self._modlist_view, self._plugin_view
        mv.selectionModel().selectionChanged.connect(
            lambda *_: self._on_mod_selection_changed())
        pv.selectionModel().selectionChanged.connect(
            lambda *_: self._on_plugin_selection_changed())

    def _suppress_xpanel(self) -> bool:
        return getattr(self, "_xpanel_busy", False)

    def _on_mod_selection_changed(self):
        if self._suppress_xpanel():
            return
        self._xpanel_busy = True
        try:
            mv, pv = self._modlist_view, self._plugin_view
            names = mv.selected_mod_names()
            if self._req_tab_active():
                # View Requirements mode: conflict highlights are disabled —
                # the panel retargets to the selection (multiple mods pool
                # their requirements together; a selected separator contributes
                # its whole block, matching selected_mod_names) and its
                # on_data_changed callback applies the purple/blue tints once
                # the deferred rebuild ends.
                self._view_requirements_view.show_mods(names)
                pv.clearSelection()
                self._update_mod_files_selection(names)
                return
            # Modlist rows: green/red tint on ALL conflict partners (loose+BSA).
            higher, lower = mv.conflict_partners(names)
            # Tk quirk: the [Overwrite] band lights GREEN when it wins over the
            # selection (so the user sees Overwrite is active), even though a
            # normal winning mod would be red. Flip it from lower→higher.
            from Utils.filemap import OVERWRITE_NAME
            if OVERWRITE_NAME in lower:
                lower = lower - {OVERWRITE_NAME}
                higher = higher | {OVERWRITE_NAME}
            mv.model().set_highlights(higher=higher, lower=lower)
            # Plugins panel: orange the selected mods' plugins. Green/red is
            # applied ONLY for BSA conflicts (Tk parity) — loose-file conflicts
            # do NOT colour plugins — and only to plugins that own a BSA.
            cd = self._conflict_data
            owner = cd.plugin_owner if cd else {}
            bsa_higher, bsa_lower = mv.bsa_conflict_partners(names)
            pv.set_highlight_from_mods(
                names, bsa_higher, bsa_lower, owner,
                bsa_index_path=self._gs.bsa_index_path())
            # Picking a mod clears any plugin selection (mutual exclusivity).
            pv.clearSelection()
            # Feed the Mod Files tab the single selected mod (real mods only).
            self._update_mod_files_selection(names)
        finally:
            self._xpanel_busy = False

    def _update_mod_files_selection(self, _names):
        """Show the single selected mod in the Mod Files tab. A separator or a
        multi-selection shows nothing (the separator overview is a later step)."""
        mv = getattr(self, "_mod_files_view", None)
        if mv is None:
            return
        rows = self._modlist_view.selectionModel().selectedRows()
        if len(rows) != 1:
            mv.show_mod(None)
            return
        e = self._modlist_model.entry(rows[0].row())
        from gui_qt.modlist.modlist_model import _BOUNDARY_NAMES
        # The synthetic Overwrite / Root Folder rows have a real on-disk folder,
        # so they show their files in the Mod Files tab like a mod. Only true
        # (user) separators show nothing.
        if e.is_separator and e.name not in _BOUNDARY_NAMES:
            mv.show_mod(None)
        else:
            mv.show_mod(e.name)

    def _asset_providers(self, rel_key, mod_name):
        """Every mod that ships the post-strip file *rel_key*, for the image
        preview's picker/compare: enabled mods in priority order plus the mod
        being viewed (it may be disabled) and whichever mod the filemap says
        wins. Uses the cached mod index / conflict data the Mod Files tab has
        already loaded, so it is cheap. Returns [] for anything that can't be
        resolved — the preview then behaves as a plain single-image viewer."""
        if not rel_key:
            return []
        try:
            g = self._gs.game
            staging = self._gs.staging_dir()
            ml = self._gs.modlist_path()
            pd = self._gs.profile_dir()
            if g is None or staging is None or ml is None or not ml.is_file():
                return []
            from Utils.filemap import read_mod_index
            from Utils.mods import mod_files as mflogic
            from Utils.mods.file_providers import find_providers
            from Utils.mods.modlist import read_modlist
            index_path = staging.parent / "modindex.bin"
            full_index = read_mod_index(index_path)
            if not full_index:
                return []
            _contested, winner = mflogic.build_conflict_cache(
                index_path, pd, full_index)
            order = [e.name for e in read_modlist(ml)
                     if not e.is_separator and e.enabled]
            for extra in (mod_name, winner.get(rel_key.lower())):
                if extra and extra not in order:
                    order.append(extra)
            return find_providers(
                full_index, rel_key, winner, order,
                lambda m: mflogic._mod_dir_for(g, m),
                lambda m: mflogic.read_strip_prefixes(pd, m))
        except Exception:
            return []

    def _open_text_editor_tab(self, path, rel_str, find_kw=None):
        """Open a text file in a save-capable editor as a MODLIST-PANEL-SCOPED tab
        (the other panels stay live). Each distinct file gets its OWN tab so
        several can be open at once; re-opening the same file focuses its tab.
        The tab title gets a '*' while there are unsaved edits.
        *find_kw* (the active content-search keyword) is pre-highlighted."""
        from gui_qt.text_editor import TextEditor
        p = _P(path)
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        key = "tf_text_editor:" + str(p.resolve() if p.exists() else p)
        editors = getattr(self, "_text_editor_widgets", None)
        if editors is None:
            editors = self._text_editor_widgets = {}
        # Same file already open → just focus it (and re-apply the search).
        if key in editors and self._tabs.has_key(key):
            widget = editors[key]
            if find_kw:
                widget.find_text(find_kw)
            self._tabs.focus_key(key)
            return
        widget = TextEditor(p, name)
        widget.setProperty("_te_key", key)
        editors[key] = widget
        widget.dirty_changed.connect(
            lambda dirty, w=widget: self._on_text_editor_dirty(w, dirty))
        widget.saved.connect(self._on_text_editor_saved)
        widget.destroyed.connect(lambda *_a, k=key: self._text_editor_widgets.pop(k, None))
        self._tabs.open_scoped_tab(
            widget, name, self._modlist_panel_stack, key=key)
        if find_kw:
            widget.find_text(find_kw)

    def _on_text_editor_dirty(self, widget, dirty):
        key = widget.property("_te_key")
        if key and self._tabs.has_key(key):
            self._tabs.set_tab_title(
                key, (widget.name + " *") if dirty else widget.name)

    def _on_text_editor_saved(self):
        # File content changed on disk → the Text Files content search may shift.
        if hasattr(self, "_text_files_view"):
            self._text_files_view.mark_dirty()
        self._notify(self.tr("Saved"), "success")

    def _on_mod_files_changed(self):
        """A Top Level / Disable edit changed deploy state — force a full index
        rescan (strip prefixes apply at scan time) + rebuild conflicts."""
        # Instant feedback: update the modlist "modified in Mod Files" eye flag
        # now (the async rescan below refreshes the rest a moment later).
        if hasattr(self, "_modlist_model"):
            self._modlist_model.set_modified_mf(self._build_modified_mf_mods())
        self._rebuild_conflicts_async(rescan_index=True)

    def _on_data_select_mod(self, mod):
        """A Data-tab file row was selected → orange-highlight its winning mod in
        the modlist (Tk parity); a folder row clears it."""
        if self._suppress_xpanel():
            return
        self._xpanel_busy = True
        try:
            mv = self._modlist_view
            mv.set_highlighted_mods({mod} if mod else set())
            mv.clearSelection()
        finally:
            self._xpanel_busy = False

    # The action buttons always show text+icon: the full-width top bar has room
    # for them even at the 1280 minimum, so the old icon-only collapse (with its
    # threshold/flicker tuning) is no longer needed.

    def _on_handler_force_updated(self, name: str, status: str):
        """Force-update worker finished (UI thread). On a real update, reload
        the game registry and re-select the game through the normal path so the
        new definition takes effect everywhere (views, actions, play bar)."""
        if status == "failed":
            self._notify(
                self.tr("Handler update failed — check your connection."),
                "error")
            return
        if status == "missing":
            self._notify(
                self.tr("Handler not found on the Resources branch."), "error")
            return
        if status == "unchanged":
            self._notify(self.tr("Handler is already up to date."), "info")
            return
        from Utils.exe_launch.game_helpers import _load_games
        names = _load_games()
        self._gs.game_names = names
        real_names = [n for n in names if n != "No games configured"]
        # A repo rename may have changed the display name; fall back sanely.
        target = (name if name in real_names
                  else (real_names[0] if real_names else None))
        if target is not None:
            self._game_selector.set_items(real_names, current=target)
            self._gs.set_game(target)
            # set_game no-ops when the name didn't change (the common case) —
            # but _load_games() just rebuilt the game object, so its active
            # profile dir + per-profile path overrides must be re-applied.
            self._gs._apply_active_profile()
            self._game_selector.set_current(target)
            self._refresh_game_actions()
            self._refresh_profile_actions()
            self._refresh_play_selector()
            self._reload_modlist()
            self._reload_plugins()
        self._append_log(f"[game] handler updated from Resources: {name}")
        self._notify(self.tr("Handler updated."), "success")

    def _sweep_deploy_trash_startup(self):
        """Remove leftover restore-trash dirs for every configured game.

        A restore renames the deploy dir to "<name>.mm_trash-<ns>" and deletes
        it in a background thread; a crash mid-delete leaves the trash behind.
        move_to_core/restore also sweep their own game, so this startup pass
        just covers games that aren't deployed again this session.  Runs on a
        daemon thread — it's pure cleanup and may touch slow drives.
        """
        from Utils.exe_launch.game_helpers import _GAMES

        games = [g for g in _GAMES.values() if g.is_configured()]

        def _run():
            from Utils.deploy.deploy_standard import sweep_deploy_trash
            for game in games:
                try:
                    data = game.get_mod_data_path()
                    root = game.get_game_path()
                    if data is None or root is None:
                        continue
                    data = Path(data)
                    # Only standard-mode layouts (Data/ inside the game root)
                    # ever create trash siblings; skip game-root-mode games so
                    # we never scan outside the game install.
                    if data == Path(root):
                        continue
                    sweep_deploy_trash(data.parent)
                except Exception:
                    continue

        threading.Thread(target=_run, name="mm-trash-sweep", daemon=True).start()

    def _request_restart(self):
        """Flag a self-restart and close the window. closeEvent runs the normal
        shutdown (IPC release, optional restore-on-close); run() then re-execs
        the process so everything rebuilds fresh (e.g. in the new language)."""
        global _RESTART_REQUESTED
        _RESTART_REQUESTED = True
        # Close on the next tick so the caller (a combo signal handler) returns
        # first and the UI isn't torn down mid-callback.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.close)

    def _download_custom_game_images(self, view=None):
        """Background-download missing custom-game banner images. If *view* is an
        AddGameView, refresh each card's logo as its image lands."""
        try:
            from Games.Custom.custom_game import download_missing_custom_game_images
        except Exception:
            return
        cb = None
        if view is not None and hasattr(view, "on_image_downloaded"):
            cb = view.on_image_downloaded
        try:
            download_missing_custom_game_images(on_done=cb)
        except Exception:
            pass

    # ---- NXM protocol handling ("Download with Manager") -----------------

    # Enderal can install Skyrim mods; Enderal SE can install Skyrim SE mods.
    # If the user is already on Enderal(SE) and the link is for the Skyrim(SE)
    # counterpart, stay on Enderal instead of switching away.
    _ENDERAL_ACCEPTS = {
        "enderal": "skyrim",
        "enderalspecialedition": "skyrimspecialedition",
    }

    def _match_game_for_domain(self, game_domain: str):
        """Return (name, game) for the configured game matching *game_domain*,
        honouring the Enderal→Skyrim exception if the current game already
        accepts this domain. None if nothing configured matches."""
        from Utils.exe_launch.game_helpers import _GAMES
        current = self._gs.game
        current_domain = (getattr(current, "nexus_game_domain", "") or "") if current else ""
        if (current is not None and current.is_configured()
                and self._ENDERAL_ACCEPTS.get(current_domain) == game_domain):
            return (self._gs.game_name, current)
        for name, game in _GAMES.items():
            if getattr(game, "nexus_game_domain", "") == game_domain and game.is_configured():
                return (name, game)
        return None

    def _open_wabbajack_website(self):
        from Utils.wabbajack.wabbajack_gallery import GALLERY_WEBSITE
        from Utils.xdg import open_url
        open_url(GALLERY_WEBSITE, log_fn=self._append_log)

    def _open_game_on_nexus(self):
        """Open the current game's Nexus Mods page in the browser."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        from Utils.xdg import open_url
        open_url(f"https://www.nexusmods.com/{domain}",
                 log_fn=self._append_log)

    def _open_nexus_browser_tab(self):
        """Open the Nexus Mods browser as a detachable tab. Needs a configured
        game with a Nexus domain and existing OAuth tokens (login UI deferred)."""
        if self._tabs.has_key("nexus_browser"):
            self._tabs.focus_key("nexus_browser")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        # Reuse the shared API (built at startup); falls back to building it now
        # if startup couldn't (e.g. the user logged in afterwards).
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            self._append_log("[nexus] no OAuth tokens — login required")
            return
        from gui_qt.nexus.nexus_browser_view import NexusBrowserView
        view = NexusBrowserView(api, domain, game,
                                install_fn=self._install_paths,
                                log_fn=self._append_log,
                                progress_fn=self._nexus_download_progress)
        self._nexus_view = view
        # Drop the reference when the tab/window is gone so we stop refreshing it.
        view.destroyed.connect(lambda *_: setattr(self, "_nexus_view", None))
        self._tabs.open_tab(view, self.tr("Nexus"), key="nexus_browser")

    def _show_failed_mods_report(self, then=None):
        """Post-install: name every mod that did NOT end up in the profile and
        why, plus any audit warnings (the audit used to be a log line only),
        then run *then()*. The full report is also saved as install_report.json
        in the profile."""
        failed = list(getattr(self, "_col_failed", None) or [])
        warns = list(getattr(self, "_col_warnings", None) or [])
        self._col_failed = []
        self._col_warnings = []
        if not failed and not warns:
            if then is not None:
                then()
            return
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        parts = []
        if failed:
            shown = "\n".join(f"• {f['name']} — {f['reason']}" for f in failed[:10])
            if len(failed) > 10:
                shown += "\n" + self.tr("…and {0} more").format(len(failed) - 10)
            retry = [f for f in failed if f.get("retryable")]
            stuck = [f for f in failed if not f.get("retryable")]
            if retry and not stuck:
                advice = self.tr("Nothing was rolled back. Press Install on the collection "
                                 "again and choose Continue to retry them.")
            elif stuck and not retry:
                advice = self.tr("Nothing was rolled back. Pressing Install again will NOT "
                                 "fix these — they fail the same way every time, so this "
                                 "needs a fix in Mosaic (or installing the mod by hand).")
            else:
                advice = self.tr("Nothing was rolled back. Install again → Continue can "
                                 "retry the download failures; the others fail the same "
                                 "way every time and need a fix in Mosaic (or a manual "
                                 "install).")
            parts.append(self.tr("These mods are not in the profile:\n\n{0}\n\n{1}")
                         .format(shown, advice))
        if warns:
            parts.append("\n\n".join("⚠ " + w for w in warns))
        parts.append(self.tr("The full report is in install_report.json inside the "
                             "profile folder."))
        title = (self.tr("{0} mod(s) did not install").format(len(failed)) if failed
                 else self.tr("The install finished with a warning"))
        ConfirmOverlay.show_over(
            self, title, "\n\n".join(parts),
            lambda _r, t=then: t() if t is not None else None,
            confirm_label=self.tr("OK"), cancel_label=None, danger=False,
            card_h=min(300 + 20 * min(len(failed), 10) + 70 * len(warns), 600))

    def _show_offsite_reminder(self):
        """Post-install reminder: the collection lists off-site mods that the
        installer can't download — the user must fetch + install them manually
        (links live in the detail view's yellow "Off-site mods" panel)."""
        offsite = self._col_offsite
        self._col_offsite = []
        if not offsite:
            return
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        names = [name or url for name, url in offsite]
        shown = "\n".join(f"• {n}" for n in names[:8])
        if len(names) > 8:
            shown += "\n" + self.tr("…and {0} more").format(len(names) - 8)
        ConfirmOverlay.show_over(
            self, self.tr("Off-site mods to install"),
            self.tr("This collection includes {0} off-site mod(s) the installer "
                    "could not download:\n\n{1}\n\nDownload and install them "
                    "manually — the links are in the collection page's "
                    "\"Off-site mods\" panel.").format(len(offsite), shown),
            None, confirm_label=self.tr("OK"), cancel_label=None, danger=False,
            card_h=min(240 + 20 * min(len(names), 9), 460))

    def _on_reset_done(self, res):
        self._reset_running = False
        if not isinstance(res, dict) or res.get("error"):
            reason = (res or {}).get("error", "unknown") if isinstance(res, dict) else "unknown"
            self._notify(self.tr("Load order reset failed: {0}").format(reason), "warning")
            return
        self._notify(
            f"Load order reset — {res.get('ordered', 0)} mods ordered"
            + (f", {res['unordered']} at top."
               if res.get("unordered") else "."), "info")
        self._reload_modlist()

    # ---- Nexus login (header menu ▸ Login to Nexus) ------------------------
    # Thin wiring over the toolkit-neutral OAuth/NXM backend in src/Nexus/,
    # mirroring the Tk gui/nexus_settings_dialog.py. The OAuth client fires its
    # callbacks from a background thread, so they only emit `_oauth_event`
    # (queued → UI thread) and never touch widgets directly.

    # ---- Check for updates -------------------------------------------------
    # Reuses the toolkit-neutral Nexus.nexus_update_checker.check_for_updates,
    # which writes has_update / missing_requirements back to each mod's meta.ini
    # (save_results=True). The modlist re-reads those on reload and paints the
    # update + missing-requirement badges, so the handler just runs the check on
    # a worker thread and reloads.

    # ---- Quick Update -----------------------------------------------------

    # ---- Quick Update (mod.io) ---------------------------------------------
    # Mirrors the Nexus Quick Update flow above, but simpler: mod.io has no
    # premium gate and no "name match vs Change Version" fork to replicate —
    # every mod carrying FLAG_MODIO_UPDATE already has a confirmed newer file
    # (modio_update_checker wrote modioLatestFileId), so every resolved target
    # just gets its latest file installed directly.

    # ---- Backups (plugins-panel-scoped overlay) ----------------------------

    # ---- Change Version (plugins-panel-scoped overlay) --------------------

    # ---- Change Version (mod.io, plugins-panel-scoped overlay) ------------

    # ---- Bundle options (plugins-panel-scoped overlay) --------------------

    # ---- Separator settings (plugins-panel-scoped overlay) ----------------

    def _on_separator_renamed(self, old_name, new_name):
        """Migrate a renamed separator's stored colour + deploy override from the
        old internal name to the new one, then persist + repaint."""
        profile_dir = self._gs.profile_dir()
        # Model colour/deploy dicts (keyed by internal name) follow the rename.
        c = self._modlist_model.sep_color(old_name)
        if c is not None:
            self._modlist_model.set_sep_color(old_name, None)
            self._modlist_model.set_sep_color(new_name, c)
        d = self._modlist_model.sep_deploy_info(old_name)
        if d:
            self._modlist_model.set_sep_deploy_info(old_name, None)
            self._modlist_model.set_sep_deploy_info(new_name, d)
        if profile_dir is not None:
            try:
                from Utils.profile.profile_state import (
                    read_separator_colors,
                    read_separator_deploy_paths,
                    write_separator_colors,
                    write_separator_deploy_paths,
                )
                colors = read_separator_colors(profile_dir)
                if old_name in colors:
                    colors[new_name] = colors.pop(old_name)
                    write_separator_colors(profile_dir, colors)
                paths = read_separator_deploy_paths(profile_dir)
                if old_name in paths:
                    paths[new_name] = paths.pop(old_name)
                    write_separator_deploy_paths(profile_dir, paths)
            except Exception as exc:
                print(f"[gui_qt] separator rename migration failed: {exc}",
                      flush=True)
        self._modlist_view.viewport().update()

    def _on_separators_removed(self, names):
        """Drop stored colour + deploy override + lock + collapsed state for
        removed separators."""
        from Utils.mods.modlist import _SEPARATOR_SUFFIX
        profile_dir = self._gs.profile_dir()
        display_names = set()
        for nm in names:
            self._modlist_model.set_sep_color(nm, None)
            self._modlist_model.set_sep_deploy_info(nm, None)
            disp = nm[:-len(_SEPARATOR_SUFFIX)] if nm.endswith(_SEPARATOR_SUFFIX) else nm
            display_names.add(disp)
            self._modlist_model._sep_locks.pop(disp, None)
            self._modlist_model._collapsed.discard(disp)
        if profile_dir is not None:
            try:
                from Utils.profile.profile_state import (
                    read_collapsed_seps,
                    read_separator_colors,
                    read_separator_deploy_paths,
                    read_separator_locks,
                    write_collapsed_seps,
                    write_separator_colors,
                    write_separator_deploy_paths,
                    write_separator_locks,
                )
                colors = read_separator_colors(profile_dir)
                paths = read_separator_deploy_paths(profile_dir)
                locks = read_separator_locks(profile_dir)
                collapsed = read_collapsed_seps(profile_dir)
                changed_c = changed_p = changed_l = changed_col = False
                for nm in names:
                    changed_c |= colors.pop(nm, None) is not None
                    changed_p |= paths.pop(nm, None) is not None
                for disp in display_names:
                    if locks.pop(disp, None) is not None:
                        changed_l = True
                    if disp in collapsed:
                        collapsed.discard(disp)
                        changed_col = True
                if changed_c:
                    write_separator_colors(profile_dir, colors)
                if changed_p:
                    write_separator_deploy_paths(profile_dir, paths)
                if changed_l:
                    write_separator_locks(profile_dir, locks)
                if changed_col:
                    write_collapsed_seps(profile_dir, collapsed)
            except Exception as exc:
                print(f"[gui_qt] separator removal cleanup failed: {exc}",
                      flush=True)

    # ---- install a Nexus mod by id (used by Missing Requirements cards) ----
    # Mirrors the Nexus browser's install flow: premium check → fetch files →
    # pick MAIN (or file chooser if several) → download → hand to _install_paths.
    # All worker stages hop back to the UI thread via Signals (NOT QThread).

    def _on_add_game_select(self, name: str):
        """A configured game was picked in the Add-Game view → switch to it and
        close the tab."""
        self._tabs.close_tab("add_game")
        if name in self._gs.game_names:
            self._on_game_changed(name)
            self._game_selector.set_current(name)
        self._append_log(f"[game] selected {name}")

    def _on_add_game_add(self, name: str):
        """An unconfigured game was picked → open the configure-game tab."""
        from Utils.exe_launch.game_helpers import _GAMES
        game = _GAMES.get(name)
        if game is None:
            self._append_log(f"[game] {name} not found in registry")
            return
        self._tabs.close_tab("add_game")
        self._open_configure_game_tab(game, from_add_game=True)

    def _wabbajack_controller(self):
        """The Wabbajack controller (browser, import, install), created on
        first use."""
        if getattr(self, "_wabbajack", None) is None:
            from gui_qt.wabbajack.wabbajack_controller import WabbajackController
            self._wabbajack = WabbajackController(self)
        return self._wabbajack

    def _close_nif_character_tabs(self):
        """The NIF Viewer / Character tabs index the game and profile they were
        opened for (see _open_nif_viewer_tab/_open_character_tab) and never
        re-point themselves at a new one — close both on a game/profile switch
        so they don't keep showing a previous game's (or profile's) meshes."""
        for key in ("nif_viewer", "character_viewer"):
            if self._tabs.has_key(key):
                self._tabs.close_tab(key)
        self._character_view = None

    def _confirm_nif_character_close(self, title: str, proceed):
        """Ask before a user-initiated action that will close the NIF Viewer
        and/or Character tab (a game/profile switch made via the selector
        buttons — see _confirm_game_change/_confirm_profile_change) — calls
        *proceed* immediately if neither is open, otherwise only after the
        user confirms. Declining calls nothing: the caller hasn't touched
        game/profile state or the selector's displayed value yet
        (SelectorButton only updates its own label when set_current() is
        called, not on selection), so nothing needs to be undone.

        Only the selector buttons route through this — every other caller of
        _on_game_changed/_on_profile_changed (Nexus auto-switch-on-download,
        Add Game, collection-cancel cleanup, ...) calls them directly and
        synchronously, with the tabs closed silently like any other
        game/profile-scoped tab; gating those behind an async confirmation
        would break callers that act right after assuming the switch is done."""
        nif_open = self._tabs.has_key("nif_viewer")
        char_open = self._tabs.has_key("character_viewer")
        if not (nif_open or char_open):
            proceed()
            return
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        if nif_open and char_open:
            what = self.tr("NIF Viewer and Character")
        elif nif_open:
            what = self.tr("NIF Viewer")
        else:
            what = self.tr("Character")
        ConfirmOverlay.show_over(
            self, title,
            self.tr("This will close the current {0} view.").format(what),
            lambda ok: proceed() if ok else None,
            confirm_label=self.tr("Switch"),
            cancel_label=self.tr("Cancel"),
            danger=False,
        )

    def _confirm_version_switch(self, title, body, on_done, confirm_label=None):
        """ConfirmOverlay for the play bar's build dropdown."""
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(self, title, body, on_done, danger=False,
                                 confirm_label=confirm_label)

    def _show_version_message(self, title, body):
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_message(self, title, body)

    def _open_game_version_wizard(self):
        """Open the Game version (Steam) wizard the dropdown falls back to."""
        game = self._gs.game
        if game is None:
            return
        from Utils.plugins.plugin_loader import get_all_wizard_tools
        try:
            tool = next((t for t in get_all_wizard_tools(game)
                         if t.id == "game_version_steam"), None)
        except Exception as exc:
            self._append_log(f"Game version: couldn't open the wizard: {exc}")
            return
        if tool is None:
            self._notify(self.tr("This game has no Steam build wizard."), "warn")
            return
        self._open_wizard_tool(tool)

    def _confirm_game_change(self, name):
        """SelectorButton's on_select for the game button — confirms first if
        it would close the NIF Viewer / Character tab, then switches."""
        if name == self._gs.game_name:
            return
        self._confirm_nif_character_close(
            self.tr("Switching game"), lambda: self._on_game_changed(name))

    def _confirm_profile_change(self, name):
        """SelectorButton's on_select for the profile button — confirms first
        if it would close the NIF Viewer / Character tab, then switches."""
        if name == self._gs.profile:
            return
        self._confirm_nif_character_close(
            self.tr("Switching profile"), lambda: self._on_profile_changed(name))

    # ------------------------------------------------------------- deploy/restore
    def _ensure_feedback(self):
        """Lazily create the progress popup stack + notifier (host = central
        widget). _progress_popup is a ProgressStack: default key "op" is the
        shared install/deploy card; downloads pass their own key so concurrent
        cards stack instead of clobbering each other."""
        if self._notifier is None:
            from gui_qt.notifications import NotificationManager, ProgressStack
            host = self.centralWidget() or self
            self._progress_popup = ProgressStack(host)
            self._notifier = NotificationManager(host)

    def _notify(self, text: str, state: str = "info", sticky: bool = False):
        """Show a toast. When *sticky* is True the toast stays on screen until
        its returned handle is dismissed (used for long-running operations like
        the update check) — otherwise it auto-dismisses after a few seconds."""
        self._ensure_feedback()
        return self._notifier.notify(text, state, sticky=sticky)

    def _show_warn_popup(self, title: str, message: str, card_h):
        """ui_hooks.warn → OK-only message card (arrives via _warn_popup)."""
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_message(
            self, title, message,
            card_h=card_h if isinstance(card_h, int) else None)

    def _save_window_state(self):
        """Persist the window geometry (pos/size/maximized) and the modlist ║
        plugins splitter position to mosaic.ini [window]."""
        try:
            from Utils.ui_config import save_qt_window_state
            geo = bytes(self.saveGeometry().toBase64()).decode("ascii")
            split = getattr(self, "_body_split", None)
            save_qt_window_state(geo, list(split.sizes()) if split else None)
        except Exception as exc:
            print(f"[gui_qt] window-state save error: {exc}", flush=True)

    def _schedule_window_state_save(self):
        """(Re)start the debounce timer — one ini write ~1s after the last
        move/resize/splitter drag."""
        t = getattr(self, "_win_state_timer", None)
        if t is not None:
            t.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_window_state_save()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_window_state_save()

    def closeEvent(self, event):
        """On close, optionally restore every deployed game to vanilla (the
        'Restore on close' setting). Synchronous (the app is exiting) — mirrors
        the Tk gui.py shutdown path."""
        self._save_window_state()
        try:
            from Nexus.nxm_handler import NxmIPC
            NxmIPC.shutdown()      # release the IPC socket(s)
        except Exception:
            pass
        try:
            from Utils.ui_config import load_restore_on_close
            if load_restore_on_close():
                self._restore_all_on_close()
        except Exception as exc:
            print(f"[gui_qt] restore-on-close error: {exc}", flush=True)
        super().closeEvent(event)

    # ----------------------------------------------------------------- install
    def _on_install_mod(self):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._install_running:
            self._notify(self.tr("An install is already in progress."), "warning")
            return
        # The picker callback fires on the portal WORKER thread; QTimer.singleShot
        # from there never fires (no event loop on that thread), so marshal to the
        # GUI thread with a Signal (auto-queued to the receiver's thread).
        from Utils.wine_proton.portal_filechooser import pick_files
        pick_files("Select mod archive(s)",
                   lambda ps: self._install_files_picked.emit(ps))

    def _on_install_files_picked(self, paths):
        """GUI thread: start the install once archives were chosen in the portal."""
        if paths:
            self._install_paths([str(p) for p in paths], clear_archives=False)


def _apply_app_identity(app) -> None:
    """Set the application/window icon and Wayland desktop-file association.

    Three things have to happen for the icon to show in the window title bar
    AND the taskbar across from-source, AppImage and Flatpak runs:

    1. QApplication.setWindowIcon() — the window's own icon. Works everywhere
       (including a plain from-source run that has no installed .desktop file)
       because we load the PNG straight off disk. This alone fixes the title
       bar; X11 taskbars also read it.
    2. setDesktopFileName() — on Wayland the compositor won't show a taskbar
       icon from the window itself; it looks up an INSTALLED .desktop file
       (by app_id) and uses that entry's Icon=. AppImage and Flatpak install
       such a file, so we point Qt at it. The basename differs per target.
    3. setApplicationName() — helps the X11 WM_CLASS / labels. (We avoid
       setApplicationDisplayName, which Qt would append to every window title.)
    """
    from PySide6.QtGui import QIcon

    app.setApplicationName("Mosaic Mod Manager")
    # NB: intentionally NOT calling setApplicationDisplayName — Qt auto-appends
    # " — {DisplayName}" to every setWindowTitle(), which duplicated the app
    # name in the title bar ("… - v2.0.0 — Mosaic Mod Manager").

    # Window icon: bundled Logo.png sits next to the other icons (src/icons/).
    # gui_qt/ is a sibling of icons/, so parent.parent/icons/Logo.png.
    logo = Path(__file__).resolve().parent.parent / "icons" / "Logo.png"
    if logo.is_file():
        ic = QIcon(str(logo))
        if not ic.isNull():
            app.setWindowIcon(ic)

    # Desktop-file name for the Wayland taskbar association. Flatpak installs
    # io.github.TheMrGeeBee.MosaicModManager.desktop; the AppImage installs
    # mod-manager.desktop. Detect which we're in so the compositor finds the
    # matching installed entry (and its Icon=). From source there's no
    # installed .desktop, so this is a harmless no-op (the window icon above
    # still covers the title bar).
    #
    # NB: match FLATPAK_ID against OUR id, not just "is any FLATPAK_ID set" —
    # running from source inside another flatpak (e.g. a flatpak VS Code /
    # terminal) sets FLATPAK_ID to that host app, and /.flatpak-info exists
    # for any flatpak-sandboxed parent, so neither is a reliable "we are the
    # Mosaic flatpak" signal on its own.
    if os.environ.get("FLATPAK_ID") == "io.github.TheMrGeeBee.MosaicModManager":
        app.setDesktopFileName("io.github.TheMrGeeBee.MosaicModManager")
    elif os.environ.get("APPDIR") or os.environ.get("APPIMAGE"):
        app.setDesktopFileName("mosaic-mod-manager")


def run() -> int:
    from PySide6.QtWidgets import QApplication

    from Nexus.nxm_handler import NxmHandler, NxmIPC, nxm_log
    from Utils.instance_lock import InstanceLock

    # The browser-spawned handoff process has no GUI, so nxm_log's file sink
    # (logs/nxm.log) is the only record of this launch — log it first thing.
    if "--nxm" in sys.argv:
        nxm_log(f"--nxm launch: argv={sys.argv[1:]}")

    # True single-instance gate: an OS-level advisory lock, not just an IPC
    # socket check. Deploy/Restore have no locking of their own (plain,
    # unlocked files on disk), so a second concurrent instance is a real
    # corruption risk, not just a UX annoyance — this must block a second
    # window from ever being built, not merely hand off nxm:// links.
    # Acquired BEFORE NxmHandler.register()/QApplication so a losing launch
    # does as little work as possible.
    if not InstanceLock.acquire():
        nxm_url = None
        if "--nxm" in sys.argv:
            try:
                idx = sys.argv.index("--nxm")
                nxm_url = sys.argv[idx + 1]
            except (IndexError, ValueError):
                nxm_log("--nxm flag present but no URL argument followed it")
        # The lock is held by a live process, but it may still be starting up
        # — NxmIPC.start_server() only binds after the window is built — so
        # retry briefly rather than giving up on the first miss. Losing the
        # lock race already proves a live process exists.
        delivered = False
        for _ in range(6):
            if NxmIPC.send_to_running(nxm_url or ""):
                delivered = True
                break
            time.sleep(0.5)
        if delivered:
            nxm_log("Handed launch off to running instance "
                     f"({'nxm link' if nxm_url else 'focus ping'}) — exiting")
        else:
            nxm_log("Another instance holds the single-instance lock but "
                     "never answered the handoff — exiting without opening "
                     "a second window (it may still be starting, or hung)")
        return 0

    # We hold the lock: register as the nxm:// handler on every launch
    # (idempotent) so "Download with Manager" on Nexus routes here.
    try:
        NxmHandler.register()
    except Exception:
        import traceback
        nxm_log(f"NxmHandler.register() crashed:\n{traceback.format_exc()}")

    # Migrate/clean mosaic.ini BEFORE anything reads it (theme loader, GameState).
    # Wipes a pre-Qt ini (missing [meta] version=2) so everyone starts fresh.
    from Utils.ui_config import ensure_ini_version, load_language, load_ui_scale
    ensure_ini_version()

    # Apply the user's saved UI scale. Qt only reads QT_SCALE_FACTOR once, at
    # QApplication construction, so this must run before the QApplication below;
    # a scale change from Settings persists the value and self-re-execs (which
    # inherits this environment), so run() must be able to *update or clear*
    # QT_SCALE_FACTOR on every launch — not just set it once.
    #
    # We must not clobber a QT_SCALE_FACTOR the user set in their own shell, yet
    # we must fully own the one we set on a previous re-exec. A marker env var
    # (_AMM_OWNS_SCALE) distinguishes the two: if it's present, the current
    # QT_SCALE_FACTOR is ours from a prior launch and we may overwrite/clear it;
    # if QT_SCALE_FACTOR is present WITHOUT the marker, it's the user's — leave
    # it alone.
    import os as _os
    try:
        _scale = load_ui_scale()
        _ours = _os.environ.get("_AMM_OWNS_SCALE") == "1"
        _user_set = "QT_SCALE_FACTOR" in _os.environ and not _ours
        if not _user_set:
            if abs(_scale - 1.0) > 0.01:
                _os.environ["QT_SCALE_FACTOR"] = (
                    f"{_scale:.4f}".rstrip("0").rstrip("."))
                _os.environ["_AMM_OWNS_SCALE"] = "1"
            else:
                # 1.0 / auto→1.0: drop any scale we set on a previous launch so
                # Qt's native per-monitor DPI handling takes over again.
                _os.environ.pop("QT_SCALE_FACTOR", None)
                _os.environ.pop("_AMM_OWNS_SCALE", None)
    except Exception:
        pass

    app = QApplication(sys.argv)
    # Garbage-collect cycles on the GUI thread only: PySide6 6.12 / Python 3.14
    # segfaults when the collector runs on a worker thread (see gui_qt/gc_guard.py).
    from gui_qt.gc_guard import install as _install_gc_guard
    _install_gc_guard(app)
    _apply_app_identity(app)
    # Install UI translators before any widget is built (Qt only translates
    # tr() calls made after the translator is installed). Language comes from
    # mosaic.ini, which ensure_ini_version() has just finished migrating.
    from gui_qt.i18n import install_translators
    install_translators(app, load_language())
    apply_theme(app)

    # Transparent, correctly-centred splash covering first load. Held open past
    # show() and dismissed by MainWindow on the first completed conflict rebuild
    # (with a watchdog fallback). Never let a splash failure block startup.
    _splash = None
    try:
        from gui_qt.splash import show_splash
        _splash = show_splash()
    except Exception:
        _splash = None

    win = MainWindow(app, splash=_splash)
    # Route stderr + uncaught tracebacks into the log panel now that the log
    # sink (set_app_log) is wired by MainWindow.__init__. Best-effort — a
    # failure here must never block startup.
    try:
        from Utils.stderr_capture import (
            install as _install_stderr_capture,
        )
        from Utils.stderr_capture import (
            install_faulthandler as _install_faulthandler,
        )
        _install_stderr_capture()
        # Native crashes (segfaults from Qt/C code) can't reach a Python hook —
        # faulthandler dumps a C-level traceback to a file in the logs dir so a
        # crashed user still has something to share.
        _install_faulthandler()
    except Exception:
        pass
    # Show the window immediately so its layout gets a real size (the deferred
    # singleShot(0) setup in __init__ reads live widget heights), but minimised
    # so nothing is visibly rendered behind the splash while it loads.
    # setWindowOpacity(0.0) used to do this job, then an off-screen move was
    # tried instead — both are a silent no-op under native Wayland/KWin (no
    # client-requested-opacity protocol most compositors implement, and
    # compositors generally refuse a client's own absolute-position requests
    # too), so the fully-opaque window kept rendering right where it was about
    # to appear, bleeding through the splash's translucent card. Minimising is
    # a basic, universally-implemented window operation on both X11 and
    # Wayland/KWin — Qt still lays the widget hierarchy out normally, it just
    # isn't composited on screen. _dismiss_splash un-minimises (preserving any
    # other state bit, e.g. Maximized, restored from last session) once the
    # plugin panel — the last render step — is populated, so the window
    # appears fully drawn, not mid-render.
    if _splash is not None:
        win.setWindowState(win.windowState() | Qt.WindowMinimized)
    win.show()
    if _splash is not None:
        try:
            _splash.raise_()
        except Exception:
            pass
    # Listen for NXM links handed off by future instances (after the window is
    # up so the received-link handler has a live UI to drive).
    win._start_nxm_ipc()
    rc = app.exec()

    # closeEvent (including any synchronous restore-on-close file I/O) has
    # fully finished by the time app.exec() returns — release the single-
    # instance lock only now, so a new launch can never start touching
    # deploy state while this exit is still cleaning up. Releasing this any
    # earlier (e.g. in closeEvent itself) would reopen the exact race this
    # lock exists to close.
    InstanceLock.release()

    # A language change requests a clean self-restart so the whole UI rebuilds
    # in the new language (no partial live-retranslate). The window's closeEvent
    # has already run (IPC socket released, restore-on-close done); re-exec the
    # same interpreter + argv in place.
    if _RESTART_REQUESTED:
        # Drop a one-shot "--nxm <url>" from the relaunch argv so a stale NXM
        # link isn't reprocessed on the fresh start.
        argv = list(sys.argv)
        if "--nxm" in argv:
            i = argv.index("--nxm")
            del argv[i:i + 2]
        try:
            os.execv(sys.executable, [sys.executable] + argv)
        except Exception:
            # If exec fails, fall through and just exit normally.
            pass
    return rc

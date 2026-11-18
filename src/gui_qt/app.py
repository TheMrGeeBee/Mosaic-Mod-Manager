"""Qt main window — header / body / footer rows + bottom log panel.

  header row : top bar (game/profile + actions) | play bar      (fixed split)
  body row   : modlist ║ plugins                                (draggable)
  footer row : mod tools | plugin tools                         (fixed split)
  log panel  : drag-resizable log text area + control bar
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import time as _time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pathlib import Path as _P

from PySide6.QtCore import QT_TRANSLATE_NOOP, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui_qt import glue
from gui_qt.app_parts.app_bsa import MainWindow as _AppBsaMixin
from gui_qt.app_parts.app_collections import MainWindow as _AppCollectionsMixin
from gui_qt.app_parts.app_log import MainWindow as _AppLogMixin
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
from gui_qt.flow_layout import FlowLayout
from gui_qt.game_state import GameState
from gui_qt.icons import icon
from gui_qt.modlist.modlist_model import COL_SIZE, ModListModel
from gui_qt.modlist.modlist_view import ModListView
from gui_qt.selector_button import SelectorButton
from gui_qt.theme.theme_qt import _c, active_palette, apply_theme

# Diagnostic prints here run on worker threads and use flush=True. Under
# Flatpak/AppImage stdout often has no reader, so a full pipe buffer makes
# print() raise BrokenPipeError, which killed the worker (e.g. the play/deploy
# build). safe_print swallows stream errors; the text is mirrored to the GUI
# log anyway. Shadowing the builtin makes every print(...) below crash-proof.
from Utils.app_log import safe_print as print  # noqa: A004
from Utils.wine_proton.framework_detect import game_provides_requirement
from Utils.wine_proton.proton_tools import DOTNET_VERSIONS

# Set when the user changes language: run() re-execs the process after the event
# loop exits so the whole UI rebuilds in the new language.
_RESTART_REQUESTED = False

# Sentinel emitted on _one_install_done when a FOMOD/BAIN archive was handed off
# to a detached wizard tab: the pipeline advances to the next queued archive
# without counting or renaming it (the detached wizard owns its own outcome).
_WIZARD_HANDOFF = object()

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

    # ---------------------------------------------------------- panel footers
    @staticmethod
    def _enable_height_for_width(w: QWidget) -> None:
        """Make a footer widget report its height via heightForWidth (the wrapped
        FlowLayout height at the *actual* width) rather than its inflated
        sizeHint (which Qt evaluates at the widget's MINIMUM width, i.e. every
        button on its own row). Without this, a footer whose buttons only wrap
        at narrow widths still reserves the fully-wrapped tall height, leaving a
        blank gap above the search box."""
        pol = w.sizePolicy()
        pol.setHeightForWidth(True)
        w.setSizePolicy(pol)

    def _modlist_footer(self) -> QWidget:
        """Buttons row + search box — lives at the bottom of the modlist panel."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # FlowLayout so longer translated labels wrap to a second row instead of
        # overflowing the panel (see gui_qt/flow_layout.py). Centred so the row
        # stays aligned with the middle of the modlist panel at any width.
        btns = FlowLayout(spacing=4, center=True)
        # label -> handler ("" = no-op stub, needs a dialog/auth — wired later).
        _handlers = {
            "Expand all": self._on_toggle_collapse_all,
            "Enable all": self._on_toggle_enable_all,
            "Filters": self._toggle_modlist_filters,
            "Refresh Modlist": self._on_refresh_modlist,
            "Check Updates": self._on_check_updates,
        }
        # (canonical key, translated display). Key drives handler lookup + the
        # == comparisons below; display is the visible button text.
        # "Backups…"/"Backup now…" moved to the profile dropdown menu (see
        # _profile_actions) — they belong next to Export/Import, not buried
        # in this row, per user feedback.
        self._modlist_footer_btns: list[QToolButton] = []
        for label, disp in [("Expand all", self.tr("Expand all")),
                            ("Enable all", self.tr("Enable all")),
                            ("Check Updates", self.tr("Check Updates")),
                            ("Filters", self.tr("Filters")),
                            ("Refresh Modlist", self.tr("Refresh Modlist"))]:
            b = self._text_button(disp, compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            if label in _handlers:
                b.clicked.connect(_handlers[label])
            if label == "Filters":
                b.setProperty("active", False)
                self._modlist_filters_btn = b
            elif label == "Expand all":
                self._expand_all_btn = b
            elif label == "Enable all":
                self._enable_all_btn = b
            elif label == "Check Updates":
                self._check_updates_btn = b
            btns.addWidget(b)
            self._modlist_footer_btns.append(b)
        v.addLayout(btns)

        # Search row: an enabled/total count label (blue outline) to the left of
        # a full-width search box. The count label replaces the old Enabled /
        # Disabled stat pill row.
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)

        count = QLabel("…")
        count.setObjectName("ModlistCountLabel")
        count.setAlignment(Qt.AlignCenter)
        count.setStyleSheet(
            "QLabel#ModlistCountLabel {"
            f" color: {_c(self._pal, 'TEXT_MAIN')};"
            f" border: 1px solid {_c(self._pal, 'ACCENT')};"
            " border-radius: 4px; padding: 2px 8px; }")
        self._modlist_count = count
        search_row.addWidget(count)

        # Search box spans the remaining footer width. (It used to be pinned to
        # the summed button-row width so its edge lined up with the last button;
        # that assumed a single, non-wrapping row — with the FlowLayout the
        # buttons can wrap, so a fixed cap would over/under-shoot. Full width is
        # simpler and robust across languages.)
        search_icon = QLabel()
        search_icon.setPixmap(icon("search.png", 18).pixmap(18, 18))
        search_icon.setToolTip(self._modlist_search_tooltip())
        search_icon.setAlignment(Qt.AlignCenter)
        search_row.addWidget(search_icon)

        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search mods… (try !update, !fomod, !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._on_modlist_search)
        self._modlist_search = search
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._enable_height_for_width(bar)
        return bar

    def _mod_files_footer(self) -> QWidget:
        """Pack/Unpack BSA + Filters buttons + search, shown under the plugins
        column when the Mod Files sub-tab is active (replaces the plugin tools)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._mf_pack_btn = self._color_button(
            self.tr("Pack BSA"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        self._mf_pack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_pack_btn.setEnabled(False)
        self._mf_pack_btn.clicked.connect(self._on_pack_bsa)
        self._mf_unpack_btn = self._color_button(
            self.tr("Unpack BSA"), _c(self._pal, "BTN_DANGER"), compact=True)
        self._mf_unpack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_unpack_btn.setEnabled(False)
        self._mf_unpack_btn.clicked.connect(self._on_unpack_bsa)
        self._mf_filters_btn = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True)
        self._mf_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_filters_btn.clicked.connect(self._toggle_mod_files_filters)
        self._mf_expand_btn = self._text_button(self.tr("⊞ Expand all"), compact=True)
        self._mf_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_expand_btn.clicked.connect(self._on_mf_expand_clicked)
        self._equalize_button_widths(self._mf_pack_btn, self._mf_unpack_btn)
        btns.addWidget(self._mf_expand_btn)
        btns.addWidget(self._mf_pack_btn)
        btns.addWidget(self._mf_unpack_btn)
        v.addLayout(btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._mf_filters_btn)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(
            lambda t: self._mod_files_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._mod_files_search = search
        return bar

    def _data_footer(self) -> QWidget:
        """Filters + Expand-all + search, shown under the plugins column when the
        Data sub-tab is active (no Pack/Unpack — Data is read-only)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._data_filters_btn = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True)
        self._data_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_filters_btn.clicked.connect(self._toggle_data_filters)
        self._data_expand_btn = self._text_button(self.tr("⊞ Expand all"), compact=True)
        self._data_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_expand_btn.clicked.connect(self._on_data_expand_clicked)
        btns.addWidget(self._data_expand_btn)
        v.addLayout(btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._data_filters_btn)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._data_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._data_search = search
        return bar

    def _on_data_expand_clicked(self):
        expanded = self._data_view._toggle_expand_all()
        self._data_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                      else self.tr("⊞ Expand all"))

    def _downloads_footer(self) -> QWidget:
        """Install Selected / Remove Selected / Locations / Filters + search,
        shown under the plugins column when the Downloads sub-tab is active."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._dl_install_btn = self._color_button(
            self.tr("Install Selected"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        self._dl_install_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_install_btn.setEnabled(False)
        self._dl_install_btn.clicked.connect(
            lambda: self._downloads_view.install_selected())
        self._dl_move_btn = self._color_button(
            self.tr("Move Selected"), _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_move_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_move_btn.setEnabled(False)
        self._dl_move_btn.clicked.connect(self._on_downloads_move)
        self._dl_remove_btn = self._color_button(
            self.tr("Remove Selected"), _c(self._pal, "BTN_DANGER"), compact=True)
        self._dl_remove_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_remove_btn.setEnabled(False)
        self._dl_remove_btn.clicked.connect(self._on_downloads_remove)
        self._dl_locations_btn = self._color_button(
            self.tr("Locations"), _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_locations_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_locations_btn.clicked.connect(self._on_downloads_locations)
        self._dl_filters_btn = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_filters_btn.clicked.connect(self._toggle_downloads_filters)
        self._equalize_button_widths(
            self._dl_install_btn, self._dl_move_btn, self._dl_remove_btn)
        btns.addWidget(self._dl_install_btn)
        btns.addWidget(self._dl_move_btn)
        btns.addWidget(self._dl_remove_btn)
        btns.addWidget(self._dl_locations_btn)
        v.addLayout(btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._dl_filters_btn)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search downloads…"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._downloads_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._dl_search = search
        return bar

    def _update_downloads_footer(self):
        n = self._downloads_view.checked_count()
        for attr, label in (("_dl_install_btn", self.tr("Install Selected")),
                            ("_dl_move_btn", self.tr("Move Selected")),
                            ("_dl_remove_btn", self.tr("Remove Selected"))):
            b = getattr(self, attr, None)
            if b is not None:
                b.setEnabled(n > 0)
                b.setText(self.tr("{0} ({1})").format(label, n) if n else label)

    def _on_downloads_locations(self):
        from gui_qt.downloads.download_locations_overlay import DownloadLocationsOverlay
        DownloadLocationsOverlay.show_over(
            self,
            lambda saved: self._downloads_view.refresh() if saved else None)

    def _on_downloads_remove(self):
        paths = self._downloads_view.checked_paths()
        if not paths:
            return
        names = "\n".join(Path(p).name for p in paths[:20])
        more = f"\n… and {len(paths) - 20} more" if len(paths) > 20 else ""

        def _confirmed(ok):
            if not ok:
                return
            removed = 0
            for p in paths:
                try:
                    Path(p).unlink()
                    removed += 1
                except OSError as exc:
                    print(f"[gui_qt] remove failed: {p}: {exc}", flush=True)
            self._notify(self.tr("Removed {0} archive(s)").format(removed), "info")
            self._downloads_view.clear_checks()
            self._downloads_view.refresh()

        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Remove archives"),
            self.tr("Permanently delete {0} archive(s) from disk?\n\n").format(len(paths))
            + names + more,
            _confirmed, confirm_label=self.tr("Delete"))

    def _on_downloads_move(self):
        """Move the checked archives between the *configured* download locations.
        Opens a borderless overlay listing those locations (Downloads / Mod
        Manager cache / extras) rather than a native folder browser."""
        paths = self._downloads_view.checked_paths()
        if not paths:
            return
        game_name = getattr(self._gs, "game_name", None)
        from gui_qt.downloads.move_downloads_overlay import MoveDownloadsOverlay
        MoveDownloadsOverlay.show_over(
            self, len(paths), game_name,
            lambda dest: self._move_downloads_to(paths, dest))

    def _move_downloads_to(self, paths, dest):
        if not dest or not paths:
            return
        dest_dir = Path(dest)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._notify(self.tr("Cannot use that folder: {0}").format(exc), "error")
            return

        # Anything that would overwrite an existing file at the destination needs
        # confirmation before we clobber it.
        clashes = [p for p in paths
                   if (dest_dir / Path(p).name).exists()
                   and Path(dest_dir / Path(p).name).resolve() != Path(p).resolve()]

        def _do_move():
            moved = 0
            for p in paths:
                src = Path(p)
                target = dest_dir / src.name
                try:
                    if src.resolve() == target.resolve():
                        continue  # already there
                except OSError:
                    pass
                try:
                    if target.exists():
                        target.unlink()
                    shutil.move(str(src), str(target))
                    moved += 1
                except OSError as exc:
                    print(f"[gui_qt] move failed: {p} -> {target}: {exc}", flush=True)
            self._notify(self.tr("Moved {0} archive(s)").format(moved), "info")
            self._downloads_view.clear_checks()
            self._downloads_view.refresh()

        if clashes:
            names = "\n".join(Path(p).name for p in clashes[:20])
            more = f"\n… and {len(clashes) - 20} more" if len(clashes) > 20 else ""
            from gui_qt.overlays.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self, self.tr("Overwrite archives"),
                self.tr("{0} file(s) already exist in that folder and will be "
                        "overwritten:\n\n").format(len(clashes)) + names + more,
                lambda ok: _do_move() if ok else None,
                confirm_label=self.tr("Overwrite"))
        else:
            _do_move()

    def _text_files_footer(self) -> QWidget:
        """Search Content / Filters + search, shown under the plugins column when
        the Text Files sub-tab is active (read-only — no Pack/Install)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Inline content-search popup (hidden until "Search Content" is clicked).
        self._tf_content_bar = QWidget()
        cbl = QHBoxLayout(self._tf_content_bar)
        cbl.setContentsMargins(0, 0, 0, 0)
        cbl.setSpacing(4)
        lbl = QLabel(self.tr("Find in files:"))
        lbl.setStyleSheet(f"color:{_c(self._pal,'TEXT_DIM')};")
        cbl.addWidget(lbl)
        self._tf_content_input = QLineEdit()
        self._tf_content_input.setPlaceholderText(self.tr("Text to search for…"))
        self._tf_content_input.setClearButtonEnabled(True)
        self._tf_content_input.returnPressed.connect(self._run_tf_content_search)
        cbl.addWidget(self._tf_content_input, 1)
        go = self._color_button(self.tr("Search"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        go.setFixedHeight(self._FOOT_BTN_H)
        go.clicked.connect(self._run_tf_content_search)
        cbl.addWidget(go)
        close = self._text_button("✕", compact=True)
        close.setFixedHeight(self._FOOT_BTN_H)
        close.clicked.connect(self._close_tf_content_bar)
        cbl.addWidget(close)
        self._tf_content_bar.setVisible(False)
        v.addWidget(self._tf_content_bar)

        btns = FlowLayout(spacing=4)
        self._tf_content_btn = self._color_button(
            self.tr("Search Content"), _c(self._pal, "BTN_INFO"), compact=True)
        self._tf_content_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_content_btn.clicked.connect(self._on_text_files_content_search)
        self._tf_filters_btn = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True)
        self._tf_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_filters_btn.clicked.connect(self._toggle_text_files_filters)
        self._tf_expand_btn = self._text_button(
            self.tr("⊞ Expand all"), compact=True)
        self._tf_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_expand_btn.clicked.connect(self._on_tf_expand_clicked)
        btns.addWidget(self._tf_expand_btn)
        btns.addWidget(self._tf_content_btn)
        # A dim status label showing the active content-search keyword.
        self._tf_content_status = QLabel("")
        self._tf_content_status.setStyleSheet(
            f"color:{_c(self._pal,'TEXT_DIM')};")
        btns.addWidget(self._tf_content_status)
        v.addLayout(btns)
        self._text_files_view.content_status_changed.connect(
            self._on_tf_content_status)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._tf_filters_btn)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._text_files_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._tf_search = search
        return bar

    def _set_tf_content_bar_visible(self, visible: bool):
        """Show/hide the inline find-in-files bar, then re-clamp the footer
        stack: its max height is pinned to the current page's height (see
        _CurrentPageStack), so the page growing a row would otherwise be
        squashed into the old clamped height."""
        self._tf_content_bar.setVisible(visible)
        self._plugin_footer_stack.clamp_to_current()

    def _on_text_files_content_search(self):
        """Toggle the inline content-search bar. When a search is active, this
        clears it; otherwise it opens the input bar above the footer + focuses it."""
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()
            self._tf_content_input.clear()
            self._set_tf_content_bar_visible(False)
            return
        showing = not self._tf_content_bar.isVisible()
        self._set_tf_content_bar_visible(showing)
        if showing:
            self._tf_content_input.setFocus()
            self._tf_content_input.selectAll()

    def _on_tf_expand_clicked(self):
        expanded = self._text_files_view._toggle_expand_all()
        self._tf_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                    else self.tr("⊞ Expand all"))

    def _run_tf_content_search(self):
        kw = self._tf_content_input.text().strip()
        if kw:
            self._text_files_view.run_content_search(kw)
        else:
            self._text_files_view.clear_content_search()

    def _close_tf_content_bar(self):
        self._set_tf_content_bar_visible(False)
        self._tf_content_input.clear()
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()

    def _on_tf_content_status(self, keyword):
        if keyword:
            self._tf_content_status.setText(self.tr('Content: "{0}"').format(keyword))
            self._tf_content_btn.setText(self.tr("Clear Content"))
        else:
            self._tf_content_status.setText("")
            self._tf_content_btn.setText(self.tr("Search Content"))

    def _left_header(self) -> QWidget:
        # Single row: game/profile selectors, then the mod-action buttons.
        header = QWidget()
        header.setObjectName("HeaderBar")
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        # Game selector — no label; the game names make it self-evident. Items
        # are populated from the configured games (_populate_selectors); with
        # none configured the button reads "Add game".
        self._game_selector = SelectorButton(
            items=[],
            current=self.tr("Add game"),
            actions=self._game_actions(),
            on_select=self._confirm_game_change,
        )
        self._game_selector.setFixedHeight(self._BTN_H)
        h.addWidget(self._game_selector)

        # Profile selector — "Profile:" prefix baked into the button text.
        self._profile_selector = SelectorButton(
            items=["default"],
            current="default",
            prefix=self.tr("Profile: "),
            min_width=150,
            actions=self._profile_actions(),
            on_select=self._confirm_profile_change,
        )
        self._profile_selector.setFixedHeight(self._BTN_H)
        # Rebuild the pinned actions on every open (like the Wizard menu): the
        # "Open" submenu and Quick-configure entries are gated on live game
        # settings, so toggling e.g. profile-specific INI files via Quick
        # configure must make the Open submenu appear without a profile swap.
        self._profile_selector._menu.aboutToShow.connect(
            self._refresh_profile_actions)
        h.addWidget(self._profile_selector)

        h.addWidget(self._group_sep())

        # Plain mod-action buttons.
        self._action_buttons = []
        _handlers = {"Install Mod": self._on_install_mod,
                     # drop QPushButton.clicked's `bool checked` arg — it would
                     # otherwise land in _on_deploy's `silent` parameter.
                     "Deploy": lambda: self._on_deploy(), "Restore": self._on_restore}
        for label, disp, ico in [
            ("Install Mod", self.tr("Install Mod"), "install.png"),
            ("Deploy",      self.tr("Deploy"),      "deploy.png"),
            ("Restore",     self.tr("Restore"),     "restore.png"),
        ]:
            # `label` stays the canonical key (handler lookup + _full_label);
            # `disp` is the translated text shown on the button + tooltip.
            b = self._action_button(disp, ico)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(disp)
            b._full_label = disp
            if label in _handlers:
                b.clicked.connect(_handlers[label])
                if label == "Deploy":
                    self._deploy_btn = b
                elif label == "Restore":
                    self._restore_btn = b
                elif label == "Install Mod":
                    self._install_btn = b
            self._action_buttons.append(b)
            h.addWidget(b)

        # Split menu buttons (placeholder menus — wired up in a later phase).
        # `label` stays the canonical key (used in == comparisons + as the
        # button's persistent id); `disp` is the translated button text. Menu
        # item labels are user-visible and translated directly.
        for label, disp, ico, items in [
            ("Proton", self.tr("Proton"), "proton.png", [
                (self.tr("Run winecfg"), self._proton_winecfg),
                (self.tr("Run winetricks"), self._proton_winetricks),
                (self.tr("Run an .exe in this prefix…"), self._proton_run_exe),
                None,
                (self.tr("Open wine registry"), self._proton_regedit),
                (self.tr("Wine DLL overrides"), self._proton_dll_overrides),
                None,
                (self.tr("Install VC++ Redistributable"), self._proton_install_vcredist),
                (self.tr("Install d3dcompiler_47"), self._proton_install_d3dcompiler),
                (self.tr("Install XACT audio (XAudio2)"), self._proton_install_xact),
                (self.tr(".NET runtime"), [
                    (self.tr(".NET {0}").format(v), (lambda v=v: self._proton_install_dotnet(v)))
                    for v in DOTNET_VERSIONS
                ]),
            ]),
            # Wizard's menu is dynamic — rebuilt per game on aboutToShow.
            ("Wizard", self.tr("Wizard"), "wizard.png", []),
            ("Nexus", self.tr("Nexus"), "nexus.png", [
                (self.tr("Open Nexus Mods"), self._open_nexus_browser_tab),
                (self.tr("Open game on nexus"), self._open_game_on_nexus),
                None,
                (self.tr("Login to Nexus"), [
                    (self.tr("Login via SSO"), self._nexus_login_sso),
                    (self.tr("Paste login code…"), self._nexus_paste_code),
                    (self.tr("Clear credentials"), self._nexus_clear_credentials),
                    (self._nxm_menu_label(), self._nexus_toggle_nxm),
                ]),
                (self.tr("Collections"), [
                    (self.tr("Browse collections…"), self._open_collections_tab),
                    (self.tr("Open current collection"), self._open_current_collection),
                    (self.tr("Reset load order"), self._reset_collection_load_order),
                ]),
            ]),
            ("Wabbajack", self.tr("Wabbajack"), "wabbajack.png", [
                (self.tr("Browse modlists…"),
                 lambda: self._wabbajack_controller().open_browser()),
                (self.tr("Import .wabbajack file…"),
                 lambda: self._wabbajack_controller().start_import()),
                None,
                (self.tr("LoversLab"), [
                    (self.tr("Log in…"),
                     lambda: self._wabbajack_controller().loverslab_login()),
                    (self.tr("Clear credentials"),
                     lambda: self._wabbajack_controller().loverslab_logout()),
                ]),
                (self.tr("Open Wabbajack gallery website"), self._open_wabbajack_website),
            ]),
        ]:
            # Proton's logo is a mono glyph — tint it white like the Settings
            # icon so it stays visible. The others are full-colour logos.
            tint = _c(self._pal, "TEXT_MAIN") if label == "Proton" else None
            b = self._menu_action_button(disp, ico, items, tint=tint)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(disp)
            b._full_label = disp
            if label == "Wizard":
                self._wizard_btn = b
                b._menu.aboutToShow.connect(self._rebuild_wizard_menu)
            self._action_buttons.append(b)
            h.addWidget(b)

        h.addStretch(1)

        # Settings — icon-only square button on the far right. Opens a Settings
        # tab scoped over the Plugins panel.
        self._settings_button = self._icon_square_button(
            "settings.png", tooltip=self.tr("Settings"), tint=_c(self._pal, "TEXT_MAIN"))
        self._settings_button.clicked.connect(self._open_settings_tab)
        h.addWidget(self._settings_button)

        self._left_header_widget = header
        return header

    def _icon_square_button(self, icon_name: str, tooltip: str = "",
                            tint: str | None = None) -> QToolButton:
        """A compact square icon-only button (e.g. Settings) for the toolbar.

        *tint* recolours a mono glyph to a theme colour so it stays visible in
        both light and dark modes (the raw PNGs are white)."""
        b = QToolButton()
        b.setIcon(icon(icon_name, self._ICON_PX, color=tint))
        b.setIconSize(QSize(self._ICON_PX, self._ICON_PX))
        b.setToolButtonStyle(Qt.ToolButtonIconOnly)
        b.setObjectName("IconButton")
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(self._BTN_H, self._BTN_H)
        if tooltip:
            b.setToolTip(tooltip)
        return b

    # The action buttons always show text+icon: the full-width top bar has room
    # for them even at the 1280 minimum, so the old icon-only collapse (with its
    # threshold/flicker tuning) is no longer needed.

    # ---- selector handlers -------------------------------------------------
    def _on_game_changed(self, name):
        if name == self._gs.game_name:
            return
        # The NIF Viewer / Character tabs index the previous game — close them
        # (see _close_nif_character_tabs) like every other game-scoped tab
        # below. The confirmation this warrants for a user-initiated switch is
        # the selector's own job (_confirm_game_change) — this method itself
        # stays synchronous since several callers (Nexus auto-switch-on-
        # download, Add Game, collection-cancel cleanup) depend on that.
        self._close_nif_character_tabs()
        self._gs.set_game(name)
        # The Profile Settings tab is scoped to the previous game — close it.
        if self._tabs.has_key("profile_settings"):
            self._tabs.close_tab("profile_settings")
            self._profile_settings_view = None
        # The Wine DLL overrides tab is game-scoped too — close it.
        if self._tabs.has_key("dll_overrides"):
            self._tabs.close_tab("dll_overrides")
            self._dll_overrides_view = None
        # The View Requirements tab reads the previous game's staging — close
        # it (its destroyed hook restores the conflict highlights).
        if self._tabs.has_key("view_requirements"):
            self._tabs.close_tab("view_requirements")
        # Wizard tool tabs are game-scoped — close them all.
        self._close_wizard_tabs()
        # The exe-settings tab is bound to the previous game's exe.
        self._close_exe_settings_tab()
        # Userlist tabs/bars hold the previous profile's userlist path.
        self._close_userlist_ui()
        # Retarget the Nexus / Collections browsers (if open) at the new game so
        # they show the new game's mods instead of holding a stale domain. A
        # missing/empty Nexus domain closes them (nothing to show).
        self._retarget_browsers_for_game()
        # Reflect the new game's profiles + keep both game selectors in sync.
        profs = self._gs.profiles()
        if profs:
            self._profile_selector.set_items(profs, current=self._gs.profile)
        # The Open submenu depends on the new game's profile-specific settings.
        self._refresh_profile_actions()
        self._game_selector.set_current(name)
        # The 'Edit custom game…' entry depends on the active game.
        self._refresh_game_actions()
        self._refresh_play_selector()
        self._clear_search_boxes()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()

    def _retarget_browsers_for_game(self):
        """On game switch, point any open Nexus / Collections browser at the new
        game (or close them if the new game has no Nexus domain). Per-collection
        detail tabs are game-specific, so they're closed unconditionally."""
        game = self._gs.game
        domain = (getattr(game, "nexus_game_domain", "") or "") if game else ""

        # The Wabbajack browser isn't tied to Nexus: it just re-filters.
        wj = getattr(self, "_wabbajack", None)
        if wj is not None:
            try:
                wj.on_game_changed(game)
            except Exception as exc:
                self._append_log(f"[wabbajack] retarget failed: {exc}")

        # Close per-collection detail tabs (they belong to the previous game).
        # Swallow (a game switch must not abort) but leave a trace — a failure
        # here leaves stale tabs from the previous game.
        try:
            for key in [k for k in list(self._tabs._keys)
                        if k.startswith("collection_detail_")]:
                self._tabs.close_tab(key)
        except Exception as exc:
            from Utils.app_log import app_log
            app_log(f"game switch: closing collection tabs failed: {exc}")

        for attr, tab_key in (("_nexus_view", "nexus_browser"),
                              ("_collections_view", "collections")):
            view = getattr(self, attr, None)
            if view is None:
                continue
            if not domain:
                # New game can't show Nexus content — close the tab.
                if self._tabs.has_key(tab_key):
                    self._tabs.close_tab(tab_key)
                setattr(self, attr, None)
                continue
            try:
                view.set_game(game, domain)
            except Exception as exc:
                self._append_log(f"[nexus] retarget failed: {exc}")

    def _clear_search_boxes(self):
        """Reset both search boxes on a game/profile switch (Tk parity — a
        stale query silently filters the freshly-loaded lists)."""
        for attr, text_attr in (("_modlist_search", "_modlist_search_text"),
                                ("_plugins_search", "_plugin_search_text")):
            box = getattr(self, attr, None)
            if box is not None and box.text():
                box.clear()   # textChanged → debounced apply sees empty
            setattr(self, text_attr, "")

    def _on_profile_changed(self, name):
        if name == self._gs.profile:
            return
        # The NIF Viewer / Character tabs index the previous profile — close
        # them (see _close_nif_character_tabs) like the other profile-scoped
        # UI below. Confirming this for a user-initiated switch is the
        # selector's own job (_confirm_profile_change) — this method stays
        # synchronous since callers (collection-cancel cleanup) depend on that.
        self._close_nif_character_tabs()
        from Utils import perftrace
        # End-to-end switch latency: the switch "feels done" only when the async
        # milestones land (meta → plugins → conflicts → final plugin pass), so
        # stamp t0 here and mark elapsed at each (see _mark_since_switch).
        if perftrace.is_enabled():
            self._switch_t0 = time.perf_counter()
            self._switch_conflicts_done = False
        with perftrace.span("switch.sync_total"):
            with perftrace.span("switch.set_profile"):
                self._gs.set_profile(name)
            self._profile_selector.set_current(name)
            self._update_profile_collection_tooltip()
            # profile_ini_files / profile_saves are per-profile overrides — set_profile
            # reloaded them, so refresh the Open submenu for the new profile.
            self._refresh_profile_actions()
            # Userlist tabs/bars are profile-scoped (userlist.yaml lives there).
            self._close_userlist_ui()
            # Exe selection + exe args are per-profile.
            self._close_exe_settings_tab()
            with perftrace.span("switch.refresh_play_selector"):
                self._refresh_play_selector()
            self._clear_search_boxes()
            # Non-specific profiles share the mods folder: a mod added while
            # another profile was active is on disk but absent from this
            # profile's modlist.txt. Sync the file against the folder so the
            # new mod shows up on switch without a manual Refresh. This is
            # cheap (one iterdir + modlist.txt diff) and does NOT rescan the
            # shared index/filemap, which is unchanged across the switch.
            with perftrace.span("switch.sync_modlist_folder"):
                try:
                    from Utils.mods.modlist import sync_modlist_with_mods_folder
                    ml = self._gs.modlist_path()
                    staging = self._gs.staging_dir()
                    if ml is not None and staging is not None:
                        sync_modlist_with_mods_folder(ml, staging)
                except Exception as exc:
                    print(f"[gui_qt] profile-switch modlist sync failed: {exc}",
                          flush=True)
            with perftrace.span("switch.reload_modlist(sync)"):
                self._reload_modlist()
            with perftrace.span("switch.reload_plugins(kickoff)"):
                if getattr(self, "_reload_had_entries", False):
                    # The conflict rebuild just queued by _reload_modlist ends
                    # in a plugin reload against the FRESH filemap. Loading now
                    # too would run the whole per-plugin pipeline twice — and
                    # the early pass resolves against the OLD profile's filemap
                    # (observed: 10 of 254 plugins listed until the rebuild
                    # corrected it). Clear the stale panel; the rebuild's
                    # reload populates it once, correctly.
                    self._clear_plugin_panel()
                else:
                    # Empty modlist → no conflict rebuild is coming; load the
                    # (vanilla-only) plugin list directly.
                    self._reload_plugins()
            self._update_deployed_profile_highlight()
        # Appended-collections section tracks the ACTIVE profile.
        view = getattr(self, "_collections_view", None)
        if view is not None:
            try:
                view.refresh_appended()
            except Exception:
                pass
        # Keep the Profile Settings ★ marker in sync if that tab is open.
        if self._tabs.has_key("profile_settings"):
            v = getattr(self, "_profile_settings_view", None)
            if v is not None:
                v.set_current_profile(name)
        # Configure-Game tab tracks the active profile too — refresh its form +
        # tab title in place (no focus change) so the user doesn't have to
        # reopen it to configure a different profile.
        if self._tabs.has_key("configure_game"):
            v = getattr(self, "_configure_game_view", None)
            if v is not None:
                try:
                    v.refresh_for_profile()
                    self._tabs.set_tab_title(
                        "configure_game", self._configure_tab_title(self._gs.game))
                except Exception:
                    pass

    def _mark_since_switch(self, label: str) -> None:
        """Perf instrumentation (MM_PERFTRACE=1): record wall-clock elapsed
        since the last profile switch under *label*. The switch's perceived
        latency spans thread boundaries (sync UI work → meta worker → plugin
        worker → conflict/filemap worker → final plugin pass), which a single
        span can't measure — so each async milestone marks its own arrival
        time. No-op when perftrace is disabled or no switch is in flight."""
        t0 = getattr(self, "_switch_t0", None)
        if t0 is None:
            return
        from Utils import perftrace
        perftrace.mark(label, time.perf_counter() - t0)

    def _game_actions(self):
        """Build the pinned entries for the game selector. The 'Edit custom
        game…' entry only appears when the active game is a custom game whose
        definition is editable (i.e. not a repo handler with editable: false)."""
        actions = [
            (self.tr("Add game…"), lambda: self._on_game_action("add")),
            (self.tr("Configure game…"), lambda: self._on_game_action("configure")),
            (self.tr("Define custom game…"), lambda: self._on_game_action("custom")),
        ]
        game = getattr(self._gs, "game", None)
        if (game is not None
                and getattr(game, "is_custom", False)
                and getattr(game, "editable", False)):
            actions.append(
                (self.tr("Edit custom game…"), lambda: self._on_game_action("edit_custom")))
        # Repo handlers (downloaded from the Resources branch) carry version +
        # editable metadata; user-authored definitions don't. Only those can be
        # force-refreshed from the repo.
        defn = getattr(game, "_defn", None) if game is not None else None
        if (game is not None
                and getattr(game, "is_custom", False)
                and isinstance(defn, dict)
                and "version" in defn and "editable" in defn):
            actions.append(
                (self.tr("Force update handler"),
                 lambda: self._on_game_action("update_handler")))
        actions.append(
            (self.tr("Open"), [
                (self.tr("Game folder"),     lambda: self._open_game_dir("game")),
                (self.tr("Prefix folder"),   lambda: self._open_game_dir("prefix")),
                (self.tr("My Games folder"), lambda: self._open_game_dir("mygames")),
                (self.tr("AppData folder"),  lambda: self._open_game_dir("appdata")),
                (self.tr("Staging folder"),  lambda: self._open_game_dir("staging")),
                (self.tr("Profile folder"),  lambda: self._open_game_dir("profile")),
                (self.tr(".config folder"),  lambda: self._open_game_dir("config")),
            ]))
        return actions

    def _refresh_game_actions(self):
        """Rebuild the game-selector menu so the 'Edit custom game…' entry
        appears/disappears to match the active game."""
        if getattr(self, "_game_selector", None) is not None:
            self._game_selector.set_actions(self._game_actions())

    def _on_game_action(self, which):
        if which == "add":
            self._open_add_game_tab()
        elif which == "configure":
            game = self._gs.game
            if game is not None:
                self._open_configure_game_tab(game)
            else:
                self._append_log("[game] no active game to configure")
        elif which == "custom":
            self._open_custom_game_tab()
        elif which == "edit_custom":
            self._open_edit_custom_game_tab()
        elif which == "update_handler":
            self._force_update_handler()
        else:
            self._append_log(f"[game] {which} (not wired yet)")

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

    def _set_deploy_buttons_enabled(self, enabled: bool):
        for b in (getattr(self, "_deploy_btn", None), getattr(self, "_restore_btn", None),
                  getattr(self, "_play_btn", None)):
            if b is not None:
                b.setEnabled(enabled)

    def _prompt_mewgenics_deploy(self, game):
        """Ask whether to generate a Steam launch command or repack the gpak,
        then act on the choice (Tk parity: gui/mewgenics_dialogs.py)."""
        from gui_qt.overlays.mewgenics_deploy_overlay import (
            MewgenicsDeployChoiceOverlay,
            MewgenicsLaunchCommandOverlay,
        )
        profile = self._gs.profile

        def _on_choice(result):
            if result is None:
                return
            if result == "steam":
                try:
                    launch_string, modpaths_file = \
                        game.get_modpaths_launch_string(profile)
                except Exception as exc:
                    self._notify(
                        self.tr("Could not build launch command: {0}")
                        .format(exc), "warning")
                    return
                MewgenicsLaunchCommandOverlay(
                    self.window(), launch_string, modpaths_file)
                return
            # result == "repack" -> run the normal deploy pipeline
            self._start_deploy(game, profile)

        MewgenicsDeployChoiceOverlay(self.window(), _on_choice)

    def _start_deploy(self, game, profile, silent: bool = False):
        """Run the deploy worker for *game* / *profile* without re-prompting
        (used by the Mewgenics 'repack' choice, which has already decided)."""
        if self._deploy_running:
            self._deploy_rerun_pending = True
            self._auto_deploy_in_progress = False
            return
        # A manual install, a collection install, or a detached-wizard staging
        # job mutates the shared game (staging root + active profile) — a
        # deploy started now could resolve the wrong profile / race the
        # staging tree, same hazard _install_paths guards against on the
        # install side (see its comment for the full explanation). Defer
        # until all of them drain; released by _drain_pending_after_staged.
        # Coalesce duplicate deferrals per game.
        if getattr(self, "_install_running", False) \
                or getattr(self, "_col_install_running", False) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            self._auto_deploy_in_progress = False
            if not any(getattr(cb, "_kind", None) == "deploy"
                       for cb in self._pending_after_staged):
                def _later(g=game, p=profile, s=silent):
                    self._start_deploy(g, p, silent=s)
                _later._kind = "deploy"
                self._pending_after_staged.append(_later)
            return
        self._deploy_running = True
        self._op_silent = silent
        self._op_is_restore = False
        self._op_title = "Deploying"
        self._set_deploy_buttons_enabled(False)
        if not silent:
            self._ensure_feedback()
            self._notify(self.tr("Deploying {0}…").format(game.name), "info")
        rf_enabled = True


        def worker():
            from Utils.deploy.deploy_pipeline import run_deploy_pipeline
            ok = False
            warns = []
            try:
                ok = run_deploy_pipeline(
                    game, profile,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, p=None: self._op_progress.emit(d, t, p),
                    root_folder_enabled=rf_enabled,
                    confirm_cet=self._make_confirm_cet_cb(game),
                    do_backup=True,
                )
            except Exception as exc:
                self._op_log.emit(f"Deploy error: {exc}")
            finally:
                try:
                    warns = list(game.pop_deploy_warnings())
                except Exception:
                    warns = []
                self._op_done.emit("deploy", bool(ok), warns)

        threading.Thread(target=worker, daemon=True).start()

    def _on_deploy(self, silent: bool = False):
        """Deploy the current game/profile. *silent* (used by auto-deploy)
        suppresses the progress popup + interim toast so rapid mod toggles
        don't flash the UI; log lines + the final success/warning toasts still
        surface (Tk parity: top_bar._run_deploy silent=)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._auto_deploy_in_progress = False
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if not hasattr(game, "deploy"):
            self._auto_deploy_in_progress = False
            self._notify(self.tr("'{0}' does not support deployment.").format(game.name), "warning")
            return

        # A game exposing get_modpaths_launch_string (Mewgenics) offers a Steam
        # launch command as an alternative to repacking its archive (Tk parity:
        # top_bar._on_deploy). Only prompt for an explicit user deploy —
        # auto-deploy (silent) falls straight through to the repack path.
        if not silent and hasattr(game, "get_modpaths_launch_string"):
            self._prompt_mewgenics_deploy(game)
            return

        self._start_deploy(game, self._gs.profile, silent=silent)

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

    def _restore_all_on_close(self):
        """Restore every configured game that has an active deployment back to
        vanilla. Ported from gui.py `_restore_all_on_close`."""
        from gui_qt.game_state import _GAMES
        from Utils.deploy.deploy import restore_root_folder

        games = [g for g in _GAMES.values()
                 if g.is_configured() and g.get_deploy_active()
                 and getattr(g, "restore_on_close_eligible", True)]
        if not games:
            return
        log_fn = lambda m: print(f"[restore-on-close] {m}", flush=True)
        log_fn(f"restoring {len(games)} game(s)...")
        for game in games:
            try:
                game_root = game.get_game_path()
                last_deployed = game.get_last_deployed_profile()
                original_profile_dir = getattr(game, "_active_profile_dir", None)
                if last_deployed:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / last_deployed)
                    # Reload so the last-deployed profile's path overrides drive
                    # the restore (it may target a different game folder).
                    game.load_paths()
                    game_root = game.get_game_path()
                try:
                    if hasattr(game, "restore"):
                        game.restore(log_fn=log_fn)
                    root_folder_dir = game.get_effective_root_folder_path()
                    if root_folder_dir.is_dir() and game_root:
                        restore_root_folder(root_folder_dir, game_root, log_fn=log_fn)
                    game.clear_deploy_active()
                finally:
                    # Restored unconditionally — skipping this when the entry
                    # value was None left the game on the LAST-DEPLOYED profile,
                    # so later staging/meta reads resolved to the wrong folder.
                    game.set_active_profile_dir(original_profile_dir)
                    game.load_paths()
            except Exception as e:
                log_fn(f"error for {game.name}: {e}")

    def _on_restore(self, *, ignore_col_install: bool = False):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._deploy_running:
            self._notify(self.tr("A deploy is in progress — try again shortly."), "warning")
            return
        # Defer behind a manual install, a collection install, or a
        # detached-wizard staging job (all mutate the shared game's active
        # profile — see _install_paths / _start_deploy for the full
        # explanation). Released by _drain_pending_after_staged. Coalesce
        # duplicate restore requests.
        # *ignore_col_install* — the collection preflight (see
        # _preflight_restore_then_apply) runs its own Restore step WHILE
        # _col_install_running is True, before any profile exists or any
        # file is staged. Queuing behind "the current install" there would
        # deadlock: the install is itself blocked waiting on this restore.
        if getattr(self, "_install_running", False) \
                or (getattr(self, "_col_install_running", False) and not ignore_col_install) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            if not any(getattr(cb, "_kind", None) == "restore"
                       for cb in self._pending_after_staged):
                def _later():
                    self._on_restore()
                _later._kind = "restore"
                self._pending_after_staged.append(_later)
            self._notify(self.tr("Restore queued — it will run after the "
                                 "current install finishes."), "info")
            return
        self._deploy_running = True
        self._op_is_restore = True
        self._op_title = "Restoring"
        self._set_deploy_buttons_enabled(False)
        self._ensure_feedback()
        self._notify(self.tr("Restoring {0}…").format(game.name), "info")
        profile = self._gs.profile

        from Utils.deploy.deploy import restore_root_folder

        def worker():
            ok = True
            try:
                from Utils.deploy.deploy_pipeline import check_paths_mounted
                err = check_paths_mounted(game)
                if err:
                    self._op_log.emit(f"Restore aborted: {err}")
                    ok = False
                else:
                    last = game.get_last_deployed_profile()
                    if last:
                        game.set_active_profile_dir(
                            game.get_profile_root() / "profiles" / last)
                        game.load_paths()
                    game_root = game.get_game_path()
                    if hasattr(game, "restore"):
                        game.restore(
                            log_fn=lambda m: self._op_log.emit(str(m)),
                            progress_fn=lambda d, t, p=None: self._op_progress.emit(d, t, p))
                    rf = game.get_effective_root_folder_path()
                    if rf.is_dir() and game_root:
                        restore_root_folder(
                            rf, game_root,
                            log_fn=lambda m: self._op_log.emit(str(m)),
                            data_deploy_dirs=(game.root_restore_protect_dirs()
                                              if hasattr(game, "root_restore_protect_dirs") else None))
            except Exception as exc:
                ok = False
                self._op_log.emit(f"Restore error: {exc}")
            finally:
                # Always restore the active profile dir to the selected profile.
                try:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / profile)
                    game.load_paths()
                    if ok and hasattr(game, "clear_deploy_active"):
                        game.clear_deploy_active()
                except Exception:
                    pass
                self._op_done.emit("restore", ok, [])

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_op_clear(self, delay_ms: int = 1200):
        """Hide the shared "op" progress card after *delay_ms*. Cancellable —
        if another operation emits progress before it fires, the pending clear
        is dropped so it can't hide the NEW operation's card (e.g. a queued
        install batch that starts right after the previous one finished)."""
        t = getattr(self, "_op_clear_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(
                lambda: self._progress_popup.clear()
                if self._progress_popup is not None else None)
            self._op_clear_timer = t
        t.start(delay_ms)

    def _on_op_progress(self, done: int, total: int, phase):
        if getattr(self, "_op_silent", False):
            return   # silent auto-deploy: no progress popup
        if self._progress_popup is not None:
            # A new/ongoing op is reporting — a clear scheduled by the previous
            # op's completion must not hide this op's card.
            t = getattr(self, "_op_clear_timer", None)
            if t is not None:
                t.stop()
            title = getattr(self, "_op_title", "Working")
            self._progress_popup.set_progress(done, total, phase, title=title)

    def _on_op_done(self, kind: str, success: bool, warnings):
        self._deploy_running = False
        self._op_silent = False
        self._set_deploy_buttons_enabled(True)
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        # Any deploy (manual OR auto) is followed by a modlist reload → conflict
        # rebuild → _on_conflicts_ready. With auto_deploy on, that rebuild would
        # otherwise start a *fresh* auto-deploy every time (an endless loop, and
        # an extra deploy even after a manual Deploy press). Arm the guard so the
        # rebuild caused by THIS deploy's reload is swallowed. (Tk parity: the
        # gui.py entry path + top_bar._on_deploy_finished both set this flag.)
        game = self._gs.game
        if (kind == "deploy" and game is not None and game.is_configured()
                and getattr(game, "auto_deploy", False)):
            self._auto_deploy_in_progress = True
        # Refresh the modlist/conflicts + deployed-profile highlight after the op.
        self._reload_modlist()
        self._update_deployed_profile_highlight()
        self._refresh_framework_banner()
        # Deploy/restore adds/removes framework launchers (script extenders)
        # in the game root — reflect that in the play-bar dropdown.
        self._refresh_play_selector()
        game_name = self._gs.game.name if self._gs.game else self.tr("Game")
        if success:
            msg = (self.tr("{0} Deployed") if kind == "deploy"
                   else self.tr("{0} Restored")).format(game_name)
            self._notify(msg, "success")
        else:
            self._notify(self.tr("Deploy failed — see log.") if kind == "deploy"
                         else self.tr("Restore failed — see log."), "error")
        for w in (warnings or []):
            self._notify(w, "warning")
        # Drain any install batch that was queued behind this deploy/restore
        # (see _install_paths). Deferred so it starts after this handler +
        # the coalesced re-deploy check below settle. Skipped if a coalesced
        # re-deploy is about to run — the install stays queued until it too
        # finishes (the drain guards on _deploy_running/_install_running).
        if self._pending_install_batches and not self._deploy_rerun_pending:
            QTimer.singleShot(0, self._drain_pending_installs)
        # A detached-wizard staging job may have queued behind this deploy —
        # drain it once the deploy is fully settled (skipped if a coalesced
        # re-deploy is about to run; _run_staged_finish re-guards on
        # _deploy_running so it stays queued until that finishes too).
        if self._staged_finish_queue and not self._deploy_rerun_pending:
            QTimer.singleShot(0, self._run_staged_finish)
        # Coalesced re-deploy if mod state changed mid-deploy.
        if kind == "deploy" and self._deploy_rerun_pending:
            self._deploy_rerun_pending = False
            # A coalesced re-deploy already reflects a decided deploy — run it
            # directly (no fresh prompt/toast; matters for the Mewgenics choice).
            game = self._gs.game
            if game is not None and game.is_configured():
                QTimer.singleShot(
                    0, lambda g=game, p=self._gs.profile: self._start_deploy(g, p))
            else:
                # The re-deploy won't run (game gone/unconfigured) — the drains
                # skipped above on _deploy_rerun_pending would otherwise strand
                # the queues. Release them now.
                if self._pending_install_batches:
                    QTimer.singleShot(0, self._drain_pending_installs)
                if self._staged_finish_queue:
                    QTimer.singleShot(0, self._run_staged_finish)
        elif kind == "deploy":
            # Play-button deploy-before-launch: fire the pending launch only
            # after the FINAL (non-coalesced) deploy, and only on success.
            action, self._post_deploy_action = self._post_deploy_action, None
            if action is not None and success:
                action()
            elif action is not None:
                self._append_log(
                    "Play: deploy failed — the pending launch was cancelled.")
            # Wizard deploy steps: one-shot completion hooks (get the outcome
            # either way so the wizard can show failure and re-enable Deploy).
            hooks, self._deploy_done_hooks = self._deploy_done_hooks, []
            for h in hooks:
                try:
                    h(success)
                except Exception as exc:
                    self._append_log(f"Wizards: deploy hook error: {exc}")
        elif kind == "restore":
            hooks, self._restore_done_hooks = self._restore_done_hooks, []
            for h in hooks:
                try:
                    h(success)
                except Exception as exc:
                    self._append_log(f"Wizards: restore hook error: {exc}")

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

    def _install_paths(self, paths: list[str], metas: dict | None = None,
                       previous_mod_name: str | None = None,
                       preferred_names: dict | None = None,
                       on_all_done=None, clear_archives: bool = True):
        """Queue + install a list of archive paths (shared by the Install Mod
        button and the Downloads tab). FOMODs pause for the wizard mid-queue.
        *metas* optionally maps an archive path → a prebuilt NexusModMeta (the
        Nexus browser supplies the real mod_id/file_id so meta.ini is correct).
        *previous_mod_name* — when set (Change Version updating an existing mod),
        and a single install lands under a DIFFERENT folder name, offer to remove
        that previous version (it inherits its modlist slot).
        *preferred_names* — optional archive-path → forced mod-folder name. Forces
        the install into that folder and SILENTLY replaces it (no Mod-Already-Exists
        dialog). Used by Quick Update, where the name match is already confirmed.
        *on_all_done* — optional no-arg callback fired once the whole batch finishes
        (after the summary), so a caller can chain post-install work (Quick Update
        re-checks flags + reports its own summary).
        *clear_archives* — False for archives the USER supplied (Install Mod
        button, Downloads tab, reinstall-from-archive): 'Clear archive after
        install' only applies to archives the app downloaded itself."""
        if not paths:
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # Queue behind a running install OR deploy OR a detached-wizard staging
        # job. All three mutate the SAME shared game object (_active_profile_dir +
        # load_paths → get_effective_*_path); a deploy swaps the active profile to
        # last_deployed/target mid-flight (deploy_pipeline), so an install started
        # concurrently can stage its mod + write modindex.bin into the WRONG
        # profile's tree — the mod then appears only after a Refresh (the
        # intermittent "manual install didn't take" bug). Serialize instead.
        # _on_install_done, _on_op_done (deploy) AND _on_wizard_finish_done (staged
        # finish) all drain the queue.
        if getattr(self, "_install_running", False) \
                or getattr(self, "_deploy_running", False) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            self._pending_install_batches.append({
                "paths": list(paths), "metas": metas,
                "previous_mod_name": previous_mod_name,
                "preferred_names": preferred_names,
                "on_all_done": on_all_done,
                "clear_archives": clear_archives})
            _busy = self.tr("install") if getattr(self, "_install_running", False) \
                else (self.tr("deploy") if getattr(self, "_deploy_running", False)
                      else self.tr("install"))
            self._notify(
                self.tr("Install queued — {0} will install after the current "
                        "{1} finishes.").format(Path(paths[0]).name, _busy), "info")
            return
        # A just-finished background worker (deploy/restore/collection install)
        # may have left the game's _active_profile_dir out of sync with the
        # selected profile — re-assert it so staging + the mod index resolve
        # against the profile the dropdown shows (Tk parity: gui.py re-ran
        # load_paths before installing).
        self._gs.reassert_active_profile()
        profile_dir = self._gs.profile_dir()
        if profile_dir is None:
            self._notify(self.tr("No active profile."), "warning")
            return
        self._install_running = True
        self._op_is_restore = False
        self._op_title = "Installing"
        if hasattr(self, "_install_btn"):
            self._install_btn.setEnabled(False)
        self._ensure_feedback()
        # Process archives ONE AT A TIME so a FOMOD can pause for the wizard.
        self._install_queue = list(paths)
        self._install_total = len(paths)
        self._install_ok = []
        # Archives handed off to a detached FOMOD/BAIN wizard: they leave the
        # pipeline immediately and report their own outcome later, so they count
        # toward neither ok nor the failure tally (see _on_install_done summary).
        self._install_handoffs = 0
        self._install_game = game
        self._install_profile_dir = profile_dir
        self._install_metas = dict(metas or {})
        self._install_prev_name = previous_mod_name
        self._install_preferred = dict(preferred_names or {})
        self._install_all_done_cb = on_all_done
        self._install_clear_archives = clear_archives
        self._notify(self.tr("Installing {0} mod(s)…").format(len(paths)) if len(paths) > 1
                     else self.tr("Installing {0}…").format(Path(paths[0]).name), "info")
        # Multi-archive batch (Downloads tab multi-select, Nexus batches):
        # extract several archives at once like a collection install, deferring
        # FOMOD/BAIN wizards to a sequential phase at the end. Change Version
        # (previous_mod_name) is always a single archive; keep it sequential.
        if len(paths) > 1 and previous_mod_name is None:
            self._install_batch_parallel()
        else:
            self._install_next()

    def _make_need_prefix_cb(self):
        """Return an on_need_prefix(required, file_list, mod_name) callback for the
        install worker. It runs on the WORKER thread, so it asks the UI thread to
        show the Set-Prefix overlay (via _need_prefix) and BLOCKS on an Event
        until the user responds — mirroring the Tk non-main-thread prefix dialog.
        Returns the prefix str ("" = as-is), or None (cancel)."""

        def _cb(required, file_list, mod_name):
            holder = {"result": None}
            ev = threading.Event()
            self._need_prefix.emit({
                "required": required, "file_list": file_list,
                "mod_name": mod_name, "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_need_prefix_ui(self, payload):
        """UI thread: show the Set-Prefix overlay; unblock the worker when done.
        The progress popup is hidden while the user decides (no work running)."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.overlays.set_prefix_overlay import SetPrefixOverlay

        def _done(result):
            payload["holder"]["result"] = result
            payload["event"].set()

        SetPrefixOverlay.show_over(
            self, payload["mod_name"], payload["required"],
            payload["file_list"], _done)

    def _make_exists_cb(self):
        """Return an on_exists(mod_name, conflict) callback for finish_install.
        Runs on the WORKER thread → shows the Mod-Already-Exists overlay on the
        UI thread and BLOCKS until the user picks (replace / rename:<n> / cancel),
        mirroring _make_need_prefix_cb."""

        def _cb(mod_name, conflict=False):
            holder = {"result": "cancel"}
            ev = threading.Event()
            self._mod_exists.emit({
                "mod_name": mod_name, "conflict": bool(conflict),
                "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_mod_exists_ui(self, payload):
        """UI thread: show the Mod-Already-Exists overlay; unblock the worker."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.overlays.mod_exists_overlay import ModExistsOverlay

        def _done(result):
            payload["holder"]["result"] = result or "cancel"
            payload["event"].set()

        ModExistsOverlay.show_over(
            self, payload["mod_name"], payload["conflict"], _done)

    def _make_variant_choice_cb(self):
        """Return an on_variant_choice(existing_name, new_name) callback for
        prepare_archive. Runs on the WORKER thread → shows the Same-Mod-Page-
        Different-File overlay on the UI thread and BLOCKS until the user
        picks (replace / rename:<n> / cancel), mirroring _make_exists_cb.
        There is no reliable local signal that distinguishes a genuine
        hotfix/version-bump update (should merge into the existing folder)
        from an unrelated alternate file on the same Nexus mod page (should
        install separately — e.g. a Male/Female variant pair, real report),
        so this always asks rather than silently guessing either way."""

        def _cb(existing_name, new_name):
            holder = {"result": "cancel"}
            ev = threading.Event()
            self._mod_variant_choice.emit({
                "existing_name": existing_name, "new_name": new_name,
                "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_mod_variant_choice_ui(self, payload):
        """UI thread: show the Same-Mod-Page-Different-File overlay; unblock
        the worker."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.overlays.mod_exists_overlay import ModExistsOverlay

        def _done(result):
            payload["holder"]["result"] = result or "cancel"
            payload["event"].set()

        ModExistsOverlay.show_over(
            self, payload["new_name"], False, _done,
            variant_of=payload["existing_name"])

    def _make_confirm_cet_cb(self, game):
        """Return a confirm_cet() callback for run_deploy_pipeline. Runs on the
        deploy WORKER thread: if Cyberpunk 2077 is being deployed in symlink mode
        with CET staged, asks the UI thread to show the warning and BLOCKS on an
        Event until the user chooses. Returns True to proceed, False to cancel.
        No conflict → returns True immediately without prompting (Tk parity:
        gui.dialogs.confirm_cet_symlink)."""
        from Utils.modding_tools.cet_check import cet_symlink_conflict

        def _cb() -> bool:
            try:
                if not cet_symlink_conflict(game):
                    return True
            except Exception:
                return True
            holder = {"result": True}
            ev = threading.Event()
            self._confirm_cet.emit({"holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_confirm_cet_ui(self, payload):
        """UI thread: show the CET-requires-Hardlink warning; unblock the worker.
        The progress popup is cleared while the user decides (no work running)."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay

        def _done(ok):
            payload["holder"]["result"] = bool(ok)
            payload["event"].set()

        ConfirmOverlay.show_over(
            self,
            self.tr("Cyber Engine Tweaks requires Hardlink mode"),
            self.tr(
                "Cyber Engine Tweaks is enabled, but cyber_engine_tweaks.asi "
                "will be symlinked.\n\nCET will not load from a symlinked "
                "asi. This happens when the deploy mode is Symlink, or when it "
                "is Hardlink but the game folder and mod staging folder are on "
                "different drives (hardlinks fall back to symlinks across "
                "drives).\n\nUse Hardlink mode with both folders on the same "
                "drive for CET to work.\n\nDeploy anyway?"
            ),
            _done,
            confirm_label=self.tr("Deploy anyway"),
            cancel_label=self.tr("Cancel"),
            card_h=340,
        )

    def _install_next(self):
        """Pop the next queued archive: prepare it on a worker; the prepared
        result comes back on _prepared_ready (UI thread)."""
        if not self._install_queue:
            self._install_done.emit(len(self._install_ok), self._install_total,
                                    self._install_ok)
            return
        path = self._install_queue.pop(0)
        idx = self._install_total - len(self._install_queue)
        self._op_log.emit(f"Installing ({idx}/{self._install_total}): {Path(path).name}")


        meta = getattr(self, "_install_metas", {}).get(path)
        forced_name = getattr(self, "_install_preferred", {}).get(path, "")

        def worker():
            from Utils.mods.mod_install import prepare_archive
            try:
                prepared = prepare_archive(
                    path, self._install_game, self._install_profile_dir,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    prebuilt_meta=meta,
                    preferred_name=forced_name,
                    on_need_prefix=self._make_need_prefix_cb(),
                    on_variant_choice=self._make_variant_choice_cb())
            except Exception as exc:
                self._op_log.emit(f"Prepare error ({Path(path).name}): {exc}")
                prepared = None
            self._prepared_ready.emit(prepared)

        threading.Thread(target=worker, daemon=True).start()

    def _install_batch_parallel(self):
        """Phase 1 of a multi-archive install: prepare + install every
        NON-interactive archive on a small worker pool (collection-install
        style: max_extract_workers threads gated by the extraction memory
        budget). Archives that need a wizard (FOMOD with steps, BAIN) are
        deferred; _on_install_batch_stage_done then runs them one-by-one
        through the existing sequential wizard handshake. Blocking UI prompts
        (need-prefix, mod-already-exists) are serialised so only one overlay
        shows at a time."""

        paths = list(self._install_queue)
        self._install_queue = []
        total = self._install_total
        ui_lock = threading.Lock()
        raw_prefix_cb = self._make_need_prefix_cb()
        raw_exists_cb = self._make_exists_cb()
        raw_variant_cb = self._make_variant_choice_cb()

        def _serialized(cb):
            def inner(*a, **k):
                with ui_lock:
                    return cb(*a, **k)
            return inner

        prefix_cb = _serialized(raw_prefix_cb)
        exists_cb = _serialized(raw_exists_cb)
        variant_cb = _serialized(raw_variant_cb)

        def driver():
            from Utils.extract_budget import (
                ExtractionMemoryBudget,
                get_uncompressed_size,
            )
            from Utils.mods.mod_install import (
                _archive_lists_fomod_config,
                finish_install,
                prepare_archive,
            )
            try:
                from Utils.ui_config import load_collection_settings
                workers = int(load_collection_settings().get(
                    "max_extract_workers", 4))
            except Exception:
                workers = 4
            workers = max(1, min(workers, len(paths)))
            budget = ExtractionMemoryBudget(max_workers=workers)
            lock = threading.Lock()
            ok_names: list = []
            deferred: list = []
            counters = {"done": 0}

            def one(path):
                name_for_log = Path(path).name
                meta = self._install_metas.get(path)
                forced = self._install_preferred.get(path, "")
                est = 0
                acquired = False
                try:
                    self._op_log.emit(f"Installing: {name_for_log}")
                    # Archive listing already shows fomod/ModuleConfig.xml →
                    # defer NOW, skipping an extract that would be thrown away
                    # and repeated in the deferred phase (collection parity).
                    if _archive_lists_fomod_config(path):
                        self._op_log.emit(
                            f"FOMOD installer detected: {name_for_log} — "
                            "deferred to the end of the batch.")
                        with lock:
                            deferred.append(path)
                        return
                    est = get_uncompressed_size(path)
                    budget.acquire(est)
                    acquired = True
                    prepared = prepare_archive(
                        path, self._install_game, self._install_profile_dir,
                        log_fn=lambda m: self._op_log.emit(str(m)),
                        prebuilt_meta=meta, preferred_name=forced,
                        on_need_prefix=prefix_cb,
                        on_variant_choice=None if forced else variant_cb)
                    if prepared is None:
                        return
                    if (prepared.is_fomod() and prepared.fomod_has_steps()) \
                            or prepared.is_bain():
                        kind = "FOMOD" if prepared.is_fomod() else "BAIN"
                        self._op_log.emit(
                            f"{kind} installer detected: {prepared.mod_name} "
                            "— deferred to the end of the batch.")
                        prepared.cleanup()
                        with lock:
                            deferred.append(path)
                        return
                    if prepared.is_fomod():
                        # FOMOD with no install steps → nothing to choose;
                        # install headlessly (parity with _on_prepared_ready).
                        self._op_log.emit(
                            f"FOMOD has no install options: "
                            f"{prepared.mod_name} — installing required files.")
                    name = finish_install(
                        prepared, None,
                        log_fn=lambda m: self._op_log.emit(str(m)),
                        on_exists=None if forced else exists_cb)
                    if name:
                        self._maybe_clear_archive(prepared)
                        with lock:
                            ok_names.append(name)
                except Exception as exc:
                    self._op_log.emit(f"Install error ({name_for_log}): {exc}")
                finally:
                    if acquired:
                        budget.release(est)
                    with lock:
                        counters["done"] += 1
                        done = counters["done"]
                    self._op_progress.emit(done, total, "Installing")

            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(one, paths))
            self._install_batch_stage_done.emit(ok_names, deferred)

        threading.Thread(target=driver, daemon=True).start()

    def _on_install_batch_stage_done(self, names, deferred):
        """UI thread: the parallel phase of a batch install finished. Run the
        optional rename-after-install prompts for the phase-1 installs (one at
        a time, as the sequential path does), then push the deferred FOMOD/BAIN
        archives through the normal sequential queue — an empty deferred list
        goes straight to the batch summary via _install_next."""
        names = list(names or [])
        deferred = list(deferred or [])

        def _proceed():
            if deferred:
                self._op_log.emit(
                    f"Installing {len(deferred)} deferred FOMOD/BAIN mod(s)…")
            self._install_queue = deferred
            self._install_next()

        try:
            from Utils.ui_config import load_rename_mod_after_install
            rename_on = load_rename_mod_after_install()
        except Exception:
            rename_on = False
        if not rename_on:
            self._install_ok.extend(names)
            _proceed()
            return

        def _chain(i):
            if i >= len(names):
                _proceed()
                return

            def _named(final, _next=i + 1):
                self._install_ok.append(final)
                _chain(_next)

            self._maybe_prompt_rename(names[i], _named)

        _chain(0)

    def _on_prepared_ready(self, prepared):
        if prepared is None:
            self._one_install_done.emit(None)
            return
        if prepared.is_fomod() and not prepared.fomod_has_steps():
            # FOMOD with no install steps (only requiredInstallFiles /
            # conditionalFileInstalls) → nothing for the user to choose. Install
            # headlessly with default selections instead of opening the wizard
            # on an empty step list (which crashed with IndexError).
            self._op_log.emit(
                f"FOMOD has no install options: {prepared.mod_name} — "
                "installing required files.")
            self._run_finish_install(prepared, None)
        elif prepared.is_fomod() or prepared.is_bain():
            # Interactive install (FOMOD wizard / BAIN picker): the user may take
            # a long time choosing, but that wait does NO staging work. Hand the
            # prepared archive off to a DETACHED, uniquely-keyed wizard tab so
            # several can be open at once and the install pipeline is free to
            # continue with the next queued archive. The pipeline treats this
            # item as handed off (not counted here — the detached wizard reports
            # its own outcome). Staging on finish is serialised separately.
            self._open_wizard_install(prepared, from_queue=True)
            # Release the pipeline slot: this item is no longer pending. Pass the
            # handoff sentinel so _on_one_install_done just advances the queue
            # without a rename prompt or an ok-count (the wizard owns those).
            self._install_handoffs = getattr(self, "_install_handoffs", 0) + 1
            self._one_install_done.emit(_WIZARD_HANDOFF)
        else:
            self._run_finish_install(prepared, None)

    def _open_wizard_install(self, prepared, from_queue: bool):
        """Open a DETACHED FOMOD/BAIN wizard for *prepared* in its own tab.

        Unlike the old single-wizard flow, this does NOT hold the install
        pipeline: the tab is uniquely keyed (fomod_wizard_N / bain_picker_N) so
        multiple wizards coexist, and on finish the staging step runs through
        _run_staged_finish (serialised against installs/deploys — the shared
        game._active_profile_dir wrong-profile guard). Each wizard reports its
        own toast + modlist reload; there is no shared _fomod_done flag.

        *from_queue* is True when the archive came off the install queue (so its
        source archive should honour 'clear archive after install' just like the
        pipeline would); the flag is captured for the deferred staging call."""
        self._wizard_seq += 1
        seq = self._wizard_seq
        clear_archives = getattr(self, "_install_clear_archives", True)
        # Capture the Change-Version 'previous mod name' for THIS handoff (a
        # single-archive Change Version can be a FOMOD): the pipeline's shared
        # _install_prev_name is one-shot and would be cleared by later items, so
        # bind it to the wizard now. Consumed once in _on_wizard_finish_done.
        prev_name = getattr(self, "_install_prev_name", None) if from_queue else None
        if prev_name is not None:
            self._install_prev_name = None
        done = {"v": False}   # per-wizard double-fire guard (finish/cancel/close)
        # NB: the progress popup is intentionally NOT cleared here (unlike the old
        # blocking flow) — the install pipeline keeps running behind this detached
        # tab, so the popup still reflects that real ongoing work and clears itself
        # when the pipeline finishes.

        if prepared.is_fomod():
            tab_key = f"fomod_wizard_{seq}"
            from gui_qt.views.fomod_wizard_view import FomodWizardView

            def _finish(selections):
                if done["v"]:
                    return
                done["v"] = True
                self._tabs.close_tab(tab_key)
                self._stage_wizard_finish(prepared, selections, None,
                                          clear_archives, prev_name)

            def _cancel():
                if done["v"]:
                    return
                done["v"] = True
                self._tabs.close_tab(tab_key)
                self._op_log.emit(f"FOMOD install cancelled: {prepared.mod_name}")
                prepared.cleanup()
                self._notify(self.tr("Install cancelled: {0}").format(prepared.mod_name), "info")

            _sel_path = None
            _game_name = getattr(prepared.game, "name", "")
            if _game_name:
                from Utils.config_paths import get_fomod_selections_path
                _sel_path = get_fomod_selections_path(_game_name,
                                                      prepared.mod_name)
            _prof_sel_path = self._profile_fomod_selection_path(
                getattr(prepared, "profile_dir", None), prepared.mod_name)
            _inst, _act, _loose = prepared.fomod_context
            # prepared.fomod_context was read from plugins.txt/loadorder.txt on the
            # worker thread. Those files can lag the LIVE plugin panel: a mod
            # enabled in the model isn't persisted to plugins.txt until the next
            # save (e.g. Sort Plugins), so a FOMOD `state="Active"` dep wouldn't
            # match and its conditional option wouldn't auto-select until the user
            # sorted. Merge the panel's live enabled/all sets so the wizard sees
            # the true current state without needing a sort first.
            try:
                pm = getattr(self, "_plugin_model", None)
                if pm is not None:
                    live_all = pm.all_lower()
                    live_active = pm.enabled_lower()
                    _inst = set(_inst) | live_all
                    # A plugin present in the panel but not enabled is genuinely
                    # inactive — only add the live-enabled ones to active, and drop
                    # any stale disk-active entry the panel now shows as disabled.
                    _act = (set(_act) | live_active) - (live_all - live_active)
            except Exception:
                pass
            self._append_log(
                f"[fomod] {prepared.mod_name}: {len(_inst)} installed / "
                f"{len(_act)} active plugin(s) for condition eval")
            view = FomodWizardView(prepared.fomod_config, prepared.fomod_base,
                                   prepared.mod_name, on_finish=_finish,
                                   on_cancel=_cancel,
                                   saved_selections=prepared.saved_fomod_selections,
                                   selections_path=_sel_path,
                                   profile_selections_path=_prof_sel_path,
                                   installed_files=_inst, active_files=_act,
                                   loose_files=_loose)
            # Closing the tab (× / detached-window close) cancels the install.
            view.destroyed.connect(lambda *_: _cancel())
            self._tabs.open_tab(view, self.tr("Install: {0}").format(prepared.mod_name),
                                key=tab_key)
        else:   # BAIN
            tab_key = f"bain_picker_{seq}"
            from gui_qt.views.bain_picker_view import BainPickerView

            def _bfinish(result):
                if done["v"]:
                    return
                done["v"] = True
                self._tabs.close_tab(tab_key)
                if result is None:
                    self._op_log.emit(
                        f"BAIN install cancelled: {prepared.mod_name}")
                    prepared.cleanup()
                    self._notify(self.tr("Install cancelled: {0}").format(prepared.mod_name), "info")
                    return
                self._stage_wizard_finish(prepared, None, result,
                                          clear_archives, prev_name)

            view = BainPickerView(
                prepared.bain_subpkgs, prepared.bain_root, prepared.mod_name,
                on_done=_bfinish,
                readme_text=getattr(prepared, "readme_text", None),
                saved_selections=getattr(prepared, "saved_bain_selections", None))
            # Closing the tab (× / detached-window close) cancels the install.
            view.destroyed.connect(lambda *_: _bfinish(None))
            self._tabs.open_tab(view, self.tr("Install: {0}").format(prepared.mod_name),
                                key=tab_key)

    def _run_finish_install(self, prepared, selections, bain_selections=None):

        # Quick Update (forced folder name) auto-confirms Replace-All — pass
        # on_exists=None so finish_install silently replaces the existing folder
        # instead of raising the Mod-Already-Exists overlay (Tk parity).
        _forced = str(getattr(prepared, "archive", "")) in \
            getattr(self, "_install_preferred", {})
        exists_cb = None if _forced else self._make_exists_cb()

        def worker():
            from Utils.mods.mod_install import finish_install
            try:
                name = finish_install(
                    prepared, selections,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    on_exists=exists_cb,
                    bain_selections=bain_selections)
            except Exception as exc:
                self._op_log.emit(f"Install error ({prepared.mod_name}): {exc}")
                name = None
            if name:
                self._maybe_clear_archive(prepared)
            self._one_install_done.emit(name)

        threading.Thread(target=worker, daemon=True).start()

    # -- detached-wizard staging --------------------------------------------
    def _stage_wizard_finish(self, prepared, selections, bain_selections,
                             clear_archives, prev_name=None):
        """A detached FOMOD/BAIN wizard finished — enqueue its staging step.

        Staging (finish_install) mutates the shared game (get_effective_mod_
        staging_path reads the live active profile), so it MUST run one-at-a-time
        against installs AND deploys — the same wrong-profile guard _install_paths
        enforces. Wizards may finish in any order and while a pipeline install or
        a deploy is running, so we queue here and drain via _run_staged_finish.

        *prev_name* — a Change-Version 'previous mod name' captured for this
        wizard; if the install lands a differently-named mod, the remove-previous
        prompt fires from _on_wizard_finish_done."""
        self._staged_finish_queue.append({
            "prepared": prepared, "selections": selections,
            "bain_selections": bain_selections,
            "clear_archives": clear_archives, "prev_name": prev_name})
        self._notify(self.tr("Installing {0}…").format(prepared.mod_name), "info")
        self._run_staged_finish()

    def _run_staged_finish(self):
        """Drain one queued detached-wizard staging job if nothing else is
        touching the shared game. Re-armed by _on_wizard_finish_done, and also by
        _on_install_done / _on_op_done when a blocking install/deploy finishes."""

        if not self._staged_finish_queue \
                or self._staged_finish_running \
                or getattr(self, "_install_running", False) \
                or getattr(self, "_deploy_running", False) \
                or getattr(self, "_col_install_running", False):
            return
        job = self._staged_finish_queue.pop(0)
        self._staged_finish_running = True
        prepared = job["prepared"]
        selections = job["selections"]
        bain_selections = job["bain_selections"]
        prev_name = job.get("prev_name")
        # Carry the clear-archive policy captured when this wizard opened (the
        # pipeline's _install_clear_archives may have changed for a later batch).
        self._install_clear_archives = job["clear_archives"]
        # A just-finished worker may have left _active_profile_dir out of sync;
        # re-assert it so get_effective_mod_staging_path resolves to the profile
        # the dropdown shows (parity with _install_paths).
        self._gs.reassert_active_profile()
        self._ensure_feedback()

        _forced = str(getattr(prepared, "archive", "")) in \
            getattr(self, "_install_preferred", {})
        exists_cb = None if _forced else self._make_exists_cb()

        def worker():
            from Utils.mods.mod_install import finish_install
            try:
                name = finish_install(
                    prepared, selections,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    on_exists=exists_cb,
                    bain_selections=bain_selections)
            except Exception as exc:
                self._op_log.emit(f"Install error ({prepared.mod_name}): {exc}")
                name = None
            if name:
                self._maybe_clear_archive(prepared)
            self._wizard_finish_done.emit(name, prepared, prev_name)

        threading.Thread(target=worker, daemon=True).start()

    def _drain_pending_after_staged(self):
        """Release Deploy/Restore requests deferred behind an install, a
        collection install, or a detached-wizard staging job (all three
        mutate the shared game's active-profile pointer — see
        _install_paths for the full race explanation). Only safe once ALL
        of them are confirmed idle; any one still running holds the shared
        game object, and releasing early would re-open the exact
        wrong-profile race this queue exists to prevent. Called from every
        handler that clears one of those flags.

        _pending_install_batches also gates this: _on_install_done clears
        _install_running and calls here SYNCHRONOUSLY, but only DEFERS the
        next queued batch to the next event-loop tick (QTimer.singleShot).
        Without this check, a Deploy queued behind several near-simultaneous
        mod updates (e.g. updating multiple outdated mods) would release
        right after the FIRST update finishes — capturing on-disk state
        before the rest have installed — instead of waiting for the whole
        queue to drain."""
        if (getattr(self, "_install_running", False)
                or getattr(self, "_deploy_running", False)
                or getattr(self, "_staged_finish_running", False)
                or self._staged_finish_queue
                or getattr(self, "_col_install_running", False)
                or self._pending_install_batches):
            return
        pending, self._pending_after_staged = self._pending_after_staged, []
        for cb in pending:
            try:
                cb()
            except Exception as exc:
                self._append_log(f"[install] deferred action error: {exc}")

    def _on_wizard_finish_done(self, name, prepared, prev_name):
        """UI thread: a detached-wizard staging job finished. Report it (toast /
        optional rename prompt / remove-previous), reload, then drain the next
        queued wizard job."""
        self._staged_finish_running = False
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()

        def _drain_next():
            # Another wizard may have finished while this one staged, and an
            # install/deploy may have queued behind the staging lock.
            self._run_staged_finish()
            # Only release the deferred install/deploy/restore queues once the
            # staged-finish queue is FULLY drained AND idle — a still-running or
            # still-queued staging job holds the shared game, so releasing now
            # would re-open the wrong-profile race we just guarded.
            if not self._staged_finish_running and not self._staged_finish_queue:
                if self._pending_install_batches:
                    QTimer.singleShot(0, self._drain_pending_installs)
                self._drain_pending_after_staged()

        def _after_named(final):
            self._reload_modlist()
            # Plugins reload comes from _on_conflicts_ready after the rebuild —
            # an immediate reload prunes the fresh plugins.txt entry against the
            # stale filemap (see _on_install_done).
            if not getattr(self, "_reload_had_entries", False):
                self._reload_plugins()
            self._notify(self.tr("Installed {0}").format(final), "success")
            # Change Version landed a different-named version → offer to remove
            # the previous version (Tk parity with _finish_one_install).
            if prev_name and final != prev_name:
                self._maybe_prompt_remove_previous(prev_name, final)
            _drain_next()

        if name:
            self._maybe_prompt_rename(name, _after_named)
        else:
            self._reload_modlist()
            # See _after_named — only load directly when no rebuild is coming.
            if not getattr(self, "_reload_had_entries", False):
                self._reload_plugins()
            # Failed/cancelled staging (finish_install returned None): keep the
            # queue moving.
            _drain_next()

    def _maybe_clear_archive(self, prepared):
        """Delete the source archive after a successful install, honouring the
        'Clear archive after install' / 'Keep FOMOD archives' settings (Tk
        parity). Runs on the install worker thread; failures are non-fatal.
        Skipped for user-supplied archives (see _install_paths clear_archives)."""
        try:
            if not getattr(self, "_install_clear_archives", True):
                return
            from Utils.ui_config import (
                load_clear_archive_after_install,
                load_keep_fomod_archives,
            )
            if not load_clear_archive_after_install():
                return
            if prepared.is_fomod() and load_keep_fomod_archives():
                return
            archive = getattr(prepared, "archive", None)
            if archive is None:
                return
            from pathlib import Path as _P

            from Nexus.nexus_download import delete_archive_and_sidecar
            delete_archive_and_sidecar(_P(archive))
            self._op_log.emit(f"Removed archive: {_P(archive).name}")
        except Exception as exc:
            self._op_log.emit(f"Archive cleanup skipped: {exc}")

    def _on_one_install_done(self, name):
        if name is _WIZARD_HANDOFF:
            # Interactive archive handed off to a detached wizard tab — it owns
            # its own outcome (toast/rename/reload on finish). Just advance the
            # queue so the next archive installs while the wizard sits open.
            self._install_next()
            return
        if name:
            # Optional post-install rename prompt (Tk parity). Modal, before the
            # next queued install — keeps one dialog at a time.
            self._maybe_prompt_rename(name, self._finish_one_install)
        else:
            self._install_next()   # continue the queue

    def _finish_one_install(self, name: str):
        """Tail of _on_one_install_done, run after the optional rename prompt
        resolves (*name* is the final mod name)."""
        self._install_ok.append(name)
        # Change Version landed a different-named version → offer to remove
        # the previous version (Tk parity). One-shot per queue.
        prev = getattr(self, "_install_prev_name", None)
        if prev and name != prev:
            self._install_prev_name = None   # don't re-prompt for later items
            self._maybe_prompt_remove_previous(prev, name)
        self._install_next()   # continue the queue

    @staticmethod
    def _resolve_staging_dir_name(staging: Path, name: str) -> str | None:
        """Resolve *name* to an actual child directory of *staging*.

        Tries an exact match first, then falls back to a case-insensitive
        match. A modlist.txt entry (e.g. one carried over from a Mod
        Organizer 2 profile transferred from a case-insensitive Windows
        filesystem onto a case-sensitive Linux one) can differ in case from
        the real on-disk folder — an exact ``.is_dir()`` check alone would
        then silently fail to find a folder that does, in fact, exist.
        Returns the real on-disk name (which may differ in case from
        *name*), or None if no matching directory exists at all.
        """
        if (staging / name).is_dir():
            return name
        try:
            lname = name.casefold()
            for child in staging.iterdir():
                if child.is_dir() and child.name.casefold() == lname:
                    return child.name
        except OSError:
            pass
        return None

    def _maybe_prompt_remove_previous(self, old_name: str, new_name: str):
        """Show the borderless 'Remove previous version?' overlay if both the old
        and new mods exist. Remove → the new mod inherits the old one's modlist
        position + enabled state, then the old mod is removed.

        *old_name*/*new_name* are the modlist.txt entry names — used as-is for
        all modlist.txt reads/writes below. The on-disk existence check (and
        the folder actually deleted on "Remove") instead resolves through
        _resolve_staging_dir_name, so a case-only mismatch between the
        recorded name and the real folder (see its docstring) doesn't
        silently skip this prompt with no explanation.
        """
        staging = self._gs.staging_dir()
        if staging is None:
            return
        old_disk_name = self._resolve_staging_dir_name(staging, old_name)
        new_disk_name = self._resolve_staging_dir_name(staging, new_name)
        if old_disk_name is None or new_disk_name is None:
            self._append_log(
                "[install] remove-previous prompt skipped — "
                f"{'old' if old_disk_name is None else 'new'} mod folder "
                f"not found on disk ('{old_name if old_disk_name is None else new_name}').")
            return
        from gui_qt.overlays.remove_previous_overlay import RemovePreviousOverlay

        def _done(result):
            if result == "remove":
                self._remove_previous_version(old_name, new_name, old_disk_name)

        RemovePreviousOverlay.show_over(self, old_name, new_name, _done)

    def _remove_previous_version(self, old_name: str, new_name: str,
                                 old_disk_name: str | None = None):
        """New mod inherits old's modlist slot + enabled state; old is removed.

        *old_disk_name* is the real on-disk folder name for *old_name* (may
        differ only in case — see _resolve_staging_dir_name); falls back to
        *old_name* itself when not supplied, matching prior behaviour."""
        try:
            from Utils.mods.mod_remove import remove_mods
            from Utils.mods.modlist import read_modlist, write_modlist
            pdir = self._gs.profile_dir()
            game = self._gs.game
            if pdir is None or game is None:
                return
            ml = pdir / "modlist.txt"
            entries = read_modlist(ml)
            old_e = next((e for e in entries if e.name == old_name), None)
            new_e = next((e for e in entries if e.name == new_name), None)
            if new_e is not None:
                # New inherits old's enabled state + position, and the OLD entry
                # is dropped here (remove_mods intentionally does NOT touch
                # modlist.txt — it leaves the row for the caller to remove).
                if old_e is not None:
                    new_e.enabled = old_e.enabled
                # Pull the new entry out first, THEN locate old's slot in the
                # remaining list, drop old, and insert new there (so new lands
                # exactly where old was).
                rest = [e for e in entries if e.name != new_name]
                idx = next((i for i, e in enumerate(rest)
                            if e.name == old_name), 0)
                rest = [e for e in rest if e.name != old_name]
                rest.insert(min(idx, len(rest)), new_e)
                write_modlist(ml, rest)
            # Delete the old mod's files/plugins/index (NOT its modlist row —
            # already dropped above). Use the real on-disk folder name so a
            # case-only mismatch against the modlist.txt string doesn't leave
            # the actual folder behind undeleted.
            remove_mods(game, pdir, [old_disk_name or old_name],
                        log_fn=lambda m: self._append_log(f"[remove] {m}"))
        except Exception as exc:
            self._append_log(f"[install] remove-previous failed: {exc}")
        self._reload_modlist()
        self._rebuild_conflicts_async()

    def _maybe_prompt_rename(self, name: str, on_done):
        """If 'Rename mod after install' is on, prompt for a new name and rename
        the mod (staging folder + index + modlist entry). Calls ``on_done`` with
        the final name (unchanged if the user cancels or the rename fails)."""
        try:
            from Utils.ui_config import load_rename_mod_after_install
            if not load_rename_mod_after_install():
                on_done(name)
                return
        except Exception:
            on_done(name)
            return

        def _named(new):
            if new is None or not new.strip():
                on_done(name)
                return
            renamed = self._rename_mod_on_disk(name, new.strip())
            on_done(renamed or name)

        from gui_qt.overlays.text_input_overlay import TextInputOverlay
        extra_kw = {}
        overlay_holder: list = []
        if self._mod_has_nexus_id(name):
            def _do_fetch(_name=name):
                if overlay_holder:
                    self._fetch_nexus_name(_name, overlay_holder[0])
            extra_kw["extra_label"] = self.tr("Fetch name from Nexus")
            extra_kw["on_extra"] = _do_fetch
        overlay = TextInputOverlay.show_over(
            self, "Rename mod", "New name for the installed mod:", _named,
            initial=name, ok_label=self.tr("Rename"), **extra_kw)
        overlay_holder.append(overlay)

    def _rename_mod_on_disk(self, old_name: str, new_name: str) -> str | None:
        """Rename a mod: staging folder → new, modindex entry, modlist entry,
        then reload. Returns the sanitised new name on success, else None.
        Mirrors the Tk modlist_panel.rename_mod_by_name operation.

        If the target name is already taken, this shows the Mod-Already-Exists
        overlay (Replace All / Rename… / Cancel) rather than failing outright,
        and the rename completes asynchronously through that flow (so the return
        value is None — the folder is still under ``old_name`` at that point)."""
        from Utils.mods.mod_name_utils import sanitize_mod_folder_name
        new_name = sanitize_mod_folder_name(new_name)
        if not old_name or not new_name or old_name == new_name:
            return None
        staging = self._gs.staging_dir()
        if staging is None:
            return None
        new_folder = staging / new_name
        if new_folder.exists():
            # Collision → let the user Replace the existing mod, pick another
            # name, or cancel (same overlay the installer uses).
            from gui_qt.overlays.mod_exists_overlay import ModExistsOverlay

            def _resolved(result):
                if not result or result == "cancel":
                    return
                if result == "replace":
                    self._replace_then_rename(old_name, new_name)
                elif result.startswith("rename:"):
                    self._rename_mod_on_disk(old_name, result[len("rename:"):])

            ModExistsOverlay.show_over(self, new_name, False, _resolved)
            return None
        return self._do_rename_mod_on_disk(old_name, new_name)

    def _replace_then_rename(self, old_name: str, new_name: str) -> None:
        """Fully remove the existing mod occupying *new_name*, drop its modlist
        row, then rename *old_name* → *new_name* (used by the rename-collision
        Replace-All path)."""
        try:
            from Utils.mods.mod_remove import remove_mods
            remove_mods(self._gs.game, self._gs.profile_dir(), [new_name],
                        log_fn=self._append_log)
        except Exception as exc:
            self._notify(self.tr("Replace failed: {0}").format(exc), "warning")
            return
        # Drop the replaced mod's modlist row so it isn't left dangling. File-
        # level, not self._modlist_model — same stale-snapshot hazard as
        # _do_rename_mod_on_disk below (this whole flow runs before the
        # post-install reload for a fresh install), and remove_row's default
        # save=True would silently re-persist that stale state.
        try:
            from Utils.mods.modlist import _lock_for, read_modlist, write_modlist
            ml_path = self._gs.modlist_path()
            if ml_path is not None:
                with _lock_for(ml_path):
                    entries = read_modlist(ml_path)
                    entries = [e for e in entries
                              if e.is_separator or e.name != new_name]
                    write_modlist(ml_path, entries)
        except Exception as exc:
            print(f"[gui_qt] replace-then-rename modlist update failed: {exc}",
                  flush=True)
        self._do_rename_mod_on_disk(old_name, new_name)

    def _do_rename_mod_on_disk(self, old_name: str, new_name: str) -> str | None:
        """Perform the actual rename (staging folder + index + state + modlist),
        assuming *new_name* is free. Returns the new name on success."""
        staging = self._gs.staging_dir()
        if staging is None:
            return None
        old_folder = staging / old_name
        new_folder = staging / new_name
        try:
            if old_folder.is_dir():
                old_folder.rename(new_folder)
        except OSError as exc:
            self._notify(self.tr("Rename failed: {0}").format(exc), "warning")
            return None
        # Keep the persistent mod index in sync (avoids a full rescan).
        try:
            from Utils.filemap import rename_in_mod_index
            from Utils.ui_config import load_normalize_folder_case
            idx_path = staging.parent / "modindex.bin"
            rename_in_mod_index(idx_path, old_name, new_name,
                                normalize_folder_case=load_normalize_folder_case())
        except Exception:
            pass
        # Re-key the name-keyed per-mod state (strip prefixes, disabled plugins,
        # excluded files, notes) — Tk parity: _migrate_mod_name_state.
        try:
            from Utils.mods.mod_rename import migrate_mod_state
            migrate_mod_state(self._gs.profile_dir(), old_name, new_name,
                              log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] rename state migration failed: {exc}", flush=True)
        # Update the modlist entry by name, persist, and reload everything.
        # Read-modify-write the FILE directly rather than renaming the row in
        # self._modlist_model and calling m.save() — the rename prompt (this
        # whole flow) runs BEFORE _on_install_done's _reload_modlist() for a
        # single install (_finish_one_install, which the prompt gates, is what
        # continues the queue into _install_done), so the model is still
        # showing its PRE-install snapshot at this point. Renaming a row that
        # isn't there yet was a silent no-op, and m.save() then overwrote the
        # freshly-written modlist.txt with that stale snapshot — deleting the
        # just-installed mod's entry outright (root cause of mods vanishing
        # until a manual Refresh, but only when using this rename prompt).
        try:
            from Utils.mods.modlist import _lock_for, read_modlist, write_modlist
            ml_path = self._gs.modlist_path()
            if ml_path is not None:
                with _lock_for(ml_path):
                    entries = read_modlist(ml_path)
                    for e in entries:
                        if not e.is_separator and e.name == old_name:
                            e.name = new_name
                            break
                    write_modlist(ml_path, entries)
        except Exception as exc:
            print(f"[gui_qt] rename modlist update failed: {exc}", flush=True)
        self._reload_modlist()
        self._notify(self.tr("Renamed to '{0}'.").format(new_name), "info")
        return new_name

    def _on_install_done(self, ok: int, total: int, names):
        self._install_running = False
        # Drain any batch queued while this one ran (deferred so it starts
        # after this handler — and any on_all_done callback — completes).
        if self._pending_install_batches:
            QTimer.singleShot(0, self._drain_pending_installs)
        # A detached-wizard staging job may have finished (or been requested)
        # while this pipeline held the shared game — let it run now.
        if self._staged_finish_queue:
            QTimer.singleShot(0, self._run_staged_finish)
        # Release any Deploy/Restore deferred behind this install (no-op if
        # a collection install or staged-finish job is still holding the
        # shared game — see _drain_pending_after_staged).
        self._drain_pending_after_staged()
        if hasattr(self, "_install_btn"):
            self._install_btn.setEnabled(True)
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        self._reload_modlist()
        # NOTE: the plugin panel is reloaded from _on_conflicts_ready, after the
        # conflict/filemap rebuild queued by _reload_modlist — NOT here. An
        # immediate reload reads the STALE filemap (the just-installed mod's
        # plugin isn't in it yet), so load_plugins' phantom prune deleted the
        # plugins.txt entry _add_plugins had just appended; the plugin then
        # appeared in the panel only via filemap recovery, never persisted
        # (observed: SkyUI_SE.esp pruned within the same second as its install).
        # Only load directly when the modlist is empty and no rebuild is coming.
        if not getattr(self, "_reload_had_entries", False):
            self._reload_plugins()
        # Re-flag Reinstall in the Downloads tab now that meta.ini changed.
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        # A batch owner (Quick Update) reports its own summary; skip the generic
        # install toast and hand control back to it.
        cb = getattr(self, "_install_all_done_cb", None)
        self._install_all_done_cb = None
        if cb is not None:
            cb(ok, total, names)
            return
        # Archives handed off to a detached wizard aren't done yet — they report
        # their own toast on finish, so drop them from this batch's tally.
        handoffs = getattr(self, "_install_handoffs", 0)
        total = max(0, total - handoffs)
        if total == 0:
            # Nothing installed synchronously; every archive opened a wizard (or
            # there was nothing to summarise). Let the wizards speak for
            # themselves — no batch toast.
            return
        if ok == total and ok > 0:
            if ok == 1:
                self._notify(self.tr("Installed {0}").format(names[0]), "success")
            else:
                self._notify(self.tr("Installed {0} mods").format(ok), "success")
        elif ok > 0:
            self._notify(self.tr("Installed {0} of {1} mods — see log for failures.").format(ok, total),
                         "warning")
        else:
            self._notify(self.tr("Install failed — see log."), "error")

    def _drain_pending_installs(self):
        """Start the next install batch queued while an earlier install OR
        deploy OR a detached-wizard staging job ran. If a callback/coalesced
        deploy already started work in the meantime, leave the queue for the next
        _on_install_done / _on_op_done / _on_wizard_finish_done to drain (all
        call here)."""
        if not self._pending_install_batches \
                or getattr(self, "_install_running", False) \
                or getattr(self, "_deploy_running", False) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            return
        b = self._pending_install_batches.pop(0)
        self._install_paths(b["paths"], metas=b["metas"],
                            previous_mod_name=b["previous_mod_name"],
                            preferred_names=b["preferred_names"],
                            on_all_done=b["on_all_done"],
                            clear_archives=b.get("clear_archives", True))

    def _build_modlist(self) -> QWidget:
        self._modlist_model = ModListModel([])
        self._modlist_model.enabled_changed.connect(self._on_mods_enabled_changed)
        self._modlist_model.save_failed.connect(
            lambda msg: self._notify(msg, "error"))
        self._modlist_view = ModListView(self._modlist_model)
        # Search/filter hidden sets are row-indexed, so any structural change
        # (reorder/insert/remove) leaves them misaligned and must be recomputed
        # from the current entries — the view's own handler only re-applies the
        # STALE indices, so search would skip/mis-show rows until a full reload.
        # Connected AFTER the view so its cache-drop + re-span run first.
        # modelReset is handled by _reload_modlist's explicit reapply.
        for sig in (self._modlist_model.layoutChanged,
                    self._modlist_model.rowsMoved,
                    self._modlist_model.rowsInserted,
                    self._modlist_model.rowsRemoved):
            sig.connect(self._on_modlist_layout_changed)
        return self._modlist_view

    def _on_modlist_layout_changed(self, *_a):
        self._apply_modlist_search()
        self._apply_modlist_filters()

    def _on_mods_enabled_changed(self, changes):
        """Mods were toggled (checkbox / Enable-Disable all / context menu) —
        mirror the change into plugins.txt + loadorder.txt (Tk parity:
        _sync_plugins_for_toggle) and refresh the Plugins tab."""
        from Utils.plugins.plugin_sync import sync_plugins_for_mods
        try:
            wrote = sync_plugins_for_mods(
                self._gs.game, self._gs.profile_dir(), self._gs.staging_dir(),
                list(changes or []), log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] plugin sync failed: {exc}", flush=True)
            return
        # NOTE: the plugin panel is reloaded from _on_conflicts_ready (after the
        # filemap rebuild that save()→on_saved() kicked off finishes), NOT here.
        # Reloading now would read a STALE filemap — the rebuild is still in
        # flight — so a just-enabled patcher mod's light copies wouldn't be the
        # resolved winners yet (ESL flags missing), and a just-disabled mod's
        # plugins couldn't be recovered from the fresh filemap. Tk parity:
        # gui.py _on_filemap_rebuilt refreshes the plugins tab after the rebuild.
        _ = wrote

    def _build_modlist_area(self) -> QWidget:
        """Modlist column: the list + its tool footer (buttons + search) stacked
        vertically, with a collapsible filter side panel docked on the left (Tk
        parity — the filter panel pushes the column right, not an overlay)."""
        # The list + footer share one vertical column. A hidden 'new profile'
        # bar sits at the very top (row 0, Tk parity) and appears when the user
        # picks 'Add new profile…'.
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        from gui_qt.new_profile_bar import NewProfileBar
        self._new_profile_bar = NewProfileBar(
            on_create=self._on_new_profile_create,
            on_cancel=lambda: None)
        cv.addWidget(self._new_profile_bar)
        cv.addWidget(self._build_modlist(), 1)
        cv.addWidget(self._modlist_footer())

        area = QWidget()
        h = QHBoxLayout(area)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self._modlist_filter_panel = self._build_modlist_filter_panel()
        self._modlist_filter_panel.setVisible(False)
        h.addWidget(self._modlist_filter_panel)
        # The Mod Files filter panel shares this window-left slot so it opens in
        # the same place as the modlist filters (just a different filter set).
        self._mod_files_filter_panel = self._build_mod_files_filter_panel()
        self._mod_files_filter_panel.setVisible(False)
        h.addWidget(self._mod_files_filter_panel)
        # The Data filter panel shares the same window-left slot.
        self._data_filter_panel = self._build_data_filter_panel()
        self._data_filter_panel.setVisible(False)
        h.addWidget(self._data_filter_panel)
        # The Downloads filter panel shares the slot too.
        self._downloads_filter_panel = self._build_downloads_filter_panel()
        self._downloads_filter_panel.setVisible(False)
        h.addWidget(self._downloads_filter_panel)
        # The Text Files filter panel shares the slot too.
        self._text_files_filter_panel = self._build_text_files_filter_panel()
        self._text_files_filter_panel.setVisible(False)
        h.addWidget(self._text_files_filter_panel)
        # The Plugins filter panel shares the slot too.
        self._plugin_filter_panel = self._build_plugin_filter_panel()
        self._plugin_filter_panel.setVisible(False)
        h.addWidget(self._plugin_filter_panel)
        h.addWidget(col, 1)
        return area

    def _build_text_files_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(self._text_files_view.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_text_files_filter_changed)
        panel.close_requested.connect(self._toggle_text_files_filters)
        self._text_files_view.filetypes_changed.connect(
            self._sync_text_files_filter_list)
        return panel

    def _toggle_text_files_filters(self):
        panel = self._text_files_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_text_files_filter_list()
        else:
            panel.setVisible(False)

    def _on_text_files_filter_changed(self, state: dict):
        self._text_files_view.apply_filter_state(state)
        active = self._text_files_filter_panel.any_active()
        b = getattr(self, "_tf_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_text_files_filter_list(self):
        if not self._text_files_filter_panel.isVisible():
            return
        self._text_files_filter_panel.set_dynamic_items(
            "filetypes", self._text_files_view.filetype_items())

    def _build_downloads_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(self._downloads_view.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_downloads_filter_changed)
        panel.close_requested.connect(self._toggle_downloads_filters)
        self._downloads_view.filetypes_changed.connect(
            self._sync_downloads_filter_list)
        return panel

    def _toggle_downloads_filters(self):
        panel = self._downloads_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_downloads_filter_list()
        else:
            panel.setVisible(False)

    def _on_downloads_filter_changed(self, state: dict):
        self._downloads_view.apply_filter_state(state)
        active = self._downloads_filter_panel.any_active()
        b = getattr(self, "_dl_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_downloads_filter_list(self):
        if not self._downloads_filter_panel.isVisible():
            return
        self._downloads_filter_panel.set_dynamic_items(
            "filetypes", self._downloads_view.filetype_items())
        self._downloads_filter_panel.set_dynamic_items(
            "locations", self._downloads_view.location_items())

    def _build_data_filter_panel(self):
        from gui_qt.data_tab.data_view import DataView
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(DataView.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_data_filter_changed)
        panel.close_requested.connect(self._toggle_data_filters)
        self._data_view.filetypes_changed.connect(self._sync_data_filter_list)
        return panel

    def _toggle_data_filters(self):
        """Open/close the Data filter panel in the window-left slot (does NOT hide
        the plugins column — the Data tree lives there)."""
        panel = self._data_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_data_filter_list()
        else:
            panel.setVisible(False)

    def _on_data_filter_changed(self, state: dict):
        self._data_view.apply_filter_state(state)
        active = self._data_filter_panel.any_active()
        b = getattr(self, "_data_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_data_filter_list(self):
        if not self._data_filter_panel.isVisible():
            return
        self._data_filter_panel.set_dynamic_items(
            "filetypes", self._data_view.filetype_items())

    def _build_mod_files_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.mod_files.mod_files_view import ModFilesView
        panel = FilterSidePanel(ModFilesView.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_mod_files_filter_changed)
        panel.close_requested.connect(self._toggle_mod_files_filters)
        # Keep the panel's file-type list + Pack/Unpack enablement in sync.
        self._mod_files_view.filetypes_changed.connect(
            self._sync_mod_files_filter_list)
        self._mod_files_view.mod_changed.connect(
            lambda _n: self._update_mf_footer_buttons())
        return panel

    def _toggle_mod_files_filters(self):
        """Open/close the Mod Files filter panel in the window-left slot. Unlike
        the modlist filter, this does NOT hide the plugins column — the Mod Files
        tree lives there, so hiding it would hide what you're filtering."""
        panel = self._mod_files_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)  # share the slot
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_mod_files_filter_list()
        else:
            panel.setVisible(False)

    def _on_mod_files_filter_changed(self, state: dict):
        self._mod_files_view.apply_filter_state(state)
        active = self._mod_files_filter_panel.any_active()
        b = getattr(self, "_mf_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_mod_files_filter_list(self):
        if not self._mod_files_filter_panel.isVisible():
            return
        self._mod_files_filter_panel.set_dynamic_items(
            "filetypes", self._mod_files_view.filetype_items())

    def _update_mf_footer_buttons(self):
        import Utils.archives.bsa_pack_ops as ops
        pack_btn = getattr(self, "_mf_pack_btn", None)
        unpack_btn = getattr(self, "_mf_unpack_btn", None)
        if pack_btn is None or unpack_btn is None:
            return
        mv = self._mod_files_view
        kind = ops.archive_kind_for_game(getattr(mv, "game", None))
        # Hide both buttons entirely on games we can't pack for (Tk parity).
        if kind is None:
            pack_btn.setVisible(False)
            unpack_btn.setVisible(False)
            return
        upper = kind.upper()
        pack_btn.setVisible(True)
        unpack_btn.setVisible(True)
        pack_btn.setText(self.tr("Pack {0}").format(upper))
        unpack_btn.setText(self.tr("Unpack {0}").format(upper))
        # Pack: any normal mod. Unpack: also needs a matching archive on disk.
        is_normal = mv.has_mod() and ops.is_packable_mod(getattr(mv, "_mod_name", None))
        pack_btn.setEnabled(is_normal)
        has_archive = False
        if is_normal:
            mod_dir = self._bsa_mod_dir()
            has_archive = mod_dir is not None and ops.mod_has_archive(mod_dir, kind)
        unpack_btn.setEnabled(has_archive)

    def _on_mf_expand_clicked(self):
        expanded = self._mod_files_view._toggle_expand_all()
        self._mf_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                    else self.tr("⊞ Expand all"))

    def _build_modlist_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.modlist.modlist_filter import STATUS_FILTERS

        # Filters whose backing data the Qt side doesn't build yet — shown but
        # disabled (greyed) so the panel is complete and they light up later.
        # (FOMOD/BAIN come from meta.is_fomod/is_bain; conflicts, plugins, BSA,
        #  PBR, updates, categories, file types are all wired.)
        _UNWIRED = set()
        items = [(key, label, key not in _UNWIRED)
                 for key, label in STATUS_FILTERS]
        spec = [
            {"title": "By status", "type": "checks", "items": items},
            {"title": "By category", "type": "dynamic", "id": "categories"},
            {"title": "By file type", "type": "dynamic", "id": "filetypes"},
            {"title": "By author", "type": "dynamic", "id": "authors"},
        ]
        panel = FilterSidePanel(spec, title=self.tr("Filters"))
        panel.changed.connect(self._on_modlist_filter_changed)
        panel.close_requested.connect(self._toggle_modlist_filters)
        self._modlist_filter_state: dict = {}
        self._modlist_filter_data = None
        return panel

    def _toggle_modlist_filters(self):
        """Show/hide the modlist filter side panel (the Filters footer button).

        Mirrors Tk: opening the modlist filter auto-hides the plugins panel so
        the filter takes its space; closing restores the plugins panel only if
        it was visible when we opened."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        show = not panel.isVisible()
        right = getattr(self, "_right_col", None)
        if show:
            # The Mod Files / Data filters share this slot — close them first.
            mfp = getattr(self, "_mod_files_filter_panel", None)
            if mfp is not None and mfp.isVisible():
                self._toggle_mod_files_filters()
            dfp = getattr(self, "_data_filter_panel", None)
            if dfp is not None and dfp.isVisible():
                self._toggle_data_filters()
            dlfp = getattr(self, "_downloads_filter_panel", None)
            if dlfp is not None and dlfp.isVisible():
                self._toggle_downloads_filters()
            tffp = getattr(self, "_text_files_filter_panel", None)
            if tffp is not None and tffp.isVisible():
                self._toggle_text_files_filters()
            self._filter_plugins_was_visible = bool(
                right is not None and right.isVisible())
            panel.setVisible(True)
            if right is not None and self._filter_plugins_was_visible:
                right.setVisible(False)
            self._rebuild_filter_data()
        else:
            panel.setVisible(False)
            if right is not None and getattr(
                    self, "_filter_plugins_was_visible", False):
                right.setVisible(True)
            self._filter_plugins_was_visible = False

    # ---- footer button handlers -----------------------------------------
    def _on_toggle_collapse_all(self):
        """Expand all / Collapse all separators (toggles based on current state)."""
        m = self._modlist_model
        if not m.collapsible_separator_names():
            return
        collapse = not m.any_collapsed()   # if any expanded → collapse all
        self._modlist_view.set_all_collapsed(collapse)
        self._refresh_footer_toggle_labels()

    def _on_toggle_enable_all(self):
        """Enable all / Disable all toggleable mods."""
        m = self._modlist_model
        enable = not m.all_mods_enabled()
        m.set_all_enabled(enable)
        self._refresh_footer_toggle_labels()
        self._notify(self.tr("All mods enabled") if enable else self.tr("All mods disabled"),
                     "info")

    def _refresh_footer_toggle_labels(self):
        """Keep the Expand/Collapse all + Enable/Disable all button text in sync
        with the list state (Tk parity)."""
        m = self._modlist_model
        eb = getattr(self, "_expand_all_btn", None)
        if eb is not None:
            has_seps = bool(m.collapsible_separator_names())
            eb.setText(self.tr("Expand all") if (not has_seps or m.any_collapsed())
                       else self.tr("Collapse all"))
        nb = getattr(self, "_enable_all_btn", None)
        if nb is not None:
            nb.setText(self.tr("Disable all") if m.all_mods_enabled() else self.tr("Enable all"))

    def _on_refresh_modlist(self):
        """Refresh: re-sync the mods folder, reload the modlist + plugins, and
        force a full index rescan (picks up files added/removed inside mods)."""
        from Utils.mods.modlist import sync_modlist_with_mods_folder
        self._reassert_profile_paths()
        ml = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        if ml is not None and staging is not None:
            try:
                sync_modlist_with_mods_folder(ml, staging)
            except Exception as exc:
                print(f"[gui_qt] modlist sync failed: {exc}", flush=True)
        self._reload_modlist(rescan_index=True, preserve_overlays=True)
        self._reload_plugins()
        self._refresh_footer_toggle_labels()
        self._notify(self.tr("Modlist refreshed"), "info")

    # ---- search boxes ----------------------------------------------------
    def _on_modlist_search(self, text: str):
        """Modlist search: hide rows whose mod name (or owning separator block)
        doesn't contain the query. Debounced to coalesce fast typing."""
        self._modlist_search_text = text
        t = getattr(self, "_modlist_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(120)
            t.timeout.connect(self._apply_modlist_search)
            self._modlist_search_timer = t
        t.start()

    def _modlist_search_tooltip(self) -> str:
        """Rich-text tooltip for the search icon: the available `!` filter tags
        plus the filetype/category forms. Grouped for readability."""
        rows = [
            (self.tr("Updates"),        "!update"),
            (self.tr("Winning conflicts"),         "!winning"),
            (self.tr("Losing conflicts"),          "!losing"),
            (self.tr("Winning & losing"),          "!partial"),
            (self.tr("Fully conflicted"),          "!full"),
            (self.tr("FOMOD installs"),            "!fomod"),
            (self.tr("BAIN installs"),             "!bain"),
            (self.tr("Missing requirements"),      "!missing"),
            (self.tr("Has notes"),                 "!notes"),
            (self.tr("Has plugins"),               "!plugins"),
            (self.tr("Has BSA/BA2 archives"),      "!bsa"),
            (self.tr("PGPatcher textures"),        "!pbr"),
            (self.tr("Enabled / disabled"),        "!enabled · !disabled"),
            (self.tr("By file type"),              "!.dds"),
            (self.tr("By category"),               "!patches"),
        ]
        items = "".join(
            f"<tr><td style='padding-right:10px'><code>{tag}</code></td>"
            f"<td>{label}</td></tr>"
            for label, tag in rows)
        head = self.tr("Filter the modlist with search tags "
                       "(combine them, and with text):")
        return f"<b>{head}</b><table>{items}</table>"

    def _modlist_token_search_active(self) -> bool:
        """True when the search box holds a `!token` filter — those key off the
        live FilterData, so callers must rebuild/reapply it like a panel filter."""
        return "!" in (getattr(self, "_modlist_search_text", "") or "")

    def _apply_modlist_search(self):
        from gui_qt.modlist.modlist_filter import search_hidden_rows
        text = getattr(self, "_modlist_search_text", "")
        entries = self._modlist_model._entries
        active = bool((text or "").strip())
        data = getattr(self, "_modlist_filter_data", None)
        self._modlist_view.set_search_hidden(
            search_hidden_rows(entries, text, data), active=active)

    def _on_modlist_filter_changed(self, state: dict):
        self._modlist_filter_state = state
        self._apply_modlist_filters()
        self._update_filters_btn_active()

    def _quick_modlist_filter_state(self, key: str) -> int:
        """Read the current tri-state for a status filter (for the column-menu
        quick-filter check marks). Reads the panel so it reflects panel edits."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is not None:
            return panel.check_state(key)
        return (getattr(self, "_modlist_filter_state", {}) or {}).get(key, 0)

    def _on_quick_modlist_filter(self, key: str, state: int):
        """Apply a quick filter chosen from the column menu by driving the
        Filters panel checkbox, so the panel and menu stay in sync and the
        normal filter pipeline (footer-button tint included) runs."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is not None:
            panel.set_check(key, state)   # emits changed -> _on_modlist_filter_changed
        else:   # panel not built yet — fall back to the state dict directly
            self._modlist_filter_state[key] = state
            self._apply_modlist_filters()
            self._update_filters_btn_active()

    def _apply_modlist_filters(self):
        from gui_qt.modlist.modlist_filter import compute_hidden_rows
        state = getattr(self, "_modlist_filter_state", {}) or {}
        # Flatten the column sort across separator groups while separators are
        # hidden (must run before reading _entries — it may re-derive the sort).
        self._modlist_model.set_separators_hidden(
            state.get("filter_hide_separators") == 1)
        data = getattr(self, "_modlist_filter_data", None)
        if data is None:
            self._modlist_view.set_filter_hidden(set())
            return
        entries = self._modlist_model._entries
        hide = compute_hidden_rows(entries, state, data)
        self._modlist_view.set_filter_hidden(hide)

    def _update_filters_btn_active(self):
        """Tint the Filters footer button when any filter is active (Tk parity)."""
        btn = getattr(self, "_modlist_filters_btn", None)
        panel = getattr(self, "_modlist_filter_panel", None)
        if btn is None or panel is None:
            return
        btn.setProperty("active", panel.any_active())
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _rebuild_filter_data(self):
        """(Re)build FilterData: the disk-derived parts (modindex read + BSA /
        plugin folder walks) run on a worker; _on_filter_data_ready assembles
        the FilterData and updates the panel on the UI thread. Runs after every
        conflict rebuild, so the scans must not block the UI."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        staging = self._gs.staging_dir()
        staging_parent = staging.parent if staging is not None else None
        plugin_exts = getattr(self._gs.game, "plugin_extensions", None)
        self._filter_data_gen += 1
        gen = self._filter_data_gen

        def worker():
            payload = None
            if staging_parent is not None:
                from gui_qt.modlist.modlist_filter import (
                    build_index_data,
                    build_mods_with_bsa,
                    build_mods_with_plugins,
                )
                try:
                    counts, mod_ft, pbr = build_index_data(staging_parent)
                    payload = {
                        "filetype_counts": counts,
                        "mod_filetypes": mod_ft,
                        "mods_with_pbr": pbr,
                        "mods_with_bsa": build_mods_with_bsa(staging_parent),
                        "mods_with_plugins": build_mods_with_plugins(
                            staging_parent, plugin_exts),
                    }
                except Exception as exc:
                    print(f"[gui_qt] filter data scan failed: {exc}", flush=True)
            self._filter_data_ready.emit(gen, payload)

        threading.Thread(target=worker, daemon=True,
                         name="filter-data").start()

    def _on_filter_data_ready(self, gen: int, payload):
        """UI thread: merge the worker's disk-derived sets with the in-memory
        meta/conflict state into FilterData, repopulate the panel, reapply."""
        if gen != self._filter_data_gen:
            return   # superseded by a newer rebuild
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        from gui_qt.modlist.modlist_filter import FilterData
        cd = getattr(self, "_conflict_data", None)

        data = FilterData()
        if cd is not None:
            data.conflict_codes = self._loose_backend_codes(cd)
            data.bsa_conflict_codes = self._bsa_backend_codes(cd)
        data.mods_with_updates = set(getattr(self, "_mod_updates", set()))
        data.missing_reqs = set(getattr(self, "_mod_missing_reqs", set()))
        data.ignored_missing_reqs = set(getattr(self, "_ignored_missing_reqs", frozenset()))
        data.category_names = dict(getattr(self, "_mod_categories", {}))
        data.author_names = dict(getattr(self, "_mod_authors", {}))
        data.fomod_mods = set(getattr(self, "_mod_fomod", set()))
        data.bain_mods = set(getattr(self, "_mod_bain", set()))
        data.modified_mf_mods = self._build_modified_mf_mods()
        data.disabled_plugins_mods = self._build_disabled_plugins_mods()
        data.notes_mods = {m for m, note in self._read_mod_notes().items()
                           if (note or "").strip()}
        # Overlay the "modified in Mod Files" eye flag in the modlist Flags column.
        self._modlist_model.set_modified_mf(data.modified_mf_mods)
        if payload:
            data.filetype_counts = payload["filetype_counts"]
            data.mod_filetypes = payload["mod_filetypes"]
            data.mods_with_pbr = payload["mods_with_pbr"]
            data.mods_with_bsa = payload["mods_with_bsa"]
            data.mods_with_plugins = payload["mods_with_plugins"]
        self._modlist_filter_data = data

        # Repopulate dynamic lists.
        cats = sorted({(c or "") for c in data.category_names.values()} | {""},
                      key=lambda c: ("(Uncategorized)" if c == "" else c).lower())
        panel.set_dynamic_items("categories", [
            (c, "(Uncategorized)" if c == "" else c, None) for c in cats])
        # Authors (Nexus uploader). Only offer names we actually know — an
        # "(Unknown)" bucket for un-stamped mods would just match everything not
        # yet looked up, which isn't a useful filter.
        auths = sorted({a for a in data.author_names.values() if a},
                       key=str.lower)
        panel.set_dynamic_items("authors", [(a, a, None) for a in auths])
        fts = sorted(data.filetype_counts.items(), key=lambda kv: kv[0])
        panel.set_dynamic_items("filetypes", [
            (ext, ext, count) for ext, count in fts])

        # Relabel / re-enable game-specific filters, then reapply.
        self._refresh_filter_game_specific()
        self._apply_modlist_filters()
        # Token searches (e.g. "!update") depend on the FilterData we just
        # (re)built — re-run so results reflect fresh conflict/update data.
        if self._modlist_token_search_active():
            self._apply_modlist_search()

    def _build_modified_mf_mods(self) -> set:
        """Mods with Mod Files tab modifications — any excluded file OR a strip
        prefix (Tk `_mod_is_modified_in_mf`)."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return set()
        out: set[str] = set()
        try:
            from Utils.profile.profile_state import (
                read_excluded_mod_files,
                read_mod_strip_prefixes,
            )
            for mod, keys in (read_excluded_mod_files(pdir, None) or {}).items():
                if keys:
                    out.add(mod)
            for mod, prefixes in (read_mod_strip_prefixes(pdir, None) or {}).items():
                if any(p for p in prefixes):
                    out.add(mod)
        except Exception:
            pass
        return out

    def _build_rerun_fomod_mods(self) -> set:
        """Mods that should show the rerun-FOMOD flag. Two conditions, both read
        from meta.ini for FOMOD mods only (read_meta is mtime-cached, so cheap)
        and evaluated against the currently-enabled plugins:

          • `fomodPendingDeps` (UNSELECTED options' deps): flag when an AND-clause
            becomes FULLY present — a patch you skipped is now relevant.
          • `fomodActiveDeps` (SELECTED options' deps): flag when an AND-clause is
            NO LONGER fully present — a patch you installed is now orphaned (its
            required mod was removed/disabled).

        See FLAG_RERUN_FOMOD."""
        if not hasattr(self, "_modlist_model") or not hasattr(self, "_plugin_model"):
            return set()
        staging = self._gs.staging_dir()
        if staging is None:
            return set()
        try:
            enabled = self._plugin_model.enabled_lower()
        except Exception:
            return set()
        # Narrow to FOMOD-installed mods when we know them (avoids reading meta for
        # every mod); fall back to all mods if the set isn't populated yet.
        candidates = getattr(self, "_mod_fomod", None) or self._modlist_model.mod_names()
        # A DISABLED mod isn't deployed, so "rerun to fix it" is meaningless — and
        # its own plugins leave the load order when disabled, which would break its
        # self-referential fomodActiveDeps and falsely raise the flag. Skip them.
        try:
            enabled_mods = self._modlist_model.enabled_mod_names()
            candidates = [m for m in candidates if m in enabled_mods]
        except Exception:
            pass
        out: set[str] = set()
        try:
            from Nexus.nexus_meta import read_meta
        except Exception:
            return set()

        # Clause delimiters — imported so the encode (fomod_installer) and decode
        # (here) can never drift. They're all filename-illegal chars, so plugin
        # names containing "+", "!", "&" etc. don't collide.
        from Utils.installers.fomod_installer import (
            FLAG_ABSENT,
            FLAG_AND_SEP,
            FLAG_OPT_SEP,
            FLAG_OR_SEP,
        )

        def _member_holds(m: str) -> bool:
            # ">name" = the plugin must be ABSENT (a state="Missing" gate);
            # "name" = the plugin must be present + enabled.
            if m.startswith(FLAG_ABSENT):
                return m[len(FLAG_ABSENT):] not in enabled
            return m in enabled

        def _and_clause(clause: str):
            # An AND-clause → its lowercase members.
            return [m.strip().lower() for m in clause.split(FLAG_AND_SEP)
                    if m.strip()]

        def _clause_holds(members) -> bool:
            return bool(members) and all(_member_holds(m) for m in members)

        def _option_conditions(raw: str):
            # Each option-group is ONE option's condition, an OR-of-ANDs. Yield the
            # list of member-lists (the OR alternatives).
            for cond in (raw or "").split(FLAG_OPT_SEP):
                alts = [_and_clause(c) for c in cond.split(FLAG_OR_SEP)]
                alts = [a for a in alts if a]
                if alts:
                    yield alts

        def _option_met(alts) -> bool:
            # OR: the option's condition holds if ANY alternative clause holds.
            return any(_clause_holds(a) for a in alts)

        def _has_present_member(alts) -> bool:
            return any(not m.startswith(FLAG_ABSENT) for a in alts for m in a)

        for name in candidates:
            try:
                meta = read_meta(staging / name / "meta.ini")
            except Exception:
                continue
            # Pending: an UNSELECTED option's condition is now satisfiable → rerun
            # to pick up the newly-relevant patch. Require at least one PRESENT
            # member across the option (a non-"!" literal) so a condition that only
            # needs something ABSENT — true almost always — doesn't fire constantly.
            if any(_option_met(alts) and _has_present_member(alts)
                   for alts in _option_conditions(
                       getattr(meta, "fomod_pending_deps", ""))):
                out.add(name)
                continue
            # Active: a SELECTED option's condition is no longer satisfied (NONE of
            # its OR alternatives hold) → rerun to drop the now-orphaned patch. For
            # an OR option, having ONE of its plugins is still valid — don't fire.
            if any(not _option_met(alts)
                   for alts in _option_conditions(
                       getattr(meta, "fomod_active_deps", ""))):
                out.add(name)
        return out

    def _refresh_rerun_fomod_flags(self) -> None:
        """Recompute + push the rerun-FOMOD overlay onto the modlist model."""
        if hasattr(self, "_modlist_model"):
            self._modlist_model.set_rerun_fomod_mods(self._build_rerun_fomod_mods())

    def _refresh_filter_game_specific(self):
        """Relabel BSA→BA2 + enable/disable PGPatcher per the active game."""
        panel = getattr(self, "_modlist_filter_panel", None)
        g = self._gs.game
        if panel is None:
            return
        archive_exts = getattr(g, "archive_extensions", None) if g else None
        if archive_exts and ".ba2" in archive_exts:
            panel.set_check_label("filter_has_bsa", self.tr("Mods with BA2 archives"))
        elif archive_exts and ".pak" in archive_exts:
            panel.set_check_label("filter_has_bsa", self.tr("Mods with PAK archives"))
        else:
            panel.set_check_label("filter_has_bsa", self.tr("Mods with BSA archives"))
        # PGPatcher (PBR) is Skyrim SE only.
        is_sse = bool(g and getattr(g, "nexus_game_domain", "")
                      == "skyrimspecialedition")
        panel.set_check_enabled("filter_has_pbr", is_sse)

    def _loose_backend_codes(self, cd) -> dict:
        """Map the display loose_codes back to backend CONFLICT_* codes the
        engine filters on (DISP 1/-1/2/3 → WINS/LOSES/PARTIAL/FULL)."""
        from Utils.filemap import (
            CONFLICT_FULL,
            CONFLICT_LOSES,
            CONFLICT_PARTIAL,
            CONFLICT_WINS,
        )
        m = {1: CONFLICT_WINS, -1: CONFLICT_LOSES,
             2: CONFLICT_PARTIAL, 3: CONFLICT_FULL}
        return {n: m[c] for n, c in (cd.loose_codes or {}).items() if c in m}

    def _reassert_profile_paths(self):
        """Force game._active_profile_dir back in sync with GameState before
        resolving profile-derived paths (modlist/plugins/staging). Background
        workers (deploy pipeline, collection install/cancel-cleanup) swap the
        game object's active dir and can leave it stale or None — after which
        get_effective_mod_staging_path() silently falls back to the SHARED
        mods folder for profile-specific-mods profiles, and reload/sync/prune
        paths read (and destructively write) against the wrong profile.
        Skipped while a worker that legitimately owns that state is running —
        it re-establishes the right dir itself when it finishes. (getattr:
        the first _reload_modlist runs before the collection state is set.)"""
        if (getattr(self, "_deploy_running", False)
                or getattr(self, "_col_install_running", False)):
            return
        self._gs.reassert_active_profile()

    def _reload_modlist(self, rescan_index: bool = False,
                        preserve_overlays: bool = False):
        """Load the active game/profile's modlist + metadata into the model.
        rescan_index=True forces the conflict rebuild to rescan the index from
        disk (Refresh button).

        preserve_overlays=True keeps the current flag/conflict icons on screen
        until the async re-read lands (Refresh of the SAME game/profile — the
        overlays are keyed by mod name so they still render correctly). Leave it
        False for a game/profile switch, where the old overlays are stale and
        must be cleared immediately."""
        from gui_qt.modlist.modlist_data import read_meta_for_entries
        from Utils.mods.modlist import read_modlist
        from Utils.perftrace import span

        self._reassert_profile_paths()

        ml_path = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        with span("reload_modlist.read_modlist"):
            entries = (read_modlist(ml_path)
                       if (ml_path and ml_path.is_file()) else [])
        # Non-empty ⇒ this reload ends in a conflict rebuild (see bottom),
        # whose completion reloads the plugin panel. The profile-switch path
        # reads this to skip its own (redundant) immediate plugin reload.
        self._reload_had_entries = bool(entries)

        # Preserve the Mod Files tab's shown mod across a same-context refresh
        # (install/toggle/deploy/Refresh of the SAME game+profile): set_entries
        # clears the modlist selection, which would otherwise blank the tab. On a
        # game/profile SWITCH the shown mod may not exist, so we drop it.
        prev_context = (self._gs.game_name, self._gs.profile_dir())
        keep_mod_files = None
        if prev_context == getattr(self, "_modlist_context", None):
            mfv = getattr(self, "_mod_files_view", None)
            if mfv is not None and mfv.has_mod():
                keep_mod_files = mfv._mod_name
        self._modlist_context = prev_context

        self._mod_categories: dict[str, str] = {}
        self._mod_authors: dict[str, str] = {}
        self._mod_updates: set[str] = set()
        self._mod_fomod: set[str] = set()
        self._mod_bain: set[str] = set()
        self._mod_missing_reqs: set[str] = set()
        self._ignored_missing_reqs: frozenset[str] = frozenset()
        if entries and staging is not None:
            pdir = self._gs.profile_dir()
            if pdir is not None:
                try:
                    from Utils.profile.profile_state import (
                        read_ignored_missing_requirements,
                    )
                    self._ignored_missing_reqs = frozenset(
                        read_ignored_missing_requirements(pdir))
                except Exception:
                    self._ignored_missing_reqs = frozenset()

        # Entries go up immediately with blank meta so a game/profile switch
        # never blocks on disk; the per-mod meta.ini reads (one file per mod)
        # run on a worker → _modlist_meta_ready fills the columns/flags in.
        # The gen bump also drops a stale in-flight read on switch.
        self._modlist_meta_gen += 1
        meta_gen = self._modlist_meta_gen
        if not preserve_overlays:
            self._modlist_model._versions = {}
            self._modlist_model._installed = {}
            self._modlist_model._categories = {}
        with span("reload_modlist.set_entries"):
            self._modlist_model.set_entries(entries)
        if not preserve_overlays:
            self._modlist_model.set_flags({})
        with span("reload_modlist.read_notes"):
            self._modlist_model.set_notes(self._read_mod_notes())
        # (The meta worker itself starts at the END of this reload — see
        # below — so its disk work doesn't contend with the sync UI section.)
        if not preserve_overlays:
            self._modlist_model.set_conflicts({}, {})   # clear stale; recomputed async
        # Persist edits back to this modlist; rebuild conflicts after each save
        # (pure reorders that can't change the filemap skip the rebuild).
        self._modlist_model.modlist_path = ml_path
        self._modlist_model.on_saved = self._on_modlist_saved
        self._modlist_view.staging_dir = staging
        self._modlist_view.profile_dir = self._gs.profile_dir()
        self._modlist_view.game = self._gs.game
        # Right-click "Check Updates" reaches the window through this callback.
        self._modlist_view.on_check_updates = self._on_check_updates
        # Change Version: right-click item + clicking the update flag icon.
        self._modlist_view.on_change_version = self._open_change_version_tab
        # Change Version (mod.io): right-click item + clicking the mod.io
        # update flag icon.
        self._modlist_view.on_change_version_modio = self._open_change_version_modio_tab
        self._modlist_view.on_bundle_options = self._open_bundle_tab
        self._modlist_view.on_flag_clicked = self._on_modlist_flag_clicked
        # Missing Requirements: right-click item + clicking the ⚠ flag icon.
        self._modlist_view.on_missing_reqs = self._open_missing_reqs_tab
        self._modlist_view.ignored_missing_reqs = self._ignored_missing_reqs
        # View Requirements: right-click item (offline, reads meta.ini data).
        self._modlist_view.on_view_requirements = self._open_view_requirements_tab
        # Quick Update: right-click on update-flagged mods (premium direct DL).
        self._modlist_view.on_quick_update = self._quick_update_mods
        # Quick Update (mod.io): right-click on mod.io-update-flagged mods.
        self._modlist_view.on_quick_update_modio = self._quick_update_modio_mods
        # Reinstall: right-click item(s) whose install archive is still on disk.
        self._modlist_view.on_reinstall = self._reinstall_mods
        # Show Conflicts: right-click item.
        self._modlist_view.on_show_conflicts = self._open_show_conflicts_tab
        # Mod removal strips plugins from plugins.txt/loadorder.txt — reload the
        # plugin panel so the removed plugins disappear without a manual refresh.
        self._modlist_view.on_mods_removed = self._on_mods_removed
        # Root-Folder toggle wrote meta.ini → refresh the root flag immediately,
        # then rescan those mods + rebuild filemap (the index caches strip-applied
        # vs verbatim paths, so it's now stale; the rebuild also refreshes the
        # filemap-derived root-rule/pre-RTX overlays).
        self._modlist_view.on_root_folder_changed = \
            lambda names: (self._refresh_modlist_flags(names),
                           self._rebuild_conflicts_async(rescan_index=True))
        # Endorse/Abstain: needs the shared Nexus API + a flag refresh.
        self._modlist_view.on_endorse = self._on_modlist_endorse
        # Like/Unlike (mod.io): needs the mod.io OAuth token + a flag refresh.
        self._modlist_view.on_modio_like = self._on_modlist_modio_like
        # Track: needs the shared Nexus API (no meta.ini flag to persist).
        self._modlist_view.on_track = self._on_modlist_track
        # Collapsing from the context menu (or a separator click) changes what
        # the footer's Expand all / Collapse all toggle should say next.
        self._modlist_view.on_collapse_changed = self._refresh_footer_toggle_labels
        # A saved note may change the note flag — light flag-only refresh.
        self._modlist_view.on_notes_changed = self._refresh_modlist_flags
        # Copy/Move to profile: worker copy + collision overlay + (move) remove.
        self._modlist_view.on_copy_to_profile = self._copy_mods_to_profile
        self._modlist_view.on_copy_separators_to_profile = self._copy_separators_to_profile
        # Rename (context menu): folder + modindex + per-mod state migration.
        self._modlist_view.on_rename_mod = self._rename_mod_on_disk
        # "Fetch name from Nexus" button inside the rename dialog.
        self._modlist_view.mod_has_nexus_id = self._mod_has_nexus_id
        self._modlist_view.on_fetch_nexus_name = self._fetch_nexus_name
        # Separator settings (colour + deploy override): open the scoped tab;
        # rename/remove migrate/drop the stored colour + deploy entries.
        self._modlist_view.on_separator_settings = self._open_sep_settings_tab
        self._modlist_view.on_separator_renamed = self._on_separator_renamed
        self._modlist_view.on_separators_removed = self._on_separators_removed
        # Enabling the Size column scans mod folder sizes on demand (Tk parity:
        # only walk the disk when Size is actually shown).
        self._modlist_view.on_sizes_requested = self._apply_modlist_sizes
        self._modlist_view.on_quick_filter = self._on_quick_modlist_filter
        self._modlist_view.quick_filter_state = self._quick_modlist_filter_state
        self._modlist_model._sizes = {}
        if not self._modlist_view.isColumnHidden(COL_SIZE):
            self._apply_modlist_sizes()
        with span("reload_modlist.load_separator_state"):
            self._modlist_view.load_separator_state()
        self._modlist_view.load_mod_lock_state()
        # Point the Mod Files tab at this game/profile (index next to filemap).
        if hasattr(self, "_mod_files_view"):
            idx = (staging.parent / "modindex.bin") if staging is not None else None
            self._mod_files_view.configure(
                self._gs.game, self._gs.profile_dir(), idx)
            # Re-show the previously selected mod on a same-context refresh (it
            # updates against the freshly-scanned files); blank otherwise. The
            # Overwrite / Root Folder boundary rows are synthetic (not in the raw
            # modlist) but always present, so they survive too.
            from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME
            still_present = keep_mod_files is not None and (
                keep_mod_files in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
                or any(e.name == keep_mod_files for e in entries))
            self._mod_files_view.show_mod(
                keep_mod_files if still_present else None)
        # Point the Data tab at this game/profile (filemap.txt + modindex.bin).
        if hasattr(self, "_data_view"):
            fm = (staging.parent / "filemap.txt") if staging is not None else None
            idx2 = (staging.parent / "modindex.bin") if staging is not None else None
            self._data_view.configure(
                self._gs.game, self._gs.profile_dir(), fm, idx2)
        # Point the Downloads tab at this game (game-name getter for cache dir).
        if hasattr(self, "_downloads_view"):
            self._downloads_view.configure(
                self._gs.game, lambda: self._gs.game_name)
        # Point the Text Files tab at this game/profile.
        if hasattr(self, "_text_files_view"):
            fm3 = (staging.parent / "filemap.txt") if staging is not None else None
            self._text_files_view.configure(
                self._gs.game, self._gs.profile_dir(), fm3, staging)
        self._refresh_footer_toggle_labels()
        # Re-apply an active search + filters against the fresh row indices.
        # set_entries fired modelReset, which dropped the view's applied-hidden
        # cache but left the OLD filter-hidden row indices in place — without
        # this reapply they'd hide the wrong (or all) rows after an install,
        # blanking the list until a manual Refresh. compute_hidden_rows re-indexes
        # off the current entry list; the async conflict/filter-data rebuild below
        # then reapplies once more with the freshly-scanned per-mod data.
        with span("reload_modlist.search+filters"):
            self._apply_modlist_search()
            self._apply_modlist_filters()
        self._refresh_modlist_stats()
        print(f"[gui_qt] modlist: {ml_path} ({len(entries)} entries)")

        # The Downloads tab reflects the active profile's STAGING folder (which
        # changes with the profile), so refresh it on every reload — even for an
        # empty profile, where the conflict rebuild below is skipped.
        if hasattr(self, "_data_view"):
            self._data_view.mark_dirty()
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        if hasattr(self, "_text_files_view"):
            self._text_files_view.mark_dirty()
        # The Nexus browser (if open) shows Install/Reinstall per the active
        # profile's installed mods — refresh on every modlist reload (covers
        # profile/game change AND post-install).
        nv = getattr(self, "_nexus_view", None)
        if nv is not None:
            nv._game = self._gs.game
            nv.refresh_installed()

        # Meta read + conflict rebuild, SEQUENCED. Both cold-read the same
        # per-mod meta.ini files (the meta columns here, the filemap's
        # root-flag collect inside the conflict build); run concurrently they
        # each parse ~the whole set and stretch each other under the GIL
        # (measured 4.0s + 3.1s concurrent vs ~0.4s + ~0.05s sequenced — the
        # second reader hits the read_meta mtime cache). So: meta worker
        # first, and _on_modlist_meta_ready chains the conflict rebuild.
        meta_started = False
        if entries and staging is not None:
            ignored = self._ignored_missing_reqs
            pdir_meta = self._gs.profile_dir()
            is_bg3 = (getattr(self._gs.game, "game_id", "") == "baldurs_gate_3")
            is_tw3 = (getattr(self._gs.game, "game_id", "") == "witcher_3")
            game_meta = self._gs.game
            meta_entries = list(entries)

            def meta_worker():
                try:
                    collection_order_mods = frozenset()
                    if is_tw3 and pdir_meta is not None:
                        try:
                            from Utils.mods.tw3_load_index import (
                                collection_governed_mod_names,
                            )
                            collection_order_mods = frozenset(
                                collection_governed_mod_names(game_meta, pdir_meta))
                        except Exception as exc:
                            print(f"[gui_qt] TW3 collection-order scan failed: {exc}", flush=True)
                    with span("modlist.meta_worker(read_meta)"):
                        payload = read_meta_for_entries(
                            meta_entries, staging, ignored,
                            profile_dir=pdir_meta, is_bg3=is_bg3,
                            collection_order_mods=collection_order_mods,
                            provided=game_provides_requirement(game_meta))
                except Exception as exc:
                    print(f"[gui_qt] meta read failed: {exc}", flush=True)
                    payload = None   # still emit — the conflict rebuild chains
                self._modlist_meta_ready.emit(meta_gen, payload)

            self._conflicts_after_meta = (meta_gen, rescan_index)
            threading.Thread(target=meta_worker, daemon=True,
                             name="modlist-meta").start()
            meta_started = True

        if entries and not meta_started:
            # No meta worker (staging unresolved) → kick the rebuild directly.
            self._rebuild_conflicts_async(rescan_index=rescan_index)
        elif not entries and rescan_index:
            # Empty modlist but an explicit rescan was requested (Refresh /
            # install / toggle). The conflict rebuild is normally skipped when
            # there are no mods (nothing to conflict), but the [Overwrite]
            # pseudo-mod is INDEPENDENT of the real modlist: files can sit in
            # overwrite/ (and in modindex.bin's [Overwrite] entry) with zero
            # enabled mods. Skipping the rebuild here left a stale [Overwrite]
            # index entry + stale filemap.txt that no Refresh could clear once
            # the modlist was empty — the file kept showing in the Data tab and
            # kept deploying (as a dangling symlink). Run the rebuild so the
            # index and filemap are reconciled against the folder; it also
            # writes an empty filemap when the last mod is removed.
            # _on_conflicts_ready sets _switch_conflicts_done when the build
            # lands, so the switch-marker bookkeeping below is still covered.
            self._rebuild_conflicts_async(rescan_index=rescan_index)
        elif not entries and getattr(self, "_switch_t0", None) is not None:
            # Empty profile, no rescan (plain profile switch) → no conflict
            # rebuild will follow, so the first plugin pass IS the final one;
            # without this the switch marks would leak into the next unrelated
            # plugin reload.
            self._switch_conflicts_done = True

    def _on_modlist_meta_ready(self, gen, payload):
        """UI thread: apply the worker-read per-mod meta (see _reload_modlist).
        *payload* is None when the read failed — the apply is skipped but the
        chained conflict rebuild below still runs (it must not be lost)."""
        if gen != self._modlist_meta_gen:
            return   # superseded — the game/profile switched mid-read
        self._mark_since_switch("switch→modlist_meta_applied")  # i18n: skip — perftrace marker label
        from Utils.perftrace import span
        # Chained conflict/filemap rebuild (see _reload_modlist): kicked here,
        # AFTER the meta read, so its root-flag collect hits the warm read_meta
        # cache instead of racing the worker over the same 400+ ini files.
        pend = getattr(self, "_conflicts_after_meta", None)
        if pend is not None and pend[0] == gen:
            self._conflicts_after_meta = None
            self._rebuild_conflicts_async(rescan_index=pend[1])
        if payload is None:
            return
        (versions, installed, flags, categories, updates,
         fomod, bain, missing_reqs, descriptions, authors) = payload
        self._mod_categories = categories
        self._mod_authors = authors
        self._mod_updates = updates
        self._mod_fomod = fomod
        self._mod_bain = bain
        self._mod_missing_reqs = missing_reqs
        with span("on_modlist_meta_ready(apply)"):
            self._modlist_model.set_meta(versions, installed, categories,
                                         descriptions, authors)
            self._modlist_model.set_flags(flags)
        # Now that _mod_fomod + meta are current, refresh the rerun-FOMOD overlay
        # (picks up a just-installed/updated mod's pending deps without waiting for
        # the next plugin reload).
        self._refresh_rerun_fomod_flags()
        # Prune any installed requirements from an open Missing Requirements panel
        # (this is the path the panel's own Install button lands on).
        view = getattr(self, "_missing_reqs_view", None)
        if view is not None:
            try:
                view.prune_installed(self._installed_mod_ids())
            except Exception:
                pass
        # The meta-derived filter inputs (updates / categories / fomod / bain /
        # missing-reqs) were just refreshed on this thread. _reload_modlist
        # cleared them to empty sets before the worker ran, so any active filter
        # that keys off them (e.g. "mods with updates") was reapplied against the
        # stale FilterData and blanked the list until Refresh. Rebuild the filter
        # data now so _on_filter_data_ready merges the fresh sets and reapplies —
        # only when a filter is actually active (avoids needless disk scans).
        # A token search (e.g. "!updates") keys off the SAME meta-derived sets,
        # so it must rebuild too — otherwise updating a mod that a token matched
        # reapplies "!updates" against the just-cleared sets and blanks the list.
        panel = getattr(self, "_modlist_filter_panel", None)
        panel_active = panel is not None and panel.any_active()
        if panel_active or self._modlist_token_search_active():
            self._rebuild_filter_data()

    def _apply_modlist_sizes(self):
        """Scan mod folder sizes and push them to the model. Called on reload
        when the Size column is visible, and when the user enables Size from the
        column menu (the disk walk is skipped while Size stays hidden).

        The walk covers every mod folder, so it runs on a daemon worker —
        a generation counter drops a stale result (profile switched or a
        newer scan started mid-walk)."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        self._sizes_gen = getattr(self, "_sizes_gen", 0) + 1
        gen = self._sizes_gen
        # Snapshot the entry list — the user can reorder/rename while the
        # worker walks the disk.
        entries = list(self._modlist_model.natural_entries())

        from gui_qt.worker import NO_EMIT, run_in_worker

        def scan():
            from gui_qt.modlist.modlist_data import compute_sizes
            sizes, size_bytes = compute_sizes(entries, staging)
            return gen, sizes, size_bytes

        run_in_worker(scan, self._sizes_ready, name="modlist-sizes",
                      unpack=True, error_result=NO_EMIT)

    def _on_sizes_ready(self, gen, sizes, size_bytes):
        if gen != getattr(self, "_sizes_gen", 0):
            return   # superseded — a newer scan is in flight
        self._modlist_model.set_sizes(sizes, size_bytes)

    def _refresh_modlist_stats(self):
        """Update the modlist footer count label: enabled / total mod counts.
        Both come straight from the model, so this is instant."""
        lbl = getattr(self, "_modlist_count", None)
        if lbl is None:
            return
        entries = [self._modlist_model.entry(r)
                   for r in range(self._modlist_model.rowCount())]
        mods = [e for e in entries if not e.is_separator]
        enabled = sum(1 for e in mods if e.enabled)
        lbl.setText(f"{enabled} / {len(mods)}")
        lbl.setToolTip(self.tr("{0} enabled of {1} mods").format(enabled, len(mods)))

    def _installed_mod_ids(self) -> set[int]:
        """The set of Nexus mod ids currently installed in the active profile's
        staging folder (read from each mod's meta.ini). Used to prune the
        Missing Requirements panel once a requirement is installed."""
        ids: set[int] = set()
        staging = self._gs.staging_dir()
        if staging is None:
            return ids
        from Nexus.nexus_meta import read_meta
        for r in range(self._modlist_model.rowCount()):
            e = self._modlist_model.entry(r)
            if e is None or e.is_separator:
                continue
            meta_path = staging / e.name / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                mid = int(getattr(read_meta(meta_path), "mod_id", 0) or 0)
            except Exception:
                continue
            if mid > 0:
                ids.add(mid)
        return ids

    def _on_mods_removed(self):
        """A mod was fully removed (single or multi right-click Remove): reload
        the plugins panel AND re-run the light flag pass. Removing a mod must
        resurface the ⚠ missing-requirement flag on mods that depended on it —
        the seeded list stays in meta.ini for exactly this, and the flag
        two-pass recomputes installed ids from the remaining rows. Must be a
        FULL flag pass (no names subset): a subset would compute installed ids
        from the subset alone."""
        self._reload_plugins()
        self._refresh_modlist_flags()

    def _refresh_modlist_flags(self, names=None):
        """Re-read the meta/profile-derived flags and push them into the model
        WITHOUT the full modlist reset (keeps selection + scroll). Call after any
        action that changes a flag (endorse, root toggle, note edit). When
        *names* is given, only those mods' metas are re-read and merged into the
        existing state (endorsing one mod shouldn't re-read 500 meta files).
        The filemap-derived overlays (pre-RTX / root-rule) refresh via the
        conflict-ready path instead."""
        from gui_qt.modlist.modlist_data import read_meta_for_entries
        # Reached from ten call sites, several of them right after a background
        # worker finished — never trust the game object's current active-profile
        # state here. Without this, a stale _active_profile_dir sends
        # staging_dir() to the shared (empty) mods/ folder and the read below
        # comes back with no flags for anything.
        self._gs.reassert_active_profile()
        staging = self._gs.staging_dir()
        if staging is None:
            return
        subset = set(names) if names else None
        entries = [e for r in range(self._modlist_model.rowCount())
                   if (e := self._modlist_model.entry(r)) is not None
                   and (subset is None or e.name in subset)]
        try:
            (_v, _i, flags, categories, updates, fomod, bain,
             missing_reqs, _desc, authors) = read_meta_for_entries(
                entries, staging, self._ignored_missing_reqs,
                profile_dir=self._gs.profile_dir(),
                is_bg3=(getattr(self._gs.game, "game_id", "") == "baldurs_gate_3"),
                provided=game_provides_requirement(self._gs.game))
        except Exception:
            return
        # Safety net, independent of the guard above. A full refresh returning
        # nothing is legitimate (clearing the last note on a short list), but
        # not when the staging folder holds no meta.ini for ANY known mod —
        # that only happens when the read went to the wrong folder, and
        # blanking the whole Flags column on the strength of it is never right.
        # The probe only runs in that already-suspicious case, and stops at the
        # first hit.
        if (subset is None and not flags and entries
                and getattr(self._modlist_model, "_flags", None)
                and not any((staging / e.name / "meta.ini").is_file()
                            for e in entries)):
            self._append_log(
                f"Flag refresh skipped: no mod metadata found under {staging} "
                f"({len(entries)} mod(s) expected) — keeping the current flags.")
            return
        if subset is None:
            (self._mod_categories, self._mod_authors, self._mod_updates,
             self._mod_fomod, self._mod_bain, self._mod_missing_reqs) = (
                categories, authors, updates, fomod, bain, missing_reqs)
        else:
            # Merge: clear the requested names first (a cleared flag won't
            # appear in the subset result), then overlay the fresh values.
            flags = {**{n: b for n, b in self._modlist_model._flags.items()
                        if n not in subset}, **flags}
            self._mod_categories = {**{n: c for n, c in self._mod_categories.items()
                                       if n not in subset}, **categories}
            self._mod_authors = {**{n: a for n, a in
                                    getattr(self, "_mod_authors", {}).items()
                                    if n not in subset}, **authors}
            for cur, fresh in ((self._mod_updates, updates),
                               (self._mod_fomod, fomod),
                               (self._mod_bain, bain),
                               (self._mod_missing_reqs, missing_reqs)):
                cur -= subset
                cur |= fresh
        # FLAG_COLLECTION_ORDER_LOCKED (TW3) is NOT recomputed on this path --
        # it's a cheap targeted refresh after things like endorse/note-edit,
        # not a full profile rescan (which would need a real file-content
        # scan, too costly to run on every small action). Preserve whatever
        # it already was so this can't silently clear it; the full reload
        # path (_on_modlist_meta_ready) recomputes it properly.
        from gui_qt.modlist.modlist_data import FLAG_COLLECTION_ORDER_LOCKED
        prev_flags = getattr(self._modlist_model, "_flags", None) or {}
        for name, prev_bits in prev_flags.items():
            if prev_bits & FLAG_COLLECTION_ORDER_LOCKED:
                flags[name] = flags.get(name, 0) | FLAG_COLLECTION_ORDER_LOCKED
        self._modlist_model.set_flags(flags)
        # Re-point the model at the (possibly rebuilt) categories dict and
        # re-sort if the Category column drives the current sort.
        self._modlist_model._categories = self._mod_categories
        self._modlist_model._resort_if_key("category")
        self._modlist_model.set_notes(self._read_mod_notes())
        # If the Missing Requirements panel is open, drop cards for any
        # requirement that's now installed (works for any install path).
        view = getattr(self, "_missing_reqs_view", None)
        if view is not None:
            try:
                view.prune_installed(self._installed_mod_ids())
            except Exception:
                pass
        # This light path just mutated the meta-derived sets (updates / fomod /
        # bain / missing-reqs) in place. An active token search or panel filter
        # keys off the FilterData COPY of those sets, so refresh it and reapply —
        # otherwise a mod that just lost its !update flag lingers (or the list
        # goes stale) until a full Refresh.
        data = getattr(self, "_modlist_filter_data", None)
        if data is not None:
            data.mods_with_updates = set(self._mod_updates)
            data.fomod_mods = set(self._mod_fomod)
            data.bain_mods = set(self._mod_bain)
            data.missing_reqs = set(self._mod_missing_reqs)
            self._apply_modlist_filters()
        if self._modlist_token_search_active():
            self._apply_modlist_search()

    def _read_mod_notes(self) -> dict:
        """Per-mod note text for the active profile (Note flag tooltip). {} if
        none / no profile."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return {}
        try:
            from Utils.profile.profile_state import read_mod_notes
            return read_mod_notes(pdir)
        except Exception:
            return {}

    def _refresh_cycle_tab_data(self):
        """Push the pinned scope's current rules to the open cycle tab."""
        if not self._tabs.has_key("plugin_cycle"):
            return
        view = getattr(self, "_plugin_cycle_view", None)
        anchor = getattr(self, "_cycle_anchor", "")
        scope = getattr(self, "_cycle_scope", frozenset())
        if view is None or not anchor or not scope:
            return
        from Utils.userlist import build_cycle_scope_data, parse_userlist
        ul_path = self._userlist_path()
        data = (parse_userlist(ul_path) if (ul_path and ul_path.is_file())
                else {"plugins": [], "groups": []})
        display: dict[str, str] = {}
        for r in self._plugin_model._rows:
            display[r.name.lower()] = r.name
        for entry in data.get("plugins", []):
            raw = entry.get("name") or ""
            if raw:
                display.setdefault(raw.lower(), raw)
        info = build_cycle_scope_data(data, scope, display)
        view.update_cycle(
            starting_plugin=display.get(anchor, anchor),
            scope_plugins=scope,
            display_names=display,
            **info,
        )

    def _on_group_add(self, plugin_names: list):
        """'Add to group…' — open the inline group-assignment bar."""
        if self._userlist_path() is None:
            self._notify(self.tr("No active profile — cannot assign group."), "warning")
            return
        self._grp_bar.open_for(plugin_names)

    def _on_overlap_ready(self, plugin_name: str, overlaps):
        self._overlap_running = False
        if overlaps is None:
            self._notify(self.tr("Overlap check failed — see log."), "error")
            return
        if not overlaps:
            self._notify(self.tr("{0}: no record overlap with other plugins.").format(plugin_name),
                         "info")
            self._plugin_view.set_master_highlight(set())
            return

        # Highlight the target + overlapping plugins (green tint + marker strip),
        # then scroll the first highlighted row into view (Tk parity).
        names_lower = {plugin_name.lower(), *(o.lower() for o in overlaps)}
        self._plugin_view.set_master_highlight(names_lower)
        m = self._plugin_model
        first = next((i for i in range(m.rowCount())
                      if m.row(i).name.lower() in names_lower), None)
        if first is not None:
            from PySide6.QtWidgets import QAbstractItemView

            from gui_qt.plugin.plugin_model import COL_FLAGS
            self._plugin_view.scrollTo(m.index(first, COL_FLAGS),
                                       QAbstractItemView.PositionAtCenter)
        self._append_log(
            f"[loot] {plugin_name} overlaps with {len(overlaps)} plugin(s): "
            + ", ".join(overlaps))
        self._notify(
            self.tr("{0} overlaps {1} plugin(s) — highlighted in list").format(
                plugin_name, len(overlaps)),
            "warning")

    def _on_modlist_saved(self, edit_ctx=None):
        """modlist.txt was rewritten (every structural edit funnels here via
        model.save()). edit_ctx classifies the edit (see model.save):
          ("move", moved, crossed) — when the move provably can't have changed
            the filemap, skip EVERYTHING (worker + plugins reload + auto-deploy
            are all no-ops).
          ("toggle", changes) — when a disable provably can't change any
            conflict product, rebuild ONLY the filemap (the toggled mod's
            entries must still leave it) and auto-deploy; skip the scan.
          None/unknown → full rebuild."""
        if edit_ctx is not None:
            kind = edit_ctx[0]
            if kind == "move" and self._move_skips_rebuild(edit_ctx[1:]):
                return
            if kind == "toggle" and self._toggle_skips_conflict_scan(edit_ctx[1]):
                self._rebuild_filemap_light_async()
                return
        self._rebuild_conflicts_async()

    def _move_skips_rebuild(self, move_ctx) -> bool:
        """True when a reorder left every moved mod on the same side of all
        its conflict partners (loose AND archive), so no filemap winner,
        conflict code or override pair can have changed. Archive partners
        matter because orphan BSAs and UE paks rank by MOD priority — a move
        across an archive partner flips that winner (plugin-tied BSAs follow
        plugin order and are unaffected, but they're in the same maps, so
        including them just makes the check more conservative). Disabled mods
        aren't in the maps (empty partners). Sufficiency of the direct-pair
        maps: provider stacks are priority-ordered, so crossing ANY
        co-provider means first crossing a direct neighbour in that stack —
        which IS in the override maps. Conservative: any doubt (stale/missing
        conflict data, unknown mod) → full rebuild. AMM_MOVE_SKIP_REBUILD=0
        kills the fast path."""
        if os.environ.get("AMM_MOVE_SKIP_REBUILD") == "0":
            return False
        # Maps must describe the CURRENT modlist: _conflict_maps_current is
        # armed only when a build's result is accepted and dropped whenever a
        # rebuild is queued (a drag racing an in-flight toggle rebuild would
        # otherwise test against the pre-toggle maps). Skipped moves keep the
        # maps valid — that's exactly what this predicate proves.
        if not getattr(self, "_conflict_maps_current", False):
            return False
        data = self._conflict_data
        if data is None:
            return False
        moved, crossed = move_ctx
        crossed_set = set(crossed)
        if moved and crossed_set:
            for m in moved:
                partners = ((data.overrides.get(m) or set())
                            | (data.overridden_by.get(m) or set())
                            | (data.bsa_overrides.get(m) or set())
                            | (data.bsa_overridden_by.get(m) or set()))
                if partners & crossed_set:
                    return False
        self._append_log(
            f"[filemap] move: no conflict crossing — rebuild skipped "
            f"(moved={len(moved)}, crossed={len(crossed_set)})")
        return True

    def _toggle_skips_conflict_scan(self, changes) -> bool:
        """True when a toggle batch is disable-only and every disabled mod has
        no loose-conflict partners, no plugin files, no BSA/BA2 archives and
        no framework-exe files — then no ConflictData product changes: codes/
        override maps lose nothing (the mod's sets were empty), plugin_owner
        and the plugins panel keep the same winners, BSA conflicts follow
        plugin order + bsa_index (untouched), framework statuses can't flip
        (matching is by exe basename — the mod ships none). Only the filemap
        file set shrinks → _rebuild_filemap_light_async. Enables are never
        skipped: the maps can't prove a disabled mod won't CREATE conflicts.
        Capability sets come from the same accepted build as the maps (see
        ConflictData), so the _conflict_maps_current guard covers them too.
        AMM_TOGGLE_LIGHT=0 kills the fast path."""
        if os.environ.get("AMM_TOGGLE_LIGHT") == "0":
            return False
        if not getattr(self, "_conflict_maps_current", False):
            return False
        data = self._conflict_data
        if data is None or not changes:
            return False
        for name, enabled in changes:
            if enabled:
                return False
            if name not in data.overrides and name not in data.overridden_by:
                return False   # unknown to the maps → treat as stale
            if data.overrides.get(name) or data.overridden_by.get(name):
                return False
            if (name in data.plugin_mods or name in data.bsa_mods
                    or name in data.framework_file_mods):
                return False
        self._append_log(
            f"[filemap] toggle: disable-only, no conflicts/plugins/BSAs — "
            f"conflict scan skipped ({len(changes)} mod(s), filemap-only)")
        return True

    def _rebuild_filemap_light_async(self):
        """Disable fast path: rebuild ONLY the filemap (the incremental delta
        removes the toggled mods' entries) — _toggle_skips_conflict_scan proved
        every conflict product unchanged, so skip the scan, the panel reloads
        and the conflict-map repaints. Shares the build lock + generation with
        _rebuild_conflicts_async so full rebuilds serialize/supersede normally;
        _conflict_maps_current deliberately stays armed (the maps still
        describe reality — that is exactly what the predicate proved)."""
        self._reassert_profile_paths()
        gen = getattr(self, "_conflict_gen", 0) + 1
        self._conflict_gen = gen
        if not hasattr(self, "_conflict_build_lock"):
            self._conflict_build_lock = threading.Lock()
        g = self._gs.game
        profile = self._gs.profile
        if g is None or not profile:
            return

        def worker():
            from Utils.perftrace import span
            with self._conflict_build_lock:
                if gen != self._conflict_gen:
                    return   # superseded while waiting — the newer build covers us
                from Utils.deploy.deploy_pipeline import _build_filemap_for_game

                def _fm_log(m):
                    m = str(m)
                    try:
                        m.encode("utf-8")
                    except UnicodeEncodeError:
                        m = m.encode("utf-8", "backslashreplace").decode(
                            "utf-8", "replace")
                    print(f"[filemap] {m}", flush=True)
                    self._append_log(f"[filemap] {m}")

                try:
                    with span("build_filemap(light)"):
                        _build_filemap_for_game(
                            g, profile, log_fn=_fm_log, rescan_index=False)
                except Exception as exc:
                    print(f"[gui_qt] light filemap rebuild failed: {exc}",
                          flush=True)
            self._filemap_light_done.emit(gen)

        threading.Thread(target=worker, daemon=True).start()

    def _on_filemap_light_done(self, gen: int):
        """UI thread: a filemap-only rebuild finished. Refresh only what the
        deployed file SET touches — conflict maps, plugins panel, filter data
        and framework banner are untouched by construction."""
        if gen != self._conflict_gen:
            return   # superseded by a full rebuild — its handler covers this
        from Utils.perftrace import span
        with span("on_filemap_light_done"):
            if hasattr(self, "_data_view"):
                self._data_view.mark_dirty()
            if hasattr(self, "_text_files_view"):
                self._text_files_view.mark_dirty()
            self._refresh_modlist_stats()
            # Active filters may key on the enabled state — reapply from the
            # in-memory FilterData (no disk rescan needed).
            self._apply_modlist_filters()
        self._maybe_auto_deploy()

    def _rebuild_conflicts_async(self, rescan_index: bool = False):
        """Build the filemap off-thread; the worker emits _conflicts_ready
        (queued → UI thread). A generation counter drops results from a
        superseded reload (user switched game before the build finished).
        rescan_index=True forces a full disk rescan (Refresh button)."""
        # The maps the move fast-path tests are about to be superseded.
        self._conflict_maps_current = False
        self._reassert_profile_paths()
        gen = getattr(self, "_conflict_gen", 0) + 1
        self._conflict_gen = gen
        # A requested full rescan must SURVIVE supersession. Previously
        # rescan_index lived only in this call's worker closure: if a Refresh's
        # rescan build (rescan_index=True) was superseded — e.g. by the enable
        # toggle the user does immediately after Refresh, which triggers a plain
        # rescan_index=False rebuild — the Refresh worker returned early WITHOUT
        # rescanning, and the superseding build reused the STALE index. The
        # just-added mod then never entered modindex.bin, so it dropped out of
        # filemap.txt (no conflicts / plugins / Data-tab) until some later full
        # rescan happened to win the race (or a different tool — e.g. the Tk
        # build, which always rescans — rewrote the index on disk). Latch the
        # request on the window so whichever build actually runs consumes it.
        if rescan_index:
            self._pending_rescan_index = True
        # Serialize the actual build: rapid triggers (e.g. a Mod Files edit while
        # a previous rescan is still running) otherwise run two rebuild_mod_index
        # writes concurrently and collide on modindex.bin.tmp. The generation
        # check still drops superseded RESULTS.
        if not hasattr(self, "_conflict_build_lock"):
            self._conflict_build_lock = threading.Lock()

        from Utils.perftrace import span

        def worker():
            # Diagnostics below go to BOTH stderr and the GUI log panel
            # (_append_log, thread-safe) so a normal user can see + send them
            # without relaunching with an env var. They fire once per conflict
            # build / supersession (not per-frame), so they're low-noise.
            with self._conflict_build_lock:
                if gen != self._conflict_gen:
                    msg = (f"conflict build gen={gen} SUPERSEDED "
                           f"(current={self._conflict_gen}) — skipped before "
                           f"build; requested rescan={rescan_index}, "
                           f"latch pending={getattr(self, '_pending_rescan_index', False)}")
                    print(f"[plugin-diag] {msg}", flush=True)
                    self._append_log(f"[rescan-diag] {msg}")
                    return   # a newer build was queued while we waited — skip
                # Honour a latched rescan request even if THIS build was queued
                # as rescan_index=False (it superseded a pending Refresh). The
                # flag is NOT cleared here — it's cleared in _on_conflicts_ready
                # only when this build's result is actually ACCEPTED. That way,
                # if this rescan is itself superseded mid-build (its result
                # dropped by the gen check), the request stays latched for the
                # next build instead of being lost.
                _latched = getattr(self, "_pending_rescan_index", False)
                do_rescan = rescan_index or _latched
                # Record what this gen's build did, so _on_conflicts_ready can
                # clear the latch only when a rescanning build is accepted.
                self._conflict_gen_did_rescan = (gen, do_rescan)
                msg = (f"build gen={gen}: requested rescan={rescan_index}, "
                       f"latched={_latched} → doing rescan={do_rescan}")
                print(f"[plugin-diag] {msg}", flush=True)
                self._append_log(f"[rescan-diag] {msg}")
                def _fm_log(m):
                    # Console for our own tracing + the GUI log panel so a user
                    # actually SEES filemap warnings (enabled-mod-not-indexed,
                    # symlink skips, name mismatches). _append_log is thread-safe
                    # (marshals to the UI thread) and classifies WARN severity.
                    # Escape surrogate bytes from on-disk names FIRST — printing
                    # them raw raises UnicodeEncodeError on strict-UTF-8 stdout,
                    # which aborted the index rescan this log_fn serves.
                    m = str(m)
                    try:
                        m.encode("utf-8")
                    except UnicodeEncodeError:
                        m = m.encode("utf-8", "backslashreplace").decode(
                            "utf-8", "replace")
                    print(f"[filemap] {m}", flush=True)
                    self._append_log(f"[filemap] {m}")
                with span(f"build_conflicts(rescan={do_rescan})"):
                    data = self._gs.build_conflicts(
                        log_fn=_fm_log,
                        rescan_index=do_rescan)
                # Decisive check: after the build, is every ENABLED modlist mod
                # actually present in the index the filemap was built from? An
                # enabled mod missing here is THE failure (no conflicts/plugins).
                self._log_enabled_not_indexed(gen)
            self._conflicts_ready.emit(gen, data)

        threading.Thread(target=worker, daemon=True).start()

    def _log_enabled_not_indexed(self, gen: int) -> None:
        """Diagnostic (off-thread): report any ENABLED modlist mod that is
        absent from modindex.bin after a build. An enabled mod missing from the
        index is the exact failure behind "no conflicts / no plugins / not in
        Data tab" — it isolates whether the cause is a stale index (rescan race:
        mod on disk + in modlist but not indexed) vs. a name/scan issue
        (near-match key) vs. all-good. Reads the index once per build."""
        try:
            from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME, read_mod_index
            from Utils.mods.modlist import read_modlist
            staging = self._gs.staging_dir()
            ml = self._gs.modlist_path()
            if staging is None or ml is None or not ml.is_file():
                return
            index_path = staging.parent / "modindex.bin"
            index = read_mod_index(index_path) or {}
            # Surface the RESOLVED index path + any RIVAL modindex.bin at the
            # OTHER convention (shared <profile_root>/ vs profile-specific
            # <profile_dir>/). Two files means the staging-path resolution
            # flip-flopped (stale _active_profile_dir) and reads/writes are
            # hitting different indexes — the "two modindex.bin" bug.
            self._append_log(
                f"[rescan-diag] gen={gen}: index path = {index_path} "
                f"(exists={index_path.is_file()})")
            try:
                canon = index_path.resolve()
                pd = self._gs.profile_dir()
                stray = (pd / "modindex.bin") if pd is not None else None
                if (stray is not None and stray.is_file()
                        and stray.resolve() != canon):
                    self._append_log(
                        f"[rescan-diag] gen={gen}: WARNING — stray modindex.bin "
                        f"at {stray} describes the same shared mods folder as the "
                        f"canonical index ({index_path}); it is a legacy leftover "
                        f"and should be removed (a Refresh sweep removes it).")
            except Exception:
                pass
            enabled = [e.name for e in read_modlist(ml)
                       if e.enabled and not e.is_separator
                       and e.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)]
            missing = []
            for name in enabled:
                if name in index:
                    continue
                on_disk = (staging / name).is_dir()
                near = [k for k in index if k.strip().casefold() == name.strip().casefold()]
                missing.append((name, on_disk, near))
            if not missing:
                self._append_log(
                    f"[rescan-diag] gen={gen}: OK — all {len(enabled)} enabled "
                    f"mod(s) present in modindex.bin ({len(index)} indexed)")
                return
            self._append_log(
                f"[rescan-diag] gen={gen}: {len(missing)} ENABLED mod(s) MISSING "
                f"from modindex.bin (index has {len(index)} mod(s)):")
            for name, on_disk, near in missing[:20]:
                if near:
                    reason = f"NAME MISMATCH — index has {near!r}"
                elif on_disk:
                    reason = ("folder EXISTS on disk but NOT indexed → STALE INDEX "
                              "(rescan race / rescan skipped)")
                else:
                    reason = "folder not found on disk"
                self._append_log(f"[rescan-diag]   {name!r}: {reason}")
        except Exception as exc:
            print(f"[plugin-diag] _log_enabled_not_indexed error: {exc}", flush=True)

    def _on_conflicts_ready(self, gen: int, data):
        if gen != self._conflict_gen:
            msg = (f"conflicts_ready gen={gen} SUPERSEDED "
                   f"(current={self._conflict_gen}) — result dropped, plugins "
                   f"NOT reloaded from this build")
            print(f"[plugin-diag] {msg}", flush=True)
            self._append_log(f"[rescan-diag] {msg}")
            return
        # This build's result is accepted. If it performed the latched full
        # rescan, clear the latch now (a fresh, correct modindex.bin is on disk).
        # A rescan that was superseded mid-build never reaches here, so its
        # latch survives for the next build — the request can't be lost.
        _did = getattr(self, "_conflict_gen_did_rescan", None)
        if _did is not None and _did[0] == gen and _did[1]:
            self._pending_rescan_index = False
        from Utils.perftrace import span
        # Profile-switch milestone: the conflict/filemap build (usually the
        # long pole) has landed; the reload_plugins below is the final pass.
        if getattr(self, "_switch_t0", None) is not None:
            self._mark_since_switch("switch→conflicts_applied")  # i18n: skip — perftrace marker label
            self._switch_conflicts_done = True
        with span("on_conflicts_ready"):
            self._conflict_data = data
            # These maps now describe the on-disk modlist — arm the move
            # fast-path (see _move_skips_rebuild). Only valid if no newer
            # rebuild was queued meanwhile; the gen check above ensures that.
            self._conflict_maps_current = True
            with span("model.set_filemap_results"):
                # Conflicts + the filemap-derived flag overlays (info=pre-RTX,
                # root=custom root rule) in one dataChanged pass.
                self._modlist_model.set_filemap_results(
                    data.loose_codes, data.bsa_codes,
                    getattr(data, "prertx_mods", set()),
                    getattr(data, "root_rule_mods", set()))
            # Cross-panel highlighting needs the override + owner maps.
            with span("view.set_conflict_maps"):
                self._modlist_view.set_conflict_maps(
                    data.overrides, data.overridden_by,
                    data.bsa_overrides, data.bsa_overridden_by)
            self._plugin_view.set_plugin_owner(data.plugin_owner)
            # Rebuild the filter data + repopulate the filter panel's dynamic lists,
            # then reapply whatever filters are currently active.
            with span("rebuild_filter_data"):
                self._rebuild_filter_data()
            # The deployed filemap changed → the Data tab is stale (rebuilds lazily).
            if hasattr(self, "_data_view"):
                self._data_view.mark_dirty()
            # A mod may have been added/removed → re-evaluate Install vs Reinstall.
            if hasattr(self, "_downloads_view"):
                self._downloads_view.mark_dirty()
            # The deployed file set changed → the Text Files list is stale.
            if hasattr(self, "_text_files_view"):
                self._text_files_view.mark_dirty()
            # A mod may have been removed/added → re-evaluate the Nexus browser's
            # Install/Reinstall buttons (remove goes through save()→conflict rebuild,
            # which is the only signal the Nexus tab gets for a removal).
            nv = getattr(self, "_nexus_view", None)
            if nv is not None:
                nv.refresh_installed()
            # The filemap (staged/deployed file set) changed → framework states may
            # have flipped (e.g. a framework mod toggled, deployed, or removed).
            # Precomputed on the conflict worker (detect_frameworks re-reads
            # filemap.txt — too slow here); bump the gen so an in-flight async
            # detect can't overwrite this fresher result. set_statuses is a
            # no-op when the rows are unchanged, so the repeated refreshes a
            # deploy/restore fires don't rebuild (and flicker) the banner.
            if hasattr(self, "_framework_banner"):
                self._framework_gen += 1
                self._framework_banner.set_statuses(data.framework_statuses)
            # A mod was toggled / added / removed → the footer counts + size are
            # stale (token-guarded, so overlapping walks coalesce).
            with span("refresh_modlist_stats"):
                self._refresh_modlist_stats()
            # The filemap is now fresh → reload the Plugins tab so ESL/master flags
            # resolve against the winning (e.g. patcher-provided light) copies and
            # plugins still deployed by another enabled mod are recovered. Tk parity:
            # gui.py _on_filemap_rebuilt calls _refresh_plugins_tab() here, AFTER the
            # rebuild — reloading earlier (on the toggle) races the stale filemap.
            with span("reload_plugins"):
                self._reload_plugins()
        self._maybe_auto_deploy()

    def _maybe_auto_deploy(self):
        """Auto deploy: if the game has auto_deploy enabled, deploy after every
        successful conflict/filemap rebuild (enable/disable/reorder/install) —
        but NOT when the rebuild was itself triggered by that auto-deploy
        (deploy → _reload_modlist → rebuild → here again), or we'd loop.
        Tk parity: gui.py _on_filemap_rebuilt. Called from _on_conflicts_ready
        AND _on_filemap_light_done (a disabled mod's files must still leave
        the game folder)."""
        if self._auto_deploy_in_progress:
            self._auto_deploy_in_progress = False
        else:
            game = self._gs.game
            if (game is not None and game.is_configured()
                    and getattr(game, "auto_deploy", False)
                    and hasattr(game, "deploy")
                    and not self._deploy_running
                    # Never auto-deploy while an install batch is mid-flight —
                    # the deploy swaps the shared game object's active profile
                    # and races the install worker's path resolution. The
                    # install's own _on_install_done reload re-triggers this.
                    and not getattr(self, "_install_running", False)
                    # Same hazard for a detached-wizard staging job in flight —
                    # _on_wizard_finish_done's reload re-triggers this.
                    and not getattr(self, "_staged_finish_running", False)
                    and not self._staged_finish_queue):
                self._auto_deploy_in_progress = True
                self._on_deploy(silent=True)


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

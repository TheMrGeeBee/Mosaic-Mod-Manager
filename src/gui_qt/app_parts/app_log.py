"""Bottom log panel: build, filters, rendering, log file and logs-folder/tab openers.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QWidget

from gui_qt.theme.theme_qt import _c


class MainWindow:

    # ------------------------------------------------------ log control bar
    def _build_log_bar(self) -> QWidget:
        """Fixed full-width control bar below the log splitter. The 'Log' button
        toggles the (drag-resizable) log text area above. Error/Warning/Clear
        controls only appear while the log is open."""
        bar = QWidget()
        bar.setObjectName("LogBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(12)

        self._log_toggle = self._text_button(self.tr("Log"), compact=True)
        self._log_toggle.clicked.connect(self._toggle_log)
        h.addWidget(self._log_toggle)

        # These only show when the log is open. The Error/Warning entries are
        # clickable toggles: clicking one filters the log to show ONLY lines of
        # that severity; clicking the active one again clears the filter and
        # shows everything again. Only one severity filter can be active at a
        # time. ``_log_filter`` is None (show all), "error", or "warning".
        self._log_filter = None
        self._errors_lbl = QLabel(self.tr("● Errors"))
        self._errors_lbl.setCursor(Qt.PointingHandCursor)
        self._errors_lbl.mousePressEvent = lambda e: self._toggle_log_filter("error")
        h.addWidget(self._errors_lbl)
        self._warnings_lbl = QLabel(self.tr("● Warnings"))
        self._warnings_lbl.setCursor(Qt.PointingHandCursor)
        self._warnings_lbl.mousePressEvent = lambda e: self._toggle_log_filter("warning")
        h.addWidget(self._warnings_lbl)
        self._refresh_log_filter_labels()
        self._open_log_tab_btn = self._text_button(self.tr("Open as tab"), compact=True)
        self._open_log_tab_btn.clicked.connect(self._open_log_tab)
        h.addWidget(self._open_log_tab_btn)
        self._clear_log_btn = self._text_button(self.tr("Clear Log"), compact=True)
        self._clear_log_btn.clicked.connect(self._clear_log)
        h.addWidget(self._clear_log_btn)
        self._open_logs_btn = self._text_button(self.tr("Open Log Folder"), compact=True)
        self._open_logs_btn.clicked.connect(self._open_logs_folder)
        h.addWidget(self._open_logs_btn)

        h.addStretch(1)

        # Changelog button — sits just left of the Nexus info; opens the
        # bundled Changelog.txt as a detachable tab.
        self._changelog_btn = self._text_button(self.tr("Changelog"), compact=True)
        self._changelog_btn.clicked.connect(self._open_changelog_tab)
        h.addWidget(self._changelog_btn)

        # GitHub — opens the project's repo.
        self._github_btn = self._text_button(self.tr("Github"), compact=True)
        self._github_btn.clicked.connect(self._open_github)
        h.addWidget(self._github_btn)

        # Endorse Mosaic's own Nexus listing (site/2139) — the Mosaic-era
        # equivalent of the pre-rename "Endorse AMM" button, which pointed at
        # the original Amethyst Mod Manager's Nexus page. A colored call-to-
        # action rather than a flat text button (previously identical to
        # "Log"/"Changelog"/"Github" and easy to miss) — muted back to a
        # plain text button once already endorsed (see
        # _refresh_endorse_mosaic_style), matching how it toggles off the
        # "here's something to do" affordance rather than continuing to
        # compete for attention. Endorsing is itself a toggle (clicking an
        # already-endorsed button abstains), so the style is re-derived from
        # state on every change, not set once at creation.
        self._endorse_mosaic_btn = QPushButton()
        self._endorse_mosaic_btn.setCursor(Qt.PointingHandCursor)
        self._endorse_mosaic_btn.clicked.connect(self._on_endorse_mosaic)
        self._refresh_endorse_mosaic_style()
        h.addWidget(self._endorse_mosaic_btn)

        # Nexus username at the far right; hover shows API rate-limit usage.
        from gui_qt.nexus.nexus_footer import NexusFooterLabel
        self._nexus_footer = NexusFooterLabel(lambda: getattr(self, "_nexus_api", None))
        h.addWidget(self._nexus_footer)

        self._log_open_widgets = [self._errors_lbl, self._warnings_lbl,
                                  self._open_log_tab_btn, self._clear_log_btn,
                                  self._open_logs_btn]
        for w in self._log_open_widgets:
            w.setVisible(False)
        return bar

    def _log_is_open(self) -> bool:
        return len(self._vsplit.sizes()) > 1 and self._vsplit.sizes()[1] > 0

    def _toggle_log(self):
        if self._log_is_open():
            self._vsplit.setSizes([self._vsplit.height(), 0])     # collapse
        else:
            total = self._vsplit.height()
            self._vsplit.setSizes([total - 180, 180])             # open ~180px
            self._log_view.moveCursor(QTextCursor.End)            # scroll to newest
        self._sync_log_controls()

    def _sync_log_controls(self):
        """Error/Warning/Clear controls are visible only while the log has
        height — whether opened by the button or dragged open/closed (Tk feel)."""
        open_ = self._log_is_open()
        for w in self._log_open_widgets:
            w.setVisible(open_)

    @staticmethod
    def _classify_log_line(line: str) -> str:
        """Return the severity of a log line: 'error', 'warning' or ''.

        Free-form backend messages are classified by keyword so error lines can
        render red and warnings yellow."""
        low = line.lower()
        if any(k in low for k in
               ("error", "failed", "failure", "fatal", "exception",
                "traceback", "[err]", "could not", "cannot ", "can't")):
            return "error"
        if any(k in low for k in ("warn", "[warning]", "deprecat")):
            return "warning"
        return ""

    def _log_line_html(self, line: str, severity: str, timestamp: str = "") -> str:
        """Escaped, colour-tinted HTML for a single log line.

        *timestamp* (already formatted, e.g. ``2026-07-04 18:00:34``) is rendered
        as a dimmed prefix so each on-screen line shows when it was logged."""
        text = escape(line) or "&nbsp;"
        prefix = ""
        if timestamp:
            dim = _c(self._pal, "TEXT_DIM")
            prefix = f'<span style="color:{dim};">[{escape(timestamp)}]</span>  '
        if severity == "error":
            color = _c(self._pal, "TEXT_ERR")
        elif severity == "warning":
            color = _c(self._pal, "TEXT_WARN")
        else:
            return f"<div>{prefix}{text}</div>"
        return f'<div>{prefix}<span style="color:{color};">{text}</span></div>'

    def _init_log_file(self):
        """Create one on-disk log file per session (Tk status_bar parity).

        Lives at ``~/.config/MosaicModManager/logs/mosaic-<ts>.log`` and is
        appended to on every ``_append_log`` call so the file stays in sync with
        the on-screen log."""
        self._log_file = None
        try:
            from datetime import datetime

            from Utils.config_paths import get_logs_dir
            ts = datetime.now().strftime("%m-%d-%y-%H%M%S")
            self._log_file = get_logs_dir() / f"mosaic-{ts}.log"
        except Exception:
            self._log_file = None

    def _write_log_file(self, line: str, timestamp: str):
        """Append one already-stripped line to the session log file (best-effort).

        *timestamp* is the same value shown on-screen so file and panel agree."""
        log_file = getattr(self, "_log_file", None)
        if log_file is None:
            return
        try:
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"[{timestamp}]  {line}\n")
        except OSError:
            pass

    def _append_log(self, message: str):
        """Backend log_fn target — append a line to the log text area.

        Thread-safe: worker threads pass this as their ``log_fn`` (Nexus browser,
        collection detail/reset, installs …). Qt widgets may ONLY be touched on
        the GUI thread — touching ``_log_view`` from a worker is a data race that
        can segfault — so when called off-thread we marshal through the queued
        ``_op_log`` signal instead of writing the widget directly.

        Lines are also retained (with their severity) in ``_log_lines`` so the
        Error/Warning toggles can re-render a filtered view."""
        # Escape surrogate bytes from on-disk file names before the message
        # touches Qt / the log file — a lone surrogate can raise on encode in
        # any sink, and log calls must never be able to crash their caller.
        message = str(message)
        try:
            message.encode("utf-8")
        except UnicodeEncodeError:
            message = message.encode("utf-8", "backslashreplace").decode(
                "utf-8", "replace")
        try:
            from PySide6.QtCore import QThread
            if QThread.currentThread() is not self.thread():
                self._op_log.emit(message)
                return
        except Exception:
            pass
        line = message.rstrip("\n")
        severity = self._classify_log_line(line)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not hasattr(self, "_log_lines"):
            # Bounded so a very long-running session (e.g. a multi-thousand-mod
            # collection install) can't grow this list forever.
            self._log_lines = deque(maxlen=20000)
        self._log_lines.append((line, severity, timestamp))
        # Persist to the session log file (before the display filter, so the file
        # captures everything — Tk status_bar parity).
        self._write_log_file(line, timestamp)
        if not self._line_visible(severity):
            return   # filtered out — retained but not shown
        html = self._log_line_html(line, severity, timestamp)
        for view in (self._log_view, getattr(self, "_log_tab_view", None)):
            if view is None:
                continue
            try:
                view.appendHtml(html)
            except Exception:
                pass

    def _render_log(self):
        """Re-render both log views from ``_log_lines`` honouring the current
        Error/Warning filter toggles."""
        lines = getattr(self, "_log_lines", [])
        html = "".join(
            self._log_line_html(text, sev, ts)
            for text, sev, ts in lines if self._line_visible(sev)
        )
        for view in (self._log_view, getattr(self, "_log_tab_view", None)):
            if view is None:
                continue
            try:
                view.clear()
                if html:
                    view.appendHtml(html)
                view.moveCursor(QTextCursor.End)
            except Exception:
                pass

    def _toggle_log_filter(self, which: str):
        """Filter the log to only *which* severity; clicking the active filter
        again clears it (shows everything). Only one filter at a time."""
        if getattr(self, "_log_filter", None) == which:
            self._log_filter = None   # clicking the active filter clears it
        else:
            self._log_filter = which
        self._refresh_log_filter_labels()
        self._render_log()

    def _refresh_log_filter_labels(self):
        """Colour the filter labels; highlight (bold) the active filter, dim the
        inactive one when a filter is engaged so it reads as an active toggle."""
        active = getattr(self, "_log_filter", None)
        dim = _c(self._pal, "TEXT_DIM")
        err_c = _c(self._pal, "TEXT_ERR")
        warn_c = _c(self._pal, "TEXT_WARN")
        if active == "error":
            self._errors_lbl.setStyleSheet(f"color:{err_c}; font-weight:bold;")
            self._warnings_lbl.setStyleSheet(f"color:{dim};")
        elif active == "warning":
            self._errors_lbl.setStyleSheet(f"color:{dim};")
            self._warnings_lbl.setStyleSheet(f"color:{warn_c}; font-weight:bold;")
        else:
            self._errors_lbl.setStyleSheet(f"color:{err_c};")
            self._warnings_lbl.setStyleSheet(f"color:{warn_c};")

    def _clear_log(self):
        """Clear both the docked log view and the full-screen log tab (if open)."""
        self._log_lines = []
        for view in (self._log_view, getattr(self, "_log_tab_view", None)):
            if view is None:
                continue
            try:
                view.clear()
            except Exception:
                pass

    def _open_logs_folder(self):
        """Open the session logs directory in the system file manager (Tk parity)."""
        try:
            from Utils.config_paths import get_logs_dir
            from Utils.xdg import xdg_open
            logs_dir = get_logs_dir()
            logs_dir.mkdir(parents=True, exist_ok=True)
            xdg_open(logs_dir, log_fn=self._append_log)
        except Exception as exc:
            self._append_log(f"[log] could not open logs folder: {exc}")

    def _open_log_tab(self):
        """Open the log as a full-screen (detachable) tab. It mirrors the docked
        log view: new lines land in both, and Clear Log wipes both."""
        if self._tabs.has_key("log"):
            self._tabs.focus_key("log")
            return
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setObjectName("LogView")
        view.setMaximumBlockCount(20000)   # bound the document too
        self._log_tab_view = view
        # Seed with the existing (filtered, colour-tinted) log contents.
        for text, sev, ts in getattr(self, "_log_lines", []):
            if self._line_visible(sev):
                view.appendHtml(self._log_line_html(text, sev, ts))
        view.moveCursor(QTextCursor.End)
        view.destroyed.connect(
            lambda *_: setattr(self, "_log_tab_view", None))
        self._tabs.open_tab(view, self.tr("Log"), key="log")

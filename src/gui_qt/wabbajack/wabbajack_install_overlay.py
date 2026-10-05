"""In-window progress for a running Wabbajack install.

Two phases, matching ``run_wabbajack_install``: downloading archives (overall
bytes plus the few currently downloading) and building files (directives
applied / total). Cancel asks the worker to stop; once the install ends
:meth:`finish` swaps in a summary and a Close button. Not dismissable with
Esc while running -- an install shouldn't vanish behind the user's back.

Every update method is a UI-thread slot target; the controller connects
them to its signals, never calls them from the worker.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QWidget

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme.theme_qt import _c, active_palette
from Utils.collections.collection_manifest import fmt_size

_MAX_ACTIVE_SHOWN = 4


class WabbajackInstallOverlay(OverlayBase):
    CARD_W = 560
    CARD_H = 380

    def __init__(self, host: QWidget, title: str, total_archives: int, total_bytes: int,
                 on_cancel, on_close=None):
        super().__init__(host, on_done=lambda _r: on_close() if on_close else None)
        self._on_cancel = on_cancel
        self._total_archives = total_archives
        self._total_bytes = max(1, total_bytes)
        self._active: "dict[str, list]" = {}   # hash -> [name, cur, total]
        self._done_bytes = 0
        self._finished_archives = 0
        self._running = True
        p = active_palette()
        self._p = p
        _card, v = self._make_card("WabbajackInstallCard")

        t = QLabel(title)
        t.setWordWrap(True)
        t.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(t)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        v.addWidget(self._status)

        self._dl_label = QLabel("")
        self._dl_label.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        v.addWidget(self._dl_label)
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 1000)
        self._dl_bar.setTextVisible(False)
        v.addWidget(self._dl_bar)
        self._active_label = QLabel("")
        self._active_label.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        v.addWidget(self._active_label)

        self._build_label = QLabel(self.tr("Building files: waiting for downloads"))
        self._build_label.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        v.addWidget(self._build_label)
        self._build_bar = QProgressBar()
        self._build_bar.setRange(0, 1)
        self._build_bar.setValue(0)
        self._build_bar.setTextVisible(False)
        v.addWidget(self._build_bar)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.hide()
        v.addWidget(self._summary)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self._cancel = QPushButton(self.tr("Cancel"))
        self._cancel.setObjectName("FormButton")
        self._cancel.setCursor(Qt.PointingHandCursor)
        self._cancel.clicked.connect(self._cancel_clicked)
        bar.addWidget(self._cancel)
        self._close = QPushButton(self.tr("Close"))
        self._close.setObjectName("PrimaryButton")
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.clicked.connect(lambda: self._finish(None))
        self._close.hide()
        bar.addWidget(self._close)
        v.addLayout(bar)

        self._render_downloads()
        self._present()

    @classmethod
    def show_over(cls, host, title, total_archives, total_bytes, on_cancel, on_close=None):
        top = host.window() if host is not None else None
        return cls(top or host, title, total_archives, total_bytes, on_cancel, on_close)

    # -- updates (UI thread) ----------------------------------------------------
    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def dl_start(self, archive_hash: str, name: str, size: int) -> None:
        self._active[archive_hash] = [name, 0, size]
        self._render_downloads()

    def dl_progress(self, archive_hash: str, cur: int, total: int) -> None:
        entry = self._active.get(archive_hash)
        if entry is not None:
            entry[1], entry[2] = cur, total or entry[2]
            self._render_downloads()

    def dl_finish(self, archive_hash: str, ok: bool) -> None:
        entry = self._active.pop(archive_hash, None)
        if entry is not None and ok:
            self._done_bytes += entry[2] or entry[1]
        self._finished_archives += 1
        self._render_downloads()

    def build_progress(self, done: int, total: int) -> None:
        self._build_bar.setRange(0, max(1, total))
        self._build_bar.setValue(done)
        self._build_label.setText(self.tr("Building files: {0} of {1}").format(done, total))

    def finish(self, summary: str, ok: bool) -> None:
        self._running = False
        color = _c(self._p, "TEXT_OK_BRIGHT" if ok else "TEXT_WARN_BRIGHT")
        self._summary.setStyleSheet(f"color:{color}; font-size:13px;")
        self._summary.setText(summary)
        self._summary.show()
        self._cancel.hide()
        self._close.show()
        self._close.setFocus()

    # -- internals --------------------------------------------------------------
    def _render_downloads(self) -> None:
        in_flight = sum(e[1] for e in self._active.values())
        frac = min(1.0, (self._done_bytes + in_flight) / self._total_bytes)
        self._dl_bar.setValue(int(frac * 1000))
        self._dl_label.setText(self.tr("Downloads: {0} of {1} archives ({2} of {3})").format(
            min(self._finished_archives, self._total_archives), self._total_archives,
            fmt_size(self._done_bytes + in_flight), fmt_size(self._total_bytes)))
        rows = []
        for name, cur, total in list(self._active.values())[:_MAX_ACTIVE_SHOWN]:
            pct = f"{int(100 * cur / total)}%" if total else fmt_size(cur)
            rows.append(f"{name} — {pct}")
        extra = len(self._active) - _MAX_ACTIVE_SHOWN
        if extra > 0:
            rows.append(self.tr("…and {0} more").format(extra))
        self._active_label.setText("\n".join(rows))

    def _cancel_clicked(self) -> None:
        self._cancel.setEnabled(False)
        self._cancel.setText(self.tr("Cancelling…"))
        if self._on_cancel:
            self._on_cancel()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._running:
            return
        if event.key() == Qt.Key_Escape:
            self._finish(None)
            return
        super().keyPressEvent(event)

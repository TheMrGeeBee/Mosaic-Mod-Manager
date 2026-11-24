"""In-window progress for checking an installed Wabbajack profile's files.

One phase: files checked / total, by bytes. Cancel asks the worker to stop;
once the check ends :meth:`finish` shows the summary, and when files didn't
match, a "Repair" button that deletes them and rebuilds them (the controller
decides what that does). Every update method is a UI-thread slot target.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme.theme_qt import _c, active_palette
from Utils.collections.collection_manifest import fmt_size


class WabbajackVerifyOverlay(OverlayBase):
    CARD_W = 560
    CARD_H = 300

    def __init__(self, host, title: str, on_cancel, on_repair, on_close=None):
        super().__init__(host, on_done=lambda _r: on_close() if on_close else None)
        self._on_cancel = on_cancel
        self._on_repair = on_repair
        self._running = True
        p = active_palette()
        self._p = p
        _card, v = self._make_card("WabbajackVerifyCard")

        t = QLabel(title)
        t.setWordWrap(True)
        t.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(t)
        self._label = QLabel(self.tr("Reading the modlist…"))
        self._label.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        v.addWidget(self._label)
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        v.addWidget(self._bar)
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
        self._repair = QPushButton("")
        self._repair.setObjectName("FormButton")
        self._repair.setCursor(Qt.PointingHandCursor)
        self._repair.setToolTip(self.tr(
            "Delete the files that don't match and build them again from the modlist. "
            "Changes the game or you made to those files are lost."))
        self._repair.clicked.connect(self._repair_clicked)
        self._repair.hide()
        bar.addWidget(self._repair)
        self._close = QPushButton(self.tr("Close"))
        self._close.setObjectName("PrimaryButton")
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.clicked.connect(lambda: self._finish(None))
        self._close.hide()
        bar.addWidget(self._close)
        v.addLayout(bar)
        self._present()

    @classmethod
    def show_over(cls, host, title, on_cancel, on_repair, on_close=None):
        top = host.window() if host is not None else None
        return cls(top or host, title, on_cancel, on_repair, on_close)

    def progress(self, files: int, total_files: int, done: int, total: int) -> None:
        # Updates come from several worker threads and can arrive out of order.
        if total_files != getattr(self, "_total_files", None):
            self._total_files, self._best = total_files, (0, 0)
        if (files, done) <= self._best:
            return
        self._best = (files, done)
        self._bar.setValue(int(1000 * done / total) if total else 0)
        self._label.setText(self.tr("Checked {0} of {1} files ({2} of {3})").format(
            files, total_files, fmt_size(done), fmt_size(total)))

    def finish(self, summary: str, ok: bool, repairable: int = 0) -> None:
        self._running = False
        if self._summary.isHidden() and getattr(self, "_total_files", None):
            self._bar.setValue(1000)
        color = _c(self._p, "TEXT_OK_BRIGHT" if ok else "TEXT_WARN_BRIGHT")
        self._summary.setStyleSheet(f"color:{color}; font-size:13px;")
        self._summary.setText(summary)
        self._summary.show()
        self._cancel.hide()
        if repairable and self._on_repair is not None:
            self._repair.setText(self.tr("Repair {0} file(s)").format(repairable))
            self._repair.show()
        self._close.show()
        self._close.setFocus()

    def _cancel_clicked(self) -> None:
        self._cancel.setEnabled(False)
        self._cancel.setText(self.tr("Cancelling…"))
        if self._on_cancel:
            self._on_cancel()

    def _repair_clicked(self) -> None:
        on_repair = self._on_repair
        self._finish(None)
        if on_repair is not None:
            on_repair()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._running:
            return
        if event.key() == Qt.Key_Escape:
            self._finish(None)
            return
        super().keyPressEvent(event)

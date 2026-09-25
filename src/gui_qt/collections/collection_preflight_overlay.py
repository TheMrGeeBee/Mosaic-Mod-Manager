"""Collection preflight overlay.

Shown before a collection install when a check needs the user's attention: a card
listing each check (✓ / ✗ / !) with, when Mosaic can fix a blocker itself, a
single "Fix and continue" button. The overlay only renders; the app owns the
worker that performs the fix and reports back through :meth:`set_busy`,
:meth:`set_error` and :meth:`set_checks` (all on the UI thread).

``on_done(result)``: ``"continue"`` when the user proceeds (all blockers cleared,
or only warnings and they chose to go on), ``None`` when they cancel.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme.theme_qt import _c, active_palette


class PreflightOverlay(OverlayBase):
    CARD_W = 560
    CARD_H = 420
    MIN_H = 240
    ESC_RESULT = None

    def __init__(self, host, checks, on_fix, on_done):
        super().__init__(host, on_done=on_done)
        self._p = active_palette()
        self._on_fix = on_fix
        self._busy = False
        _card, v = self._make_card("PreflightCard")

        self._title = QLabel("")
        self._title.setStyleSheet(
            f"color:{_c(self._p, 'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(self._title)

        self._body = QLabel("")
        self._body.setTextFormat(Qt.RichText)
        self._body.setWordWrap(True)
        self._body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._body.setStyleSheet(f"color:{_c(self._p, 'TEXT_DIM')}; font-size:13px;")
        v.addWidget(self._body, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{_c(self._p, 'TEXT_MAIN')}; font-size:13px;")
        v.addWidget(self._status)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self._cancel = QPushButton(self.tr("Cancel"))
        self._cancel.setObjectName("FormButton")
        self._cancel.setCursor(Qt.PointingHandCursor)
        self._cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(self._cancel)
        self._go = QPushButton("")
        self._go.setObjectName("PrimaryButton")
        self._go.setCursor(Qt.PointingHandCursor)
        self._go.clicked.connect(self._on_go)
        bar.addWidget(self._go)
        v.addLayout(bar)

        self._mode = "close"
        self.set_checks(checks)
        self._present()

    @classmethod
    def show_over(cls, host, checks, on_fix, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, checks, on_fix, on_done)

    # ---- rendering ------------------------------------------------------------
    def _row(self, check) -> str:
        if check.ok:
            mark, color = "✓", "#5fb35f"
        elif check.blocking:
            mark, color = "✗", "#e05555"
        else:
            mark, color = "!", "#e5a640"
        detail = (f'<br><span style="color:{_c(self._p, "TEXT_DIM")}">'
                  f"{html.escape(check.detail)}</span>") if check.detail else ""
        return (f'<p style="margin:0 0 8px 0"><span style="color:{color}">{mark}</span> '
                f"<b>{html.escape(check.title)}</b>{detail}</p>")

    def set_checks(self, checks) -> None:
        """Re-render after (re)running the checks and pick the primary action."""
        from Utils.collections.collection_preflight import (
            blocking_failures, fixable, warnings)
        checks = list(checks)
        blockers, fixes, warns = blocking_failures(checks), fixable(checks), warnings(checks)
        self._body.setText("".join(self._row(c) for c in checks))
        if fixes and len(fixes) == len(blockers):
            self._title.setText(self.tr("Before installing: the game needs a change"))
            self._mode, label = "fix", self.tr("Fix and continue")
        elif blockers:
            self._title.setText(self.tr("Before installing: this needs your attention"))
            self._mode, label = "close", ""
        else:
            self._title.setText(self.tr("Before installing"))
            self._mode, label = "continue", self.tr("Continue anyway")
        self._go.setText(label)
        self._go.setVisible(bool(label))
        self._go.setEnabled(not self._busy)
        self._cancel.setText(self.tr("Close") if self._mode == "close" else self.tr("Cancel"))
        self._cancel.setEnabled(not self._busy)

    def set_busy(self, text: str) -> None:
        self._busy = True
        self._status.setStyleSheet(f"color:{_c(self._p, 'TEXT_MAIN')}; font-size:13px;")
        self._status.setText(text)
        self._go.setEnabled(False)
        self._cancel.setEnabled(False)

    def set_error(self, text: str) -> None:
        self._busy = False
        self._status.setStyleSheet("color:#e05555; font-size:13px;")
        self._status.setText(text)
        self._go.setEnabled(True)
        self._cancel.setEnabled(True)

    def close_ok(self) -> None:
        self._finish("continue")

    # ---- actions ---------------------------------------------------------------
    def _on_go(self) -> None:
        if self._busy:
            return
        if self._mode == "continue":
            self._finish("continue")
        elif self._mode == "fix":
            self._status.setText("")
            self._on_fix(self)

    def keyPressEvent(self, event):
        if self._busy and event.key() == Qt.Key_Escape:
            return                       # a fix is running — don't abandon it half-way
        super().keyPressEvent(event)

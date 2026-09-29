"""Generic winetricks-verb installer wizard — one button's worth of UI over
Utils.wine_proton.protontricks.install_winetricks_verb.

Reusable across any game/verb via WizardTool.extra:
  verb    — the winetricks verb to install (e.g. "dotnet472")
  label   — human-readable name shown in the header/status text
  timeout — per-attempt subprocess timeout in seconds (default 1800 — legacy
            .NET Framework verbs chain several sequential installers and can
            run long)
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
)

from gui_qt.theme.theme_qt import active_palette, _c, button_qss, ok_text, err_text
from gui_qt.safe_emit import safe_emit

if TYPE_CHECKING:
    from Games.base_game import BaseGame


class WinetricksVerbView(QWidget):
    """Install a single winetricks verb into the game's Proton prefix."""

    _status_sig = Signal(str, str)
    _done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 verb: str = "", label: str = "", timeout: int = 1800):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._verb = verb
        self._label = label or verb
        self._timeout = timeout
        self._closing = False

        def _guard(fn):
            return lambda *a: None if self._closing else fn(*a)

        self._status_sig.connect(_guard(lambda t, c: self._set_status(t, c)))
        self._done_sig.connect(_guard(self._on_done))

        self.setObjectName("WinetricksVerbView")
        self._build()
        self._start()

    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Install {0} — {1}").format(self._label, self._game.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = QPushButton(self.tr("✕ Close"))
        close.setCursor(Qt.PointingHandCursor)
        close.setStyleSheet(button_qss("BTN_DANGER", padding="5px 12px"))
        close.clicked.connect(self._finish)
        hb.addWidget(close)
        v.addWidget(bar)

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(20, 16, 20, 16)
        bv.setSpacing(10)
        bv.addStretch(1)
        self._status = QLabel(self.tr(
            "Installing {0} into the game's Proton prefix…\n"
            "This can take several minutes.").format(self._label))
        self._status.setAlignment(Qt.AlignHCenter)
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
        bv.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)
        self._bar.setTextVisible(False)
        bv.addWidget(self._bar)
        self._done_btn = QPushButton(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.setStyleSheet(button_qss("BTN_SUCCESS"))
        self._done_btn.clicked.connect(self._finish)
        bv.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        bv.addStretch(1)
        v.addWidget(body, 1)

    def _set_status(self, text: str, color: str):
        self._status.setStyleSheet(f"color:{color};" if color else f"color:{_c(active_palette(),'TEXT_MAIN')};")
        self._status.setText(text)

    def _start(self):
        game = self._game
        verb = self._verb
        label = self._label
        timeout = self._timeout

        def worker():
            try:
                from Utils.wine_proton.protontricks import install_winetricks_verb
                ok = install_winetricks_verb(
                    game, verb, log_fn=lambda m: self._log(str(m)), timeout=timeout)
                if ok:
                    safe_emit(self._status_sig,
                        self.tr("{0} installed successfully.\n\nClick Done to close.")
                        .format(label), ok_text())
                else:
                    safe_emit(self._status_sig,
                        self.tr("{0} install failed — see the log for details.")
                        .format(label), err_text())
                safe_emit(self._done_sig, ok)
            except Exception as exc:
                self._log(f"Wizard error: {exc}")
                safe_emit(self._status_sig, self.tr("Error: {0}").format(exc), err_text())
                safe_emit(self._done_sig, False)

        threading.Thread(target=worker, daemon=True, name="winetricks-verb").start()

    def _on_done(self, _ok: bool):
        self._bar.setVisible(False)
        self._done_btn.setEnabled(True)

    def _finish(self):
        if self._closing:
            return
        self._closing = True
        self._on_close_cb()

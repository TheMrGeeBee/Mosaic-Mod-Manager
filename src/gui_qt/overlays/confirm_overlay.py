"""Generic borderless in-window confirmation overlay.

A dimmed child overlay (see gui_qt/overlay_base.py) with a centered card:
title, body text, and Confirm / Cancel buttons. ``on_done(True)`` on confirm,
``on_done(False)`` on cancel / Esc.

Pass ``cancel_label=None`` for a single-button message card (OK-only) — the
in-app replacement for ``QMessageBox.warning``/``critical``/``information``.
Pass ``alt_label`` for a third button between them; it sends ``on_done("alt")``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme.theme_qt import _c, active_palette, contrast_text


class ConfirmOverlay(OverlayBase):
    CARD_W = 480
    CARD_H = 240
    MIN_H = 180
    ESC_RESULT = False

    _NO_CANCEL = object()  # sentinel: distinguish "OK-only" from "use default"

    def __init__(self, host: QWidget, title: str, body: str, on_done,
                 confirm_label: str | None = None,
                 cancel_label=_NO_CANCEL,
                 danger: bool = True,
                 card_h: int | None = None,
                 alt_label: str | None = None):
        super().__init__(host, on_done=on_done, card_h=card_h)
        p = active_palette()

        _card, v = self._make_card(
            "ConfirmCard",
            extra_qss=(
                f" #DangerButton {{ background:{_c(p,'BTN_DANGER')};"
                f" color:{contrast_text(_c(p,'BTN_DANGER'))};"
                f" border:none; border-radius:4px; padding:6px 14px;"
                f" font-weight:600; }}"
                f" #DangerButton:hover {{ background:{_c(p,'BTN_DANGER_HOV')}; }}"
                f" QPushButton:focus {{ border:2px solid {_c(p,'ACCENT')}; }}"))

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        body_lbl = QLabel(body)
        body_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        body_lbl.setWordWrap(True)
        v.addWidget(body_lbl)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self._buttons: list[QPushButton] = []
        cancel = None
        if cancel_label is not None:
            cancel_text = (self.tr("Cancel")
                           if cancel_label is self._NO_CANCEL else cancel_label)
            cancel = QPushButton(cancel_text)
            cancel.setObjectName("FormButton")
            cancel.setCursor(Qt.PointingHandCursor)
            cancel.clicked.connect(lambda: self._finish(False))
            bar.addWidget(cancel)
            self._buttons.append(cancel)
        if alt_label is not None:
            # Optional middle choice: on_done receives the string "alt".
            alt = QPushButton(alt_label)
            alt.setObjectName("FormButton")
            alt.setCursor(Qt.PointingHandCursor)
            alt.clicked.connect(lambda: self._finish("alt"))
            bar.addWidget(alt)
            self._buttons.append(alt)
        confirm = QPushButton(
            confirm_label if confirm_label is not None else self.tr("Remove"))
        confirm.setObjectName("DangerButton" if danger else "PrimaryButton")
        confirm.setCursor(Qt.PointingHandCursor)
        confirm.clicked.connect(lambda: self._finish(True))
        bar.addWidget(confirm)
        self._buttons.append(confirm)
        v.addLayout(bar)

        # Keyboard: a destructive prompt starts on Cancel so a stray Enter
        # can't delete anything; Tab / arrows move between the buttons and
        # Enter presses the focused one.
        (cancel if danger and cancel is not None else confirm).setFocus()
        self._present()

    # -- keyboard -------------------------------------------------------------
    def _focus_step(self, step: int) -> None:
        """Move focus to the next/previous button, wrapping around."""
        if not self._buttons:
            return
        current = self.focusWidget()
        i = self._buttons.index(current) if current in self._buttons else -1
        self._buttons[(i + step) % len(self._buttons)].setFocus(
            Qt.TabFocusReason)

    def focusNextPrevChild(self, next_: bool) -> bool:
        # Keep Tab inside the card; the default would walk into the widgets
        # behind the overlay.
        self._focus_step(1 if next_ else -1)
        return True

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Right, Qt.Key_Down):
            self._focus_step(1)
        elif key in (Qt.Key_Left, Qt.Key_Up):
            self._focus_step(-1)
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            focused = self.focusWidget()
            if focused in self._buttons:
                focused.click()
            elif self._buttons:
                self._buttons[-1].click()
        else:
            super().keyPressEvent(event)

    @classmethod
    def show_over(cls, host, title, body, on_done, **kw):
        top = host.window() if host is not None else None
        return cls(top or host, title, body, on_done, **kw)

    @classmethod
    def show_message(cls, host, title, body, on_done=None, ok_label=None, **kw):
        """OK-only message card (QMessageBox.warning/critical replacement)."""
        from PySide6.QtCore import QCoreApplication
        if ok_label is None:
            ok_label = QCoreApplication.translate("ConfirmOverlay", "OK")
        return cls.show_over(host, title, body, on_done,
                             confirm_label=ok_label, cancel_label=None,
                             danger=False, **kw)

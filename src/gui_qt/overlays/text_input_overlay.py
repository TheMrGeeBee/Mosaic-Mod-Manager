"""Generic borderless in-window text-input overlay.

A dimmed child overlay (see gui_qt/overlay_base.py) with a centered card:
title, prompt, a line edit and Cancel / OK buttons. ``on_done(text)`` on
confirm, ``on_done(None)`` on cancel / Esc / backdrop click. Replaces the
native ``QInputDialog.getText`` / ``getInt`` prompts; pass a ``QIntValidator``
for numeric input.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QLineEdit, QPushButton

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme.theme_qt import active_palette, _c


class TextInputOverlay(OverlayBase):
    CARD_W = 480
    CARD_H = 190
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, title: str, prompt: str, on_done,
                 initial: str = "", ok_label: str = "OK", validator=None,
                 extra_label: str = "", on_extra=None):
        """*extra_label*/*on_extra* — an optional secondary action button
        (e.g. "Fetch name from Nexus") shown left-aligned in the button bar,
        separate from Cancel/OK. ``on_extra`` is a no-arg callback invoked on
        click; it does not close the overlay — use ``set_text()`` to update
        the field in place once it has a result."""
        super().__init__(host, on_done=on_done)
        p = active_palette()

        _card, v = self._make_card("TextInputCard")

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        prompt_lbl = QLabel(prompt)
        prompt_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        prompt_lbl.setWordWrap(True)
        v.addWidget(prompt_lbl)

        self._edit = QLineEdit()
        if validator is not None:
            self._edit.setValidator(validator)
        self._edit.setText(initial)
        self._edit.selectAll()
        self._edit.returnPressed.connect(self._confirm)
        v.addWidget(self._edit)
        v.addStretch(1)

        bar = QHBoxLayout()
        if extra_label and on_extra is not None:
            extra = QPushButton(extra_label)
            extra.setObjectName("FormButton")
            extra.setCursor(Qt.PointingHandCursor)
            # QPushButton.clicked emits a bool `checked` arg. on_extra is
            # documented as a no-arg callback, but a caller's closure often
            # declares a same-named DEFAULTED parameter for its own capture
            # purposes (e.g. `def _do_fetch(_name=mod_name): ...`) - Qt sees
            # that parameter slot is acceptable and passes `checked`
            # positionally into it, silently clobbering the intended default
            # (observed: mod_name ended up as the bool False, crashing on
            # `staging / mod_name`). The lambda absorbs the bool so on_extra
            # is always actually called with zero arguments.
            extra.clicked.connect(lambda _checked=False: on_extra())
            bar.addWidget(extra)
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        ok = QPushButton(ok_label)
        ok.setObjectName("PrimaryButton")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._confirm)
        bar.addWidget(ok)
        v.addLayout(bar)

        self._present()
        self._edit.setFocus()

    @classmethod
    def show_over(cls, host, title, prompt, on_done, **kw):
        top = host.window() if host is not None else None
        return cls(top or host, title, prompt, on_done, **kw)

    def set_text(self, value: str) -> None:
        """Replace the field's current contents (e.g. after an async fetch)."""
        self._edit.setText(value)
        self._edit.selectAll()
        self._edit.setFocus()

    # -- internals ----------------------------------------------------------
    def _confirm(self):
        self._finish(self._edit.text())

"""Modhold account wizard: connect, disconnect, or paste a key.

"Connect" opens modholdmods.com, where the player signs in and says yes once;
the key comes back to a one-shot localhost listener (Modhold/modhold_connect.py)
and is stored in the system keyring (or an encrypted file). Mosaic never sees a
Modhold password. A Pass lets the account see and download alpha/beta and paid
mods, which Modhold decides on its side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from Modhold import modhold_key
from Modhold.modhold_connect import ModholdConnect
from wizards_qt._view_base import WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_GREEN_OK = "#5fb95f"
_RED_ERR = "#d65c5c"


class ModholdSettingsView(WizardViewBase):
    """Connect Mosaic to a Modhold account."""

    _key_sig = Signal(str, str)     # (key, user) from the listener thread
    _error_sig = Signal(str)        # listener failure / timeout

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx, title=self.tr("Modhold account"))
        self._connect: ModholdConnect | None = None
        self._key_sig.connect(self._guard(self._on_key))
        self._error_sig.connect(self._guard(self._on_error))
        self._stack.addWidget(self._build_page())
        self._refresh()

    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Modhold account"))
        self._make_note(lay, self.tr(
            "Connect your Modhold account to download mods and loadouts. You sign in "
            "on modholdmods.com and approve once; Mosaic never sees your password.\n\n"
            "With a Modhold Pass you also get alpha/beta and paid mods. The key is "
            "stored securely (system keyring, or an encrypted file when no keyring "
            "is available) and you can remove it at any time."))

        self._status = self._make_status(lay)

        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 8, 0, 0)
        rh.setSpacing(8)
        rh.addStretch(1)
        self._disconnect_btn = QPushButton(self.tr("Disconnect"))
        self._disconnect_btn.setCursor(Qt.PointingHandCursor)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        rh.addWidget(self._disconnect_btn)
        self._cancel_btn = QPushButton(self.tr("Cancel"))
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.clicked.connect(self._on_cancel)
        rh.addWidget(self._cancel_btn)
        self._connect_btn = self._accent_btn(self.tr("Connect to Modhold"))
        self._connect_btn.clicked.connect(self._on_connect)
        rh.addWidget(self._connect_btn)
        rh.addStretch(1)
        lay.addWidget(row)

        lay.addSpacing(12)
        self._make_note(lay, self.tr(
            "Or paste a key from your Modhold account if the browser flow "
            "does not work for you."))
        self._entry = QLineEdit()
        self._entry.setPlaceholderText(self.tr("Modhold key (mhm_…)"))
        self._entry.setEchoMode(QLineEdit.Password)
        self._entry.setMinimumWidth(420)
        lay.addWidget(self._entry, 0, Qt.AlignHCenter)
        save_row = QWidget()
        sh = QHBoxLayout(save_row)
        sh.setContentsMargins(0, 4, 0, 0)
        sh.addStretch(1)
        self._save_btn = self._accent_btn(self.tr("Save key"))
        self._save_btn.clicked.connect(self._on_save_pasted)
        sh.addWidget(self._save_btn)
        sh.addStretch(1)
        lay.addWidget(save_row)
        lay.addStretch(1)
        return page

    # ---- state ---------------------------------------------------------------
    def _waiting(self) -> bool:
        return self._connect is not None

    def _refresh(self):
        waiting = self._waiting()
        has_key = bool(modhold_key.load_modhold_key())
        self._connect_btn.setEnabled(not waiting)
        self._cancel_btn.setVisible(waiting)
        self._disconnect_btn.setVisible(has_key and not waiting)
        self._save_btn.setEnabled(not waiting)
        if waiting:
            self._set_status(self._status,
                             self.tr("Waiting for you to approve in the browser…"))
        elif has_key:
            user = modhold_key.load_modhold_user()
            text = (self.tr("Connected as {0}.").format(user) if user
                    else self.tr("Connected."))
            self._set_status(self._status, text, _GREEN_OK)
        else:
            self._set_status(self._status, self.tr("Not connected."))

    # ---- actions -------------------------------------------------------------
    def _on_connect(self):
        if self._waiting():
            return
        self._connect = ModholdConnect(
            on_key=lambda k, u: safe_emit(self._key_sig, k, u),
            on_error=lambda m: safe_emit(self._error_sig, m))
        try:
            url = self._connect.start()
        except OSError as e:
            self._connect = None
            self._set_status(self._status,
                             self.tr("Could not start the sign-in listener: {0}").format(e),
                             _RED_ERR)
            return
        self._refresh()
        self._open_url(url)

    def _on_cancel(self):
        if self._connect is not None:
            self._connect.cancel()
            self._connect = None
        self._refresh()

    def _on_key(self, key: str, user: str):
        self._connect = None
        try:
            modhold_key.save_modhold_key(key, user)
        except Exception as e:
            self._set_status(self._status,
                             self.tr("Could not save the key: {0}").format(e), _RED_ERR)
            return
        self._log("Modhold: connected" + (f" as {user}." if user else "."))
        self._refresh()

    def _on_error(self, message: str):
        self._connect = None
        self._refresh()
        self._set_status(self._status, message, _RED_ERR)

    def _on_save_pasted(self):
        key = self._entry.text().strip()
        if not key:
            self._set_status(self._status, self.tr("Enter a key first."), _RED_ERR)
            return
        try:
            modhold_key.save_modhold_key(key, "")
        except Exception as e:
            self._set_status(self._status,
                             self.tr("Could not save the key: {0}").format(e), _RED_ERR)
            return
        self._entry.clear()
        self._log("Modhold: key saved.")
        self._refresh()

    def _on_disconnect(self):
        try:
            modhold_key.clear_modhold_key()
        except Exception as e:
            self._set_status(self._status,
                             self.tr("Could not remove the key: {0}").format(e), _RED_ERR)
            return
        self._log("Modhold: disconnected.")
        self._refresh()

    def _finish(self):
        if self._connect is not None:
            self._connect.cancel()
            self._connect = None
        super()._finish()

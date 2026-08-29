"""mod.io API key wizard (Baldur's Gate 3) — Qt port of wizards/modio_settings.py.

Paste the free read-only mod.io API key, test it against the API and store
it (system keyring / encrypted file).  The mod.io logic lives in the BG3
game folder (Games/Baldur's Gate 3/modio_*.py); that folder isn't
importable by dotted path (space in the name), so modules load by file path.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_KEY_URL = "https://mod.io/me/access"
_GREEN_OK = "#5fb95f"
_RED_ERR = "#d65c5c"


def _load_bg3_modio(stem: str):
    """Load a Games/Baldur's Gate 3/<stem>.py module by file path."""
    mod_name = f"{stem}_bg3"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    bg3_dir = (Path(__file__).resolve().parent.parent
               / "Games" / "Baldur's Gate 3")
    spec = importlib.util.spec_from_file_location(mod_name, str(bg3_dir / f"{stem}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class ModioSettingsView(WizardViewBase):
    """Enter, test and store the mod.io API key; optionally log in with
    email too (needed for Like — the read-only key can't do that)."""

    _save_done_sig = Signal(str, bool, str)   # (key, ok, err)
    _email_sent_sig = Signal(bool, str)       # (ok, err)
    _login_done_sig = Signal(bool, str)       # (ok, err)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx, title=self.tr("mod.io API Key"))
        self._busy = False
        self._login_busy = False
        self._modio_key = _load_bg3_modio("modio_key")
        self._modio_oauth = _load_bg3_modio("modio_oauth")

        self._save_done_sig.connect(self._guard(self._save_done))
        self._email_sent_sig.connect(self._guard(self._email_sent))
        self._login_done_sig.connect(self._guard(self._login_done))
        self._stack.addWidget(self._build_page())

    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("mod.io API Key"))
        self._make_note(lay, (
            self.tr("Paste your mod.io read-only API key to enable update checks\n"
            "for Baldur's Gate 3 mods installed manually from mod.io.\n\n"
            "The key is read-only and stored securely (system keyring,\n"
            "or an encrypted file when no keyring is available).")))

        link = QPushButton(self.tr("Get my API key (mod.io)"))
        link.setCursor(Qt.PointingHandCursor)
        link.clicked.connect(lambda: self._open_url(_KEY_URL))
        lay.addWidget(link, 0, Qt.AlignHCenter)

        self._entry = QLineEdit()
        self._entry.setPlaceholderText(self.tr("mod.io API key"))
        self._entry.setMinimumWidth(420)
        try:
            existing = self._modio_key.load_modio_key()
        except Exception:
            existing = ""
        if existing:
            self._entry.setText(existing)
        lay.addWidget(self._entry, 0, Qt.AlignHCenter)

        self._status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        clear = QPushButton(self.tr("Clear key"))
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(self._on_clear)
        rh.addWidget(clear)
        self._save_btn = self._accent_btn(self.tr("Test && Save"))
        self._save_btn.clicked.connect(self._on_save)
        rh.addWidget(self._save_btn)
        rh.addStretch(1)
        lay.addWidget(row)

        lay.addSpacing(12)
        self._make_note(lay, self.tr(
            "Optional: log in with email to Like mods on mod.io from the app.\n"
            "Not needed for update checks — only for Like."))

        self._login_email = QLineEdit()
        self._login_email.setPlaceholderText(self.tr("Email address"))
        self._login_email.setMinimumWidth(420)
        lay.addWidget(self._login_email, 0, Qt.AlignHCenter)

        email_row = QWidget()
        eh = QHBoxLayout(email_row); eh.setContentsMargins(0, 4, 0, 0); eh.setSpacing(8)
        eh.addStretch(1)
        self._send_code_btn = self._accent_btn(self.tr("Send code"))
        self._send_code_btn.clicked.connect(self._on_send_code)
        eh.addWidget(self._send_code_btn)
        eh.addStretch(1)
        lay.addWidget(email_row)

        self._login_code = QLineEdit()
        self._login_code.setPlaceholderText(self.tr("Code from email"))
        self._login_code.setMinimumWidth(420)
        lay.addWidget(self._login_code, 0, Qt.AlignHCenter)

        login_row = QWidget()
        lh = QHBoxLayout(login_row); lh.setContentsMargins(0, 4, 0, 0); lh.setSpacing(8)
        lh.addStretch(1)
        self._logout_btn = QPushButton(self.tr("Log out"))
        self._logout_btn.setCursor(Qt.PointingHandCursor)
        self._logout_btn.clicked.connect(self._on_logout)
        lh.addWidget(self._logout_btn)
        self._verify_btn = self._accent_btn(self.tr("Verify"))
        self._verify_btn.clicked.connect(self._on_verify_code)
        lh.addWidget(self._verify_btn)
        lh.addStretch(1)
        lay.addWidget(login_row)

        self._login_status = self._make_status(lay)
        self._refresh_login_status()
        return page

    def _refresh_login_status(self):
        tokens = None
        try:
            tokens = self._modio_oauth.load_modio_tokens()
        except Exception:
            tokens = None
        if tokens:
            self._set_status(self._login_status,
                             self.tr("Logged in to mod.io — Like is available."),
                             _GREEN_OK)
        else:
            self._set_status(self._login_status, "")

    def _set_result(self, text: str, ok: "bool | None" = None):
        color = ""
        if ok is True:
            color = _GREEN_OK
        elif ok is False:
            color = _RED_ERR
        self._set_status(self._status, text, color)

    # ---- actions ----------------------------------------------------------------
    def _on_save(self):
        if self._busy:
            return
        key = self._entry.text().strip()
        if not key:
            self._set_result(self.tr("Enter a key first."), ok=False)
            return
        self._busy = True
        self._save_btn.setEnabled(False)
        self._set_result(self.tr("Testing key…"))

        def worker():
            ok = False
            err = ""
            try:
                modio_api = _load_bg3_modio("modio_api")
                ok = modio_api.ModioAPI(key).test_key()
            except Exception as e:
                err = str(e)
            safe_emit(self._save_done_sig, key, ok, err)

        threading.Thread(target=worker, daemon=True, name="modio-test").start()

    def _save_done(self, key: str, ok: bool, err: str):
        self._busy = False
        self._save_btn.setEnabled(True)
        if not ok:
            msg = (self.tr("Key rejected by mod.io.") if not err
                   else self.tr("Key test failed: {0}").format(err))
            self._set_result(msg, ok=False)
            return
        try:
            self._modio_key.save_modio_key(key)
            self._set_result(
                self.tr("Key saved. mod.io update checks are now enabled."),
                ok=True)
            self._log("mod.io: API key saved.")
        except Exception as e:
            self._set_result(self.tr("Could not save key: {0}").format(e), ok=False)

    def _on_clear(self):
        try:
            self._modio_key.clear_modio_key()
            self._entry.clear()
            self._set_result(self.tr("Key cleared."))
            self._log("mod.io: API key cleared.")
        except Exception as e:
            self._set_result(self.tr("Could not clear key: {0}").format(e), ok=False)

    # ---- email login --------------------------------------------------------
    def _on_send_code(self):
        if self._login_busy:
            return
        email = self._login_email.text().strip()
        if not email:
            self._set_status(self._login_status, self.tr("Enter an email address first."),
                             _RED_ERR)
            return
        api_key = self._entry.text().strip()
        if not api_key:
            self._set_status(self._login_status,
                             self.tr("Enter and save your mod.io API key first."), _RED_ERR)
            return
        self._login_busy = True
        self._send_code_btn.setEnabled(False)
        self._set_status(self._login_status, self.tr("Sending code…"))

        def worker():
            ok = False
            err = ""
            try:
                self._modio_oauth.request_email_code(email, api_key)
                ok = True
            except Exception as e:
                err = str(e)
            safe_emit(self._email_sent_sig, ok, err)

        threading.Thread(target=worker, daemon=True, name="modio-email-request").start()

    def _email_sent(self, ok: bool, err: str):
        self._login_busy = False
        self._send_code_btn.setEnabled(True)
        if ok:
            self._set_status(self._login_status,
                             self.tr("Code sent — check your email, then enter it below."),
                             _GREEN_OK)
        else:
            self._set_status(self._login_status,
                             self.tr("Could not send code: {0}").format(err), _RED_ERR)

    def _on_verify_code(self):
        if self._login_busy:
            return
        code = self._login_code.text().strip()
        if not code:
            self._set_status(self._login_status, self.tr("Enter the code from your email."),
                             _RED_ERR)
            return
        api_key = self._entry.text().strip()
        if not api_key:
            self._set_status(self._login_status,
                             self.tr("Enter and save your mod.io API key first."), _RED_ERR)
            return
        self._login_busy = True
        self._verify_btn.setEnabled(False)
        self._set_status(self._login_status, self.tr("Verifying…"))

        def worker():
            ok = False
            err = ""
            try:
                self._modio_oauth.exchange_code(code, api_key)
                ok = True
            except Exception as e:
                err = str(e)
            safe_emit(self._login_done_sig, ok, err)

        threading.Thread(target=worker, daemon=True, name="modio-email-exchange").start()

    def _login_done(self, ok: bool, err: str):
        self._login_busy = False
        self._verify_btn.setEnabled(True)
        if ok:
            self._login_code.clear()
            self._set_status(self._login_status,
                             self.tr("Logged in to mod.io — Like is available."), _GREEN_OK)
            self._log("mod.io: logged in via email.")
        else:
            self._set_status(self._login_status,
                             self.tr("Could not verify code: {0}").format(err), _RED_ERR)

    def _on_logout(self):
        try:
            self._modio_oauth.clear_modio_tokens()
            self._refresh_login_status()
            self._log("mod.io: logged out.")
        except Exception as e:
            self._set_status(self._login_status, self.tr("Could not log out: {0}").format(e),
                             _RED_ERR)

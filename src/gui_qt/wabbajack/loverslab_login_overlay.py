"""In-window LoversLab login form for Wabbajack installs.

Step 1 is username + password; when the account has two-factor auth,
LoversLab answers with a code form and the card switches to step 2, a code
field (a wrong code can be retried). Both steps run on a worker thread
through ``Utils.wabbajack.downloaders.loverslab_auth``. Only the resulting
session is saved (keyring, or the encrypted-file fallback); the password and
code are used for their one request and never stored. ``on_done(True)`` once
logged in, ``on_done(False)`` on cancel / Esc.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from gui_qt.overlay_base import OverlayBase
from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import _c, active_palette


def _log(text: str) -> None:
    from Utils.app_log import app_log
    app_log(text)


class LoversLabLoginOverlay(OverlayBase):
    CARD_W = 460
    CARD_H = 300
    ESC_RESULT = False

    # ("ok", None) | ("mfa", MfaChallenge) | ("error", message)
    _login_result = Signal(str, object)

    def __init__(self, host: QWidget, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        _card, v = self._make_card("LoversLabLoginCard")
        self._challenge = None

        title = QLabel(self.tr("Log in to LoversLab"))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        self._info = QLabel(self.tr(
            "Some modlists download files from LoversLab, which needs you to be "
            "logged in. Your password is only used to log in and isn't saved; "
            "Mosaic keeps the login session in your system keyring."))
        self._info.setWordWrap(True)
        self._info.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        v.addWidget(self._info)

        self._user = QLineEdit()
        self._user.setPlaceholderText(self.tr("Username or email"))
        self._password = QLineEdit()
        self._password.setPlaceholderText(self.tr("Password"))
        self._password.setEchoMode(QLineEdit.Password)
        self._password.returnPressed.connect(self._submit)
        self._code = QLineEdit()
        self._code.setPlaceholderText(self.tr("Authenticator code"))
        self._code.setMaxLength(12)
        self._code.returnPressed.connect(self._submit)
        self._code.hide()
        v.addWidget(self._user)
        v.addWidget(self._password)
        v.addWidget(self._code)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color:{_c(p,'TEXT_ERR_BRIGHT')}; font-size:12px;")
        self._error.hide()
        v.addWidget(self._error)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self._cancel = QPushButton(self.tr("Cancel"))
        self._cancel.setObjectName("FormButton")
        self._cancel.setCursor(Qt.PointingHandCursor)
        self._cancel.clicked.connect(lambda: self._finish(False))
        bar.addWidget(self._cancel)
        self._ok = QPushButton(self.tr("Log in"))
        self._ok.setObjectName("PrimaryButton")
        self._ok.setCursor(Qt.PointingHandCursor)
        self._ok.clicked.connect(self._submit)
        bar.addWidget(self._ok)
        v.addLayout(bar)

        self._login_result.connect(self._on_result)
        self._present()
        self._user.setFocus()

    @classmethod
    def show_over(cls, host, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, on_done)

    def _idle_label(self) -> str:
        return self.tr("Verify code") if self._challenge is not None else self.tr("Log in")

    def _set_busy(self, busy: bool) -> None:
        for w in (self._user, self._password, self._code, self._ok):
            w.setEnabled(not busy)
        if busy:
            self._ok.setText(self.tr("Verifying…") if self._challenge is not None
                             else self.tr("Logging in…"))
        else:
            self._ok.setText(self._idle_label())

    def _submit(self):
        if self._challenge is not None:
            code = self._code.text().strip()
            if not code:
                self._show_error(self.tr("Enter the code from your authenticator app."))
                return
            args, target = (self._challenge, code), self._mfa_worker
        else:
            user, password = self._user.text().strip(), self._password.text()
            if not user or not password:
                self._show_error(self.tr("Enter both your username and password."))
                return
            args, target = (user, password), self._login_worker
        self._error.hide()
        self._set_busy(True)
        threading.Thread(target=target, args=args, daemon=True, name="loverslab-login").start()

    def _login_worker(self, user: str, password: str) -> None:
        from Utils.wabbajack.downloaders import loverslab_auth
        try:
            loverslab_auth.save_session(loverslab_auth.login(user, password, log=_log))
            safe_emit(self._login_result, "ok", None)
        except loverslab_auth.LoversLabMfaRequired as exc:
            safe_emit(self._login_result, "mfa", exc.challenge)
        except loverslab_auth.LoversLabAuthError as exc:
            diagnostic = getattr(exc, "diagnostic", "")
            if diagnostic:
                from Utils.app_log import app_log
                app_log("LoversLab login challenge page (form structure, no values):\n"
                        + diagnostic)
            safe_emit(self._login_result, "error", str(exc))
        except Exception as exc:
            safe_emit(self._login_result, "error", f"Login failed: {exc}")

    def _mfa_worker(self, challenge, code: str) -> None:
        from Utils.wabbajack.downloaders import loverslab_auth
        try:
            loverslab_auth.save_session(loverslab_auth.complete_mfa(challenge, code, log=_log))
            safe_emit(self._login_result, "ok", None)
        except loverslab_auth.LoversLabAuthError as exc:
            safe_emit(self._login_result, "error", str(exc))
        except Exception as exc:
            safe_emit(self._login_result, "error", f"Login failed: {exc}")

    def _show_error(self, text: str) -> None:
        _log(f"LoversLab login: {text}")
        self._error.setText(text)
        self._error.show()

    def _enter_code_step(self, challenge) -> None:
        self._challenge = challenge
        self._password.clear()
        self._user.hide()
        self._password.hide()
        self._info.setText(self.tr(
            "Your password was accepted. This account uses two-factor "
            "authentication: enter the current code from your authenticator app."))
        self._code.show()
        self._code.setFocus()

    def _on_result(self, kind, payload):
        if self._done:
            if kind == "mfa" and payload is not None:
                payload.close()
            return
        if kind == "ok":
            self._challenge = None
            self._finish(True)
            return
        if kind == "mfa":
            self._enter_code_step(payload)
            self._set_busy(False)
            return
        if self._challenge is not None and self._challenge.session is None:
            self._back_to_password_step()
        self._set_busy(False)
        self._show_error(str(payload))
        if self._challenge is not None:
            self._code.clear()
            self._code.setFocus()
        else:
            self._password.clear()
            self._password.setFocus()

    def _back_to_password_step(self) -> None:
        self._challenge = None
        self._code.clear()
        self._code.hide()
        self._user.show()
        self._password.show()
        self._info.setText(self.tr("Log in again with your username and password."))

    def _finish(self, result=None):
        if self._challenge is not None:
            self._challenge.close()
            self._challenge = None
        super()._finish(result)

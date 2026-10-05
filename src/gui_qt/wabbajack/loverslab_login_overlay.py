"""In-window LoversLab login form for Wabbajack installs.

Username + password, logged in on a worker thread through
``Utils.wabbajack.downloaders.loverslab_auth``. Only the resulting session is
saved (keyring, or the encrypted-file fallback); the password is used for
that one request and never stored. ``on_done(True)`` once logged in,
``on_done(False)`` on cancel / Esc.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from gui_qt.overlay_base import OverlayBase
from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import _c, active_palette


class LoversLabLoginOverlay(OverlayBase):
    CARD_W = 460
    CARD_H = 300
    ESC_RESULT = False

    _login_result = Signal(object)  # None on success, else an error message

    def __init__(self, host: QWidget, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        _card, v = self._make_card("LoversLabLoginCard")

        title = QLabel(self.tr("Log in to LoversLab"))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        info = QLabel(self.tr(
            "This modlist downloads files from LoversLab, which needs you to be "
            "logged in. Your password is only used to log in and isn't saved; "
            "Mosaic keeps the login session in your system keyring."))
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        v.addWidget(info)

        self._user = QLineEdit()
        self._user.setPlaceholderText(self.tr("Username or email"))
        self._password = QLineEdit()
        self._password.setPlaceholderText(self.tr("Password"))
        self._password.setEchoMode(QLineEdit.Password)
        self._password.returnPressed.connect(self._submit)
        v.addWidget(self._user)
        v.addWidget(self._password)

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

    def _set_busy(self, busy: bool) -> None:
        for w in (self._user, self._password, self._ok):
            w.setEnabled(not busy)
        self._ok.setText(self.tr("Logging in…") if busy else self.tr("Log in"))

    def _submit(self):
        user, password = self._user.text().strip(), self._password.text()
        if not user or not password:
            self._show_error(self.tr("Enter both your username and password."))
            return
        self._error.hide()
        self._set_busy(True)
        threading.Thread(target=self._worker, args=(user, password),
                         daemon=True, name="loverslab-login").start()

    def _worker(self, user: str, password: str) -> None:
        from Utils.wabbajack.downloaders import loverslab_auth
        try:
            loverslab_auth.save_session(loverslab_auth.login(user, password))
            safe_emit(self._login_result, None)
        except loverslab_auth.LoversLabAuthError as exc:
            safe_emit(self._login_result, str(exc))
        except Exception as exc:
            safe_emit(self._login_result, f"Login failed: {exc}")

    def _show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.show()

    def _on_result(self, error):
        if self._done:
            return
        if error is None:
            self._finish(True)
            return
        self._set_busy(False)
        self._password.clear()
        self._show_error(str(error))
        self._password.setFocus()

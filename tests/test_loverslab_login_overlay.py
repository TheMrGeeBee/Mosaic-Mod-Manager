"""The LoversLab login overlay's two-step flow (password, then a two-factor
code, with a wrong code retried), driven through real clicks offscreen.
The auth module is faked; its own behaviour is covered in
test_wabbajack_loverslab_auth.py."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow

from Utils.wabbajack.downloaders import loverslab_auth as auth
from gui_qt.wabbajack.loverslab_login_overlay import LoversLabLoginOverlay


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _teardown(app, host):
    """Destroy this test's window now -- with its children, including an
    overlay whose deleteLater is still queued -- so the teardown can't land in
    a later test's processEvents (see the Qt test host-window cleanup note).
    Only this window: flushing every queued deletion would also hit other
    test files' leftovers."""
    import shiboken6
    host.close()
    app.processEvents()
    if shiboken6.isValid(host):
        shiboken6.delete(host)
    app.processEvents()


def _wait_until(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the UI")


class _Challenge:
    def __init__(self):
        self.session = object()
        self.closed = False

    def close(self):
        self.closed = True


def test_password_then_wrong_code_then_right_code(qapp, monkeypatch):
    challenge = _Challenge()
    saved, codes = [], []

    def fake_login(user, password, log=None):
        assert (user, password) == ("me@example.com", "pw")
        raise auth.LoversLabMfaRequired(challenge)

    def fake_complete(ch, code, log=None):
        assert ch is challenge
        codes.append(code)
        if code != "123456":
            raise auth.LoversLabAuthError("LoversLab didn't accept that code. Check it and try again.")
        return {"ips4_member_id": "42"}

    monkeypatch.setattr(auth, "login", fake_login)
    monkeypatch.setattr(auth, "complete_mfa", fake_complete)
    monkeypatch.setattr(auth, "save_session", saved.append)
    results = []
    host = QMainWindow()
    host.resize(900, 600)
    host.show()
    try:
        ov = LoversLabLoginOverlay.show_over(host, results.append)
        QTest.keyClicks(ov._user, "me@example.com")
        QTest.keyClicks(ov._password, "pw")
        QTest.mouseClick(ov._ok, Qt.LeftButton)
        _wait_until(qapp, lambda: ov._code.isVisible() and ov._ok.isEnabled())
        assert not ov._user.isVisible() and ov._ok.text() == "Verify code"
        assert ov._password.text() == ""          # not kept around

        QTest.keyClicks(ov._code, "000000")
        QTest.mouseClick(ov._ok, Qt.LeftButton)
        _wait_until(qapp, lambda: ov._error.isVisible() and ov._ok.isEnabled())
        assert "didn't accept" in ov._error.text() and ov._code.text() == ""

        QTest.keyClicks(ov._code, "123456")
        QTest.mouseClick(ov._ok, Qt.LeftButton)
        _wait_until(qapp, lambda: results == [True])
        assert codes == ["000000", "123456"]
        assert saved == [{"ips4_member_id": "42"}]
    finally:
        _teardown(qapp, host)


def test_cancel_during_the_code_step_closes_the_pending_login(qapp, monkeypatch):
    challenge = _Challenge()

    def fake_login(user, password, log=None):
        raise auth.LoversLabMfaRequired(challenge)

    monkeypatch.setattr(auth, "login", fake_login)
    results = []
    host = QMainWindow()
    host.resize(900, 600)
    host.show()
    try:
        ov = LoversLabLoginOverlay.show_over(host, results.append)
        QTest.keyClicks(ov._user, "me")
        QTest.keyClicks(ov._password, "pw")
        QTest.mouseClick(ov._ok, Qt.LeftButton)
        _wait_until(qapp, lambda: ov._code.isVisible() and ov._ok.isEnabled())
        QTest.mouseClick(ov._cancel, Qt.LeftButton)
        _wait_until(qapp, lambda: results == [False])
        assert challenge.closed
    finally:
        _teardown(qapp, host)

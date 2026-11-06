"""Offscreen test of gui_qt.game_version_picker: the play bar's Build dropdown
on a fake Steam library, with the overlay/notify/wizard calls recorded instead
of shown. Covers what it offers, what it refuses, and that picking a build
Mosaic keeps really swaps the game's files."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from Utils.steam import depot_versions as dv


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


# Generous on purpose: every wait here is for a worker thread's signal, and a
# busy machine has been seen to take far longer than the work itself does.
def _wait_until(app, cond, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the UI")


APPMANIFEST = '''"AppState"
{
	"appid"		"377160"
	"buildid"		"24564252"
	"InstalledDepots"
	{
		"377162"	{ "manifest"		"4737695048810094469" "size" "55343016" }
	}
}'''


class FakeGame:
    name = "Fallout 4"
    steam_id = "377160"
    exe_name = "Fallout4Launcher.exe"

    def __init__(self, root, deployed=False):
        self.root = root
        self._deployed = deployed

    def get_game_path(self):
        return self.root

    def get_deploy_active(self):
        return self._deployed


class Recorder:
    """Stands in for the host window's overlays and toasts."""

    def __init__(self, answer=True):
        self.answer = answer
        self.confirms, self.messages, self.notes, self.logs = [], [], [], []
        self.wizard_opened = 0
        self.restored = 0

    def confirm(self, title, body, on_done, confirm_label=None):
        self.confirms.append((body, confirm_label))
        on_done(self.answer)

    def message(self, title, body):
        self.messages.append(body)

    def notify(self, text, state="info"):
        self.notes.append((text, state))

    def log(self, text):
        self.logs.append(text)

    def open_wizard(self):
        self.wizard_opened += 1

    def run_restore(self, on_done):
        self.restored += 1
        on_done(True)


def _picker(tmp_path, monkeypatch, *, answer=True):
    from Utils import config_paths
    from gui_qt import game_version_picker as gvp
    state = tmp_path / "config" / "games" / "Fallout 4"
    monkeypatch.setattr(config_paths, "get_game_config_dir", lambda name: state)
    monkeypatch.setattr(dv, "steam_roots", lambda: [tmp_path / "Steam"])
    monkeypatch.setattr(dv, "content_log_paths", lambda roots=None: [])
    rec = Recorder(answer)
    host = QMainWindow()
    picker = gvp.GameVersionPicker(
        host=host, log_fn=rec.log, notify=rec.notify, confirm=rec.confirm,
        message=rec.message, open_wizard=rec.open_wizard, run_restore=rec.run_restore)
    return picker, rec, state, host


def _library(tmp_path):
    lib = tmp_path / "lib" / "steamapps"
    game = lib / "common" / "Fallout 4"
    game.mkdir(parents=True)
    (game / "Fallout4.exe").write_bytes(b"exe 1.11.240")
    (lib / "appmanifest_377160.acf").write_text(APPMANIFEST)
    return game


def _items(picker):
    return list(picker.button._items)


def test_it_lists_the_installed_build_and_the_shipped_table(qapp, tmp_path, monkeypatch):
    picker, rec, _state, host = _picker(tmp_path, monkeypatch)
    try:
        picker.refresh(FakeGame(_library(tmp_path)))
        assert picker.button.isVisibleTo(host)
        assert _items(picker)[0] == "Steam build 24564252"
        assert "1.10.163" in _items(picker)
        assert picker.button.current() == "Steam build 24564252"
    finally:
        host.close()


def test_it_hides_itself_for_a_game_with_nothing_to_switch_between(qapp, tmp_path, monkeypatch):
    picker, _rec, _state, host = _picker(tmp_path, monkeypatch)
    try:
        picker.refresh(None)
        assert not picker.button.isVisibleTo(host)
        # Configured, but no shipped table and nothing in the store.
        class NoTable(FakeGame):
            steam_id = "999999999"
        (tmp_path / "lib" / "steamapps").mkdir(parents=True)
        picker.refresh(NoTable(tmp_path / "lib" / "steamapps" / "common" / "x"))
        assert not picker.button.isVisibleTo(host)
    finally:
        host.close()


def _switch_away(game_root, state):
    """Put the game on 1.10.163, so the build Steam installed is in the store."""
    depot = game_root.parent.parent.parent / "depot"
    depot.mkdir(parents=True, exist_ok=True)
    (depot / "Fallout4.exe").write_bytes(b"exe 1.10.163")
    entry = dv.VersionEntry(label="1.10.163", depots={"377162": "1" * 19})
    dv.apply_version(game_root, entry, {"377162": depot}, state,
                     from_label="Steam build 24564252")


def test_picking_a_build_mosaic_keeps_swaps_the_files(qapp, tmp_path, monkeypatch):
    picker, rec, state, host = _picker(tmp_path, monkeypatch)
    game_root = _library(tmp_path)
    try:
        _switch_away(game_root, state)
        picker.refresh(FakeGame(game_root))
        assert picker.button.current() == "1.10.163"

        picker._on_select("Steam build 24564252")
        _wait_until(qapp, lambda: rec.notes and rec.notes[-1][1] == "success")
        body, label = rec.confirms[0]
        assert "Nothing is downloaded" in body and label == "Switch"
        assert (game_root / "Fallout4.exe").read_bytes() == b"exe 1.11.240"
        assert rec.notes[-1][0] == "Now on Steam build 24564252."
        # The dropdown followed, and 1.10.163 is now the one on hand.
        assert picker.button.current() == "Steam build 24564252"
        assert dv.stored_labels(state) == ["1.10.163"]
    finally:
        host.close()


def test_cancelling_the_confirmation_changes_nothing(qapp, tmp_path, monkeypatch):
    picker, rec, state, host = _picker(tmp_path, monkeypatch, answer=False)
    game_root = _library(tmp_path)
    try:
        _switch_away(game_root, state)
        picker.refresh(FakeGame(game_root))
        picker._on_select("Steam build 24564252")
        _wait_until(qapp, lambda: bool(rec.confirms))
        qapp.processEvents()
        assert (game_root / "Fallout4.exe").read_bytes() == b"exe 1.10.163"
        assert picker.button.current() == "1.10.163"
        assert not rec.notes
    finally:
        host.close()


def test_picking_a_build_that_needs_downloading_offers_the_wizard(qapp, tmp_path, monkeypatch):
    picker, rec, _state, host = _picker(tmp_path, monkeypatch)
    try:
        picker.refresh(FakeGame(_library(tmp_path)))
        picker._on_select("1.10.163")
        _wait_until(qapp, lambda: rec.wizard_opened > 0)
        assert "isn't on this computer yet" in rec.confirms[0][0]
        assert rec.confirms[0][1] == "Open wizard"
        # Nothing switched, so the button stays where it was.
        assert picker.button.current() == "Steam build 24564252"
    finally:
        host.close()


def test_it_refuses_while_mods_are_deployed_and_offers_restore(qapp, tmp_path, monkeypatch):
    picker, rec, state, host = _picker(tmp_path, monkeypatch)
    game_root = _library(tmp_path)
    try:
        _switch_away(game_root, state)
        (game_root / "Data_Core").mkdir()
        picker.refresh(FakeGame(game_root, deployed=True))
        picker._on_select("Steam build 24564252")
        _wait_until(qapp, lambda: bool(rec.confirms))
        body, label = rec.confirms[0]
        assert "Restore first" in body and "Data_Core exists" in body
        assert label == "Restore now"
        assert rec.restored == 1
        # Refused, not attempted.
        assert (game_root / "Fallout4.exe").read_bytes() == b"exe 1.10.163"
    finally:
        host.close()


def test_a_failed_switch_is_reported_and_leaves_the_button_alone(qapp, tmp_path, monkeypatch):
    picker, rec, state, host = _picker(tmp_path, monkeypatch)
    game_root = _library(tmp_path)
    try:
        _switch_away(game_root, state)
        slot = dv.read_index(state)["slots"]["Steam build 24564252"]
        (tmp_path / "x").write_bytes(b"")
        import pathlib
        (pathlib.Path(slot["dir"]) / "Fallout4.exe").write_bytes(b"corrupted")
        picker.refresh(FakeGame(game_root))
        picker._on_select("Steam build 24564252")
        _wait_until(qapp, lambda: bool(rec.messages))
        assert "Verify integrity" in rec.messages[-1]
        assert (game_root / "Fallout4.exe").read_bytes() == b"exe 1.10.163"
        assert picker.button.current() == "1.10.163"
        assert picker.button.isEnabled()
    finally:
        host.close()


def test_it_refuses_while_the_skyrim_runtime_swap_is_applied(qapp, tmp_path, monkeypatch):
    """The runtime swap owns nine of the files a build switch does, so the core
    refuses. The dropdown's job is to say so up front rather than ask and then
    refuse -- whether a record is live is decided (and tested) in
    depot_versions, which is why it is stubbed here."""
    picker, rec, state, host = _picker(tmp_path, monkeypatch)
    game_root = _library(tmp_path)
    blocker = ("The Skyrim runtime swap is applied: the exe and the files it patches read "
               "1.6.1170 while the rest of the game is 1.7.104. Revert the runtime swap "
               "first (Wizard \u25b8 Skyrim runtime swap), then switch.")
    try:
        _switch_away(game_root, state)
        monkeypatch.setattr(dv, "runtime_swap_blocker", lambda root, sd: blocker)
        picker.refresh(FakeGame(game_root))
        picker._on_select("Steam build 24564252")
        _wait_until(qapp, lambda: bool(rec.messages))
        assert rec.messages[-1] == blocker
        assert not rec.confirms                      # never asked
        assert (game_root / "Fallout4.exe").read_bytes() == b"exe 1.10.163"
        assert picker.button.isEnabled()
    finally:
        host.close()

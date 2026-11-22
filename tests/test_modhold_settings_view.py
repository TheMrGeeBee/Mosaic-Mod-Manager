"""ModholdSettingsView: connect/disconnect/paste states and the wizard wiring."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from Modhold import modhold_key
from wizards_qt import modhold_settings_view as mod

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])

@pytest.fixture
def store(monkeypatch):
    data = {"key": "", "user": ""}
    monkeypatch.setattr(modhold_key, "load_modhold_key", lambda: data["key"])
    monkeypatch.setattr(modhold_key, "load_modhold_user", lambda: data["user"])

    def save(key, user=""):
        data["key"], data["user"] = key.strip(), user.strip()

    def clear():
        data["key"] = data["user"] = ""

    monkeypatch.setattr(modhold_key, "save_modhold_key", save)
    monkeypatch.setattr(modhold_key, "clear_modhold_key", clear)
    return data

@pytest.fixture
def view(app, store, monkeypatch):
    opened = []
    monkeypatch.setattr(mod.ModholdSettingsView, "_open_url",
                        lambda self, url: opened.append(url))
    game = SimpleNamespace(game_id="skyrim_se", name="Skyrim SE")
    v = mod.ModholdSettingsView(game, log_fn=lambda *_: None, on_close=lambda: None)
    v.opened = opened
    yield v
    v._finish()

def test_starts_not_connected(view):
    assert view._status.text() == "Not connected."
    assert view._disconnect_btn.isHidden()
    assert view._cancel_btn.isHidden()

def test_shows_the_connected_user(app, store, monkeypatch):
    store["key"], store["user"] = "mhm_x", "Sunny"
    monkeypatch.setattr(mod.ModholdSettingsView, "_open_url", lambda *_: None)
    v = mod.ModholdSettingsView(SimpleNamespace(game_id="skyrim_se"),
                                log_fn=lambda *_: None, on_close=lambda: None)
    assert v._status.text() == "Connected as Sunny."
    assert not v._disconnect_btn.isHidden()
    v._finish()

def test_connect_opens_the_modhold_url_and_waits(view):
    view._on_connect()
    assert len(view.opened) == 1
    assert view.opened[0].startswith("https://modholdmods.com/connect?")
    assert "Waiting" in view._status.text()
    assert not view._connect_btn.isEnabled()
    assert not view._cancel_btn.isHidden()

def test_cancel_stops_waiting(view):
    view._on_connect()
    view._on_cancel()
    assert view._connect is None
    assert view._status.text() == "Not connected."
    assert view._connect_btn.isEnabled()

def test_key_arriving_is_stored(view, store):
    view._on_connect()
    view._on_key("mhm_abc", "Sunny")
    assert store == {"key": "mhm_abc", "user": "Sunny"}
    assert view._status.text() == "Connected as Sunny."

def test_listener_error_is_shown(view):
    view._on_connect()
    view._on_error("Timed out waiting for Modhold. Try connecting again.")
    assert view._connect is None
    assert "Timed out" in view._status.text()

def test_paste_key_saves_and_clears_the_field(view, store):
    view._entry.setText("  mhm_pasted ")
    view._on_save_pasted()
    assert store["key"] == "mhm_pasted"
    assert view._entry.text() == ""
    assert view._status.text() == "Connected."

def test_paste_empty_is_rejected(view, store):
    view._on_save_pasted()
    assert store["key"] == ""
    assert "Enter a key" in view._status.text()

def test_disconnect_removes_the_key(view, store):
    store["key"] = "mhm_x"
    view._refresh()
    view._on_disconnect()
    assert store["key"] == ""
    assert view._status.text() == "Not connected."

def test_closing_while_waiting_stops_the_listener(view):
    view._on_connect()
    conn = view._connect
    view._finish()
    assert view._connect is None
    assert conn._finished

def test_listener_callbacks_marshal_through_signals(view, store):
    """The listener fires on its own thread; the view must still end up stored."""
    view._on_connect()
    done = threading.Event()
    view._key_sig.connect(lambda *_: done.set())
    t = threading.Thread(target=lambda: view._connect._on_key("mhm_t", "T"))
    t.start()
    t.join()
    QApplication.processEvents()
    assert done.wait(2)
    assert store["key"] == "mhm_t"

def test_wizard_is_registered_and_offered_only_for_mapped_games():
    from Games.base_game import BaseGame
    from wizards_qt import REGISTRY
    assert "wizards.modhold_settings.ModholdSettingsWizard" in REGISTRY

    # Build the tool list without instantiating a full game handler.
    mapped = SimpleNamespace(modhold_game_slug="skyrim-se", reshade_dll="d3d9.dll",
                             reshade_arch=64, steam_id="")
    unmapped = SimpleNamespace(modhold_game_slug="", reshade_dll="d3d9.dll",
                               reshade_arch=64, steam_id="")
    ids_mapped = [t.id for t in BaseGame._base_wizard_tools(mapped)]
    ids_unmapped = [t.id for t in BaseGame._base_wizard_tools(unmapped)]
    assert "modhold_account" in ids_mapped
    assert "modhold_account" not in ids_unmapped

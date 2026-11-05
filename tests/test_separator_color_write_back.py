"""The app's separator-colour write-back, driven through the real method.

``test_mo2_separator_meta.py`` covers ``write_separator_color`` directly. This
file binds ``MainWindow._write_separator_color_to_staging`` -- the method
``_save_sep_settings`` actually calls -- onto a stub, because that is where the
staging path is resolved and where the decision not to invent MO2 folders for
Mosaic's own separators lives.

Without the write-back, a colour changed in Mosaic never reaches MO2 and is
silently overwritten by the old one the next time the staging folder is
imported.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import types

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from Utils.mods.mo2_separator_meta import (  # noqa: E402
    SeparatorMeta,
    read_separator_meta,
    write_separator_meta,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def write_back(app):
    """The unbound method under test (booting a whole MainWindow would need a
    game, a profile and a Nexus login)."""
    from gui_qt.app import MainWindow
    return MainWindow._write_separator_color_to_staging


def _host(staging):
    """Just enough of MainWindow for the method: ``self._gs.game``."""
    class _Game:
        def get_effective_mod_staging_path(self):
            return str(staging)

    return types.SimpleNamespace(_gs=types.SimpleNamespace(game=_Game()))


def test_recolouring_an_imported_separator_reaches_mo2(write_back, tmp_path):
    staging = tmp_path / "mods"
    write_separator_meta(staging / "Textures_separator",
                         SeparatorMeta(color="#FF55FF", nest=2, max_size=3))

    write_back(_host(staging), "Textures_separator", "#123456")

    meta = read_separator_meta(staging / "Textures_separator")
    assert meta.color == "#123456"
    # Mosaic has no nesting model, so it must carry the curator's grouping
    # through untouched rather than flatten it on a colour edit.
    assert (meta.nest, meta.max_size) == (2, 3)


def test_a_mosaic_only_separator_gets_no_mo2_folder(write_back, tmp_path):
    """Mosaic's own separators are modlist.txt entries with nothing on disk.
    Creating MO2 folders for them is a bigger decision than saving a colour."""
    staging = tmp_path / "mods"
    staging.mkdir()

    write_back(_host(staging), "MosaicOnly_separator", "#00FF00")

    assert not (staging / "MosaicOnly_separator").exists()


def test_clearing_a_colour_is_written_through(write_back, tmp_path):
    staging = tmp_path / "mods"
    write_separator_meta(staging / "Textures_separator", SeparatorMeta(color="#FF55FF"))

    write_back(_host(staging), "Textures_separator", None)

    assert read_separator_meta(staging / "Textures_separator").color is None


def test_no_game_selected_does_not_raise(write_back):
    """Cosmetic mirroring: profile_state already holds the real value, so a
    failure here must never take a colour change down with it."""
    no_game = types.SimpleNamespace(_gs=types.SimpleNamespace(game=None))
    write_back(no_game, "X_separator", "#ffffff")


def test_an_unreadable_staging_path_does_not_raise(write_back, tmp_path):
    class _Game:
        def get_effective_mod_staging_path(self):
            raise OSError("staging drive is not mounted")

    host = types.SimpleNamespace(_gs=types.SimpleNamespace(game=_Game()))
    write_back(host, "X_separator", "#ffffff")

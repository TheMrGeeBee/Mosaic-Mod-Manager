"""The install box names the last phase: packing the mod archives one at a time.

Before this, the box kept saying "Building files…" and "8 archive(s) at once"
while the last few hundred steps packed .bsa files one by one, so it looked
stuck on a LoreRim install.
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from Utils.wabbajack.wabbajack_install import _packing_kinds
from Utils.wabbajack.wabbajack_manifest import CreateBSADirective


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _packing(*names):
    return [(CreateBSADirective(to=f"Some Mod/{n}"), None) for n in names]


def test_packing_kinds_names_the_formats():
    assert _packing_kinds(_packing("a.bsa", "b.BSA")) == "BSA"
    assert _packing_kinds(_packing("a.ba2")) == "BA2"
    assert _packing_kinds(_packing("a.ba2", "b.bsa")) == "BA2/BSA"


def test_overlay_shows_which_archive_is_being_packed(qapp):
    from gui_qt.wabbajack.wabbajack_install_overlay import WabbajackInstallOverlay

    host = QWidget()
    overlay = WabbajackInstallOverlay.show_over(host, "Installing Test", 1, 1,
                                                on_cancel=lambda: None)
    overlay.set_workers(8, "trying one more")
    assert "at once" in overlay._workers_label.text()
    overlay.set_packing(11, 383, "Sons of Skyrim - Textures.bsa")
    assert overlay._workers_label.text() == "Archive 12 of 383: Sons of Skyrim - Textures.bsa"
    overlay.set_status("Building BSA archives…")
    assert overlay._status.text() == "Building BSA archives…"
    host.deleteLater()


def test_long_summary_scrolls_inside_the_card(qapp):
    from gui_qt.wabbajack.wabbajack_install_overlay import WabbajackInstallOverlay

    host = QWidget()
    host.resize(1600, 900)
    host.show()
    overlay = WabbajackInstallOverlay.show_over(host, "Installing LoreRim", 1, 1,
                                                on_cancel=lambda: None)
    mods = ", ".join(f"Some Root Builder mod {i}" for i in range(120))
    overlay.finish(f"Installed 3936 mods. 17 mod(s) use Root Builder: {mods}", ok=True)
    qapp.processEvents()
    scroll = overlay._summary_scroll
    assert scroll.isVisible() and scroll.widget() is overlay._summary
    # The card grows to its finished height; the text that still doesn't fit scrolls.
    assert overlay._card.height() == WabbajackInstallOverlay.FINISHED_CARD_H
    assert scroll.geometry().bottom() < overlay._close.geometry().top()
    assert scroll.verticalScrollBar().maximum() > 0
    host.deleteLater()

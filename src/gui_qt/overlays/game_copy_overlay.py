"""Ask where a profile's game copy goes and which build it's made at.

``on_done((Path dest, str build))`` with ``dest`` = ``<modlist folder>/Stock
Game``, or ``on_done(None)`` on Cancel / Esc. A child overlay via
gui_qt/overlay_base.py.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QWidget,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme.theme_qt import _c, active_palette
from Utils.game_copy import COPY_DIRNAME


class _PickSignal(QObject):
    # The portal folder picker calls back on its worker thread.
    picked = Signal(object)


class GameCopyOverlay(OverlayBase):
    CARD_W = 560
    CARD_H = 400
    MIN_H = 360

    def __init__(self, host: QWidget, *, profile: str, default_folder: Path,
                 builds: "list[tuple[str, str]]", current: str, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        _card, v = self._make_card("GameCopyCard")

        title = QLabel(self.tr("Make a game copy for {0}").format(profile))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)
        intro = QLabel(self.tr(
            "This profile will run on its own copy of the game, like MO2's Stock Game, so "
            "Steam updates and other profiles' versions never affect it. Mosaic clones the "
            "Steam install, which is instant and takes no extra space on btrfs/XFS."))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        v.addWidget(intro)

        v.addWidget(self._label(self.tr("Modlist folder"), p))
        row = QHBoxLayout()
        self._folder = QLineEdit(str(default_folder))
        self._folder.textChanged.connect(self._update_dest)
        row.addWidget(self._folder, 1)
        browse = QPushButton(self.tr("Browse…"))
        browse.setObjectName("FormButton")
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        v.addLayout(row)
        self._dest_lbl = QLabel()
        self._dest_lbl.setWordWrap(True)
        self._dest_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        v.addWidget(self._dest_lbl)

        v.addWidget(self._label(self.tr("Game version"), p))
        self._group = QButtonGroup(self)
        self._build_of: "dict[QRadioButton, str]" = {}
        for label, name in builds:
            text = name if label != current else self.tr("{0} (what Steam has installed now)"
                                                         ).format(name)
            rb = QRadioButton(text)
            rb.setChecked(label == current)
            self._group.addButton(rb)
            self._build_of[rb] = label
            v.addWidget(rb)
        if len(builds) == 1:
            hint = QLabel(self.tr("Other versions appear here once Mosaic keeps them (switch "
                                  "the Steam install to one once with the Build menu)."))
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
            v.addWidget(hint)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        self._ok = QPushButton(self.tr("Make copy"))
        self._ok.setObjectName("PrimaryButton")
        self._ok.setCursor(Qt.PointingHandCursor)
        self._ok.clicked.connect(self._accept)
        bar.addWidget(self._ok)
        v.addLayout(bar)

        self._pick = _PickSignal(self)
        self._pick.picked.connect(self._on_picked)
        self._update_dest()
        self._present()

    @staticmethod
    def _label(text, p) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{_c(p,'TEXT_SEP')}; font-weight:600; margin-top:6px;")
        return lbl

    def _dest(self) -> "Path | None":
        raw = self._folder.text().strip()
        return Path(raw).expanduser() / COPY_DIRNAME if raw else None

    def _update_dest(self):
        dest = self._dest()
        self._dest_lbl.setText(self.tr("The copy goes to: {0}").format(dest) if dest
                               else self.tr("Choose a folder."))
        self._ok.setEnabled(dest is not None)

    def _browse(self):
        from Utils.wine_proton.portal_filechooser import pick_folder
        pick_folder("Select the modlist folder", lambda path: self._pick.picked.emit(path))

    def _on_picked(self, path):
        if path:
            self._folder.setText(str(path))

    def _accept(self):
        dest = self._dest()
        rb = self._group.checkedButton()
        if dest is not None and rb is not None:
            self._finish((dest, self._build_of[rb]))

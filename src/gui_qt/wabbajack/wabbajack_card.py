"""A single Wabbajack gallery modlist card for the Wabbajack browser.

Laid out like Jackify's gallery cards: a 16:9 cover, the name with
OFFICIAL / NSFW badges beside it, author, game, and a
"Download | Install | Total" size line. The whole card is clickable and opens
the details window (``wabbajack_details_overlay``), where the list is
installed from. Every card has the same size so the grid stays uniform.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from gui_qt.nexus.nexus_mod_card import _TwoLineLabel
from gui_qt.theme.theme_qt import _c, active_palette

CARD_W = 300
IMG_W = 298           # CARD_W minus the 1px border each side
IMG_H = 168           # 16:9, the gallery's cover format
CARD_H = 318


def fmt_bytes(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def badge(text: str, colour: str) -> QLabel:
    b = QLabel(text)
    b.setStyleSheet(f"color:#ffffff; background:{colour}; border-radius:3px;"
                    f" padding:1px 6px; font-size:10px; font-weight:600;")
    return b


def badges_for(entry, p) -> "list[QLabel]":
    out = []
    if entry.official:
        out.append(badge("OFFICIAL", "#2e9b4f"))
    if entry.nsfw:
        out.append(badge("NSFW", _c(p, "BTN_DANGER")))
    if entry.force_down:
        out.append(badge("UNAVAILABLE", _c(p, "TEXT_DIM")))
    return out


def size_line(entry) -> str:
    return (f"Download: {fmt_bytes(entry.archives_size)}  |  "
            f"Install: {fmt_bytes(entry.install_size)}  |  "
            f"Total: {fmt_bytes(entry.total_size)}")


class WabbajackCard(QWidget):
    """*entry* is a ``GalleryModlist``; clicking calls *on_open(entry)*;
    *on_context(entry, global_pos)* is the right-click menu."""

    def __init__(self, entry, on_open, on_context=None, parent=None):
        super().__init__(parent)
        self.setObjectName("GameCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(self.tr("Show details and install"))
        self.entry = entry
        self._on_open = on_open
        self._on_context = on_context
        p = active_palette()
        dim = _c(p, "TEXT_DIM")
        self.setStyleSheet(
            f"#GameCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}"
            f"#GameCard:hover {{ border:1px solid {_c(p,'ACCENT')}; }}")

        v = QVBoxLayout(self)
        v.setContentsMargins(1, 1, 1, 1)
        v.setSpacing(0)

        self._img = QLabel()
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setFixedSize(IMG_W, IMG_H)
        self._img.setStyleSheet(
            f"background:{_c(p,'BG_DEEP')};"
            f" border-top-left-radius:7px; border-top-right-radius:7px; color:{dim};")
        self._img.setText("…")
        v.addWidget(self._img)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        title = _TwoLineLabel(entry.title)
        title.setStyleSheet(f"color:{_c(p,'ACCENT')}; font-weight:600; font-size:14px;")
        title.setFixedHeight(40)
        head.addWidget(title, 1)
        badge_col = QVBoxLayout()
        badge_col.setSpacing(3)
        for b in badges_for(entry, p):
            badge_col.addWidget(b, 0, Qt.AlignRight)
        badge_col.addStretch(1)
        head.addLayout(badge_col)
        bl.addLayout(head)

        if entry.author:
            author = QLabel(self.tr("by {0}").format(entry.author))
            author.setStyleSheet(f"color:{dim}; font-size:11px;")
            author.setMaximumWidth(CARD_W - 22)
            bl.addWidget(author)
        game = QLabel(entry.mosaic_game or entry.game)
        game.setStyleSheet(f"color:{dim}; font-size:11px;")
        bl.addWidget(game)
        bl.addStretch(1)

        sizes = QLabel(size_line(entry))
        sizes.setStyleSheet(f"color:{dim}; font-size:10px;")
        bl.addWidget(sizes)

        v.addWidget(body, 1)

    def set_thumbnail(self, pm: QPixmap) -> None:
        if pm is not None and not pm.isNull():
            self._img.setText("")
            self._img.setPixmap(pm)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self._on_open(self.entry)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        if self._on_context is not None:
            self._on_context(self.entry, event.globalPos())
        else:
            super().contextMenuEvent(event)

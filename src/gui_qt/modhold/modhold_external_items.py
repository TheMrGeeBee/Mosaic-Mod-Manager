"""After a loadout install: the items Modhold doesn't host, with links to get them."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from Modhold.loadout_install import LOADOUT_RECORD_NAME


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def external_items_html(items: "list[tuple[str, str, str]]") -> str:
    """Rows of ``(name, url, note)`` as clickable HTML, one per line."""
    rows = []
    for name, url, note in items:
        label = _esc(name or url)
        link = f'<a href="{_esc(url)}">{label}</a>' if url else label
        tail = f" — {_esc(note)}" if note else ""
        rows.append(f"• {link}{tail}")
    return "<br>".join(rows)


class ModholdExternalItems(QDialog):
    """Non-modal list of what the user still has to download and install themselves."""

    def __init__(self, loadout_name: str, items: "list[tuple[str, str, str]]",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Items to get yourself"))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "{0} includes {1} item(s) that are not hosted on Modhold, so Mosaic could not "
            "download them. Get each one from its link and install it with Install Mod. "
            "The same list is saved in the profile ({2}).").format(
                loadout_name, len(items), LOADOUT_RECORD_NAME))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        caution = QLabel(self.tr(
            "These links come from the loadout's author. Neither Mosaic nor Modhold checks "
            "where they lead, and a site can change hands or disappear. Only download from "
            "sites you trust."))
        caution.setWordWrap(True)
        lay.addWidget(caution)
        body = QLabel(external_items_html(items))
        body.setTextFormat(Qt.RichText)
        body.setOpenExternalLinks(True)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextBrowserInteraction)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(220)
        scroll.setWidget(body)
        lay.addWidget(scroll)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.close)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.close)
        lay.addWidget(buttons)

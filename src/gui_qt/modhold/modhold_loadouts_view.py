"""Modhold loadouts tab: the public loadouts for a game, most installed first."""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import _c, active_palette, button_qss, err_text
from Modhold import modhold_key
from Modhold.modhold_api import ModholdAPI

_SUMMARY_MAX = 260


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _LoadoutCard(QFrame):
    def __init__(self, loadout: dict, on_install, on_open, parent: QWidget | None = None):
        super().__init__(parent)
        p = active_palette()
        self.loadout = loadout
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(f"_LoadoutCard {{ border:1px solid {_c(p, 'BORDER_FAINT')}; "
                           f"border-radius:4px; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel(f"<b>{_esc(loadout.get('name', ''))}</b>")
        title.setTextFormat(Qt.RichText)
        title.setWordWrap(True)
        col.addWidget(title)
        meta = self.tr("by {0} · {1} mods · {2} installs · updated {3}").format(
            loadout.get("by") or loadout.get("by_handle") or "?",
            int(loadout.get("mods") or 0), int(loadout.get("installs") or 0),
            (loadout.get("updated") or "")[:10])
        dim = QLabel(meta)
        dim.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')};")
        col.addWidget(dim)
        summary = (loadout.get("summary") or "").strip()
        if len(summary) > _SUMMARY_MAX:
            summary = summary[:_SUMMARY_MAX].rstrip() + "…"
        if summary:
            s = QLabel(summary)
            s.setWordWrap(True)
            col.addWidget(s)
        row.addLayout(col, 1)
        btns = QVBoxLayout()
        install = QPushButton(self.tr("Install"))
        install.setCursor(Qt.PointingHandCursor)
        install.setStyleSheet(button_qss("BTN_INFO"))
        install.clicked.connect(lambda: on_install(loadout))
        page = QPushButton(self.tr("Open page"))
        page.setCursor(Qt.PointingHandCursor)
        page.clicked.connect(lambda: on_open(loadout))
        btns.addWidget(install)
        btns.addWidget(page)
        btns.addStretch(1)
        row.addLayout(btns)


class ModholdLoadoutsView(QWidget):
    """Browse a game's Modhold loadouts and start installing one."""

    _loaded_sig = Signal(object, str)      # (list of loadout dicts | None, error)

    def __init__(self, game, slug: str, install_fn, log_fn=None, api_factory=None, parent=None):
        super().__init__(parent)
        self._game, self._slug = game, slug
        self._install_fn = install_fn
        self._log = log_fn or (lambda *_: None)
        self._api_factory = api_factory or (
            lambda: ModholdAPI(modhold_key.load_modhold_key()))
        self._closing = False
        self._gen = 0
        self._cards: list[_LoadoutCard] = []
        self._loaded_sig.connect(lambda *a: None if self._closing else self._on_loaded(*a))
        self.destroyed.connect(self._on_destroyed)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search {0} loadouts…").format(
            getattr(game, "name", "")))
        self._search.returnPressed.connect(self._load)
        top.addWidget(self._search, 1)
        go = QPushButton(self.tr("Search"))
        go.clicked.connect(self._load)
        top.addWidget(go)
        root.addLayout(top)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        host = QWidget()
        self._list = QVBoxLayout(host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(6)
        self._list.addStretch(1)
        self._scroll.setWidget(host)
        root.addWidget(self._scroll, 1)
        self._load()

    def _set_status(self, text: str, error: bool = False):
        self._status.setStyleSheet(f"color:{err_text()};" if error else "")
        self._status.setText(text)

    def _load(self):
        self._gen += 1
        gen, q = self._gen, self._search.text().strip()
        self._set_status(self.tr("Loading…"))

        def worker():
            try:
                items = self._api_factory().list_loadouts(self._slug, q)
                safe_emit(self._loaded_sig, {"gen": gen, "items": items}, "")
            except Exception as e:
                safe_emit(self._loaded_sig, {"gen": gen, "items": None}, str(e))

        threading.Thread(target=worker, daemon=True, name="modhold-loadouts").start()

    def _on_loaded(self, result, error: str):
        if result.get("gen") != self._gen:
            return
        items = result.get("items")
        if items is None:
            self._set_status(self.tr("Could not load loadouts: {0}").format(error), error=True)
            return
        while self._list.count() > 1:
            w = self._list.takeAt(0)
            if w.widget():
                w.widget().deleteLater()
        self._cards = []
        for lo in items:
            card = _LoadoutCard(lo, self._install_fn, self._open_page)
            self._list.insertWidget(self._list.count() - 1, card)
            self._cards.append(card)
        self._set_status("" if items else self.tr("No loadouts found."))

    def _open_page(self, loadout: dict):
        url = loadout.get("page") or ""
        if url:
            from Utils.xdg import open_url
            open_url(url, log_fn=self._log)

    def _on_destroyed(self, *_):
        self._closing = True

"""Wabbajack modlist browser — a full detachable tab, modelled on Jackify's
"Select Modlist" screen.

  ┌────────────────┬───────────────────────────────────────────────┐
  │ Filters        │                         N modlists · Refresh │
  │ [search……]     │                                               │
  │ Sort ▾         │              card grid (scrolls)              │
  │ ☐ Official only│                                               │
  │ ☐ Show NSFW    │   click a card → details window → Install     │
  │ ☑ Hide unavail.│                                               │
  │ Tags: [list]   │                                               │
  │ Gallery website│                                               │
  └────────────────┴───────────────────────────────────────────────┘

The gallery (``Utils.wabbajack.wabbajack_gallery``) is fetched once on a
worker thread and cached for an hour; filtering to the current game, search,
tags, sort and the toggles all happen locally. A card opens
``WabbajackDetailsOverlay``, whose Install Modlist downloads the list's
``.wabbajack`` file and hands its path to *on_file_ready* (the import screen:
profile choice, preflight, install).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMenu, QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from gui_qt.nexus.nexus_mod_card import ThumbnailLoader
from gui_qt.safe_emit import safe_emit
from gui_qt.selector_button import SelectorButton
from gui_qt.theme.theme_qt import _c, active_palette
from gui_qt.wabbajack.wabbajack_card import CARD_W, IMG_H, IMG_W, WabbajackCard

PANEL_W = 250

SORT_KEYS = [
    ("Recently updated", "updated"),
    ("Name", "name"),
    ("Smallest download", "smallest"),
    ("Largest download", "largest"),
]


def sort_entries(entries, key: str):
    if key == "name":
        return sorted(entries, key=lambda e: e.title.lower())
    if key == "smallest":
        return sorted(entries, key=lambda e: e.archives_size)
    if key == "largest":
        return sorted(entries, key=lambda e: -e.archives_size)
    # Most recently updated first; lists without a date go last.
    return sorted(entries, key=lambda e: (e.date_updated or "", e.title.lower()), reverse=True)


def filter_entries(entries, *, game_name: str, query: str = "", show_adult: bool = False,
                   official_only: bool = False, hide_unavailable: bool = True,
                   tags: "set[str] | frozenset" = frozenset()):
    """Lists for *game_name*, narrowed by the toggles, by *query* (title,
    author, description or tags) and by *tags* (a list must have all of
    them)."""
    q = query.strip().lower()
    want = {t.lower() for t in tags}
    out = []
    for e in entries:
        if e.mosaic_game != game_name:
            continue
        if (e.force_down and hide_unavailable) or (e.nsfw and not show_adult):
            continue
        if official_only and not e.official:
            continue
        if want and not want <= {t.lower() for t in e.tags}:
            continue
        if q and not any(q in (s or "").lower() for s in
                         (e.title, e.author, e.description, " ".join(e.tags))):
            continue
        out.append(e)
    return out


class WabbajackBrowserView(QWidget):
    """*game* is the active game; *cache_path* / *files_dir* say where the
    gallery cache and fetched ``.wabbajack`` files live; *on_file_ready(path)*
    opens a fetched file; *notify(text, level)* shows a toast."""

    _gallery_ready = Signal(object, str, int)          # entries, error, token

    def __init__(self, game, *, cache_path, files_dir, on_file_ready,
                 log_fn=None, notify=None, parent=None):
        super().__init__(parent)
        self._game = game
        self._cache_path = cache_path
        self._files_dir = files_dir
        self._on_file_ready = on_file_ready
        self._log = log_fn or (lambda m: None)
        self._notify = notify or (lambda text, level="info": None)

        self._all = []
        self._shown = []
        self._sort = "updated"
        self._cards: "list[WabbajackCard]" = []
        self._ids: "dict[str, int]" = {}
        self._cols = 0
        self._token = 0
        self._details = None

        self._thumbs = ThumbnailLoader(self, crop_w=IMG_W, crop_h=IMG_H)
        self._thumbs.loaded.connect(self._on_thumb)
        from gui_qt.wabbajack.wabbajack_details_overlay import IMG_H as D_H, IMG_W as D_W
        self._covers = ThumbnailLoader(self, crop_w=D_W, crop_h=D_H)
        self._covers.loaded.connect(self._on_cover)
        self._gallery_ready.connect(self._on_gallery)

        self._build()
        self._load()

    # -- construction -----------------------------------------------------------
    def _button(self, text, slot):
        b = QToolButton()
        b.setText(text)
        b.setObjectName("ActionButton")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        p = active_palette()

        # --- filter panel -------------------------------------------------------
        panel = QFrame()
        panel.setObjectName("WabbajackFilterPanel")
        panel.setFixedWidth(PANEL_W)
        panel.setStyleSheet(f"#WabbajackFilterPanel {{ background:{_c(p,'BG_PANEL')};"
                            f" border-right:1px solid {_c(p,'BORDER')}; }}")
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(12, 12, 12, 12)
        pv.setSpacing(8)
        head = QLabel(self.tr("Filters"))
        head.setStyleSheet(f"color:{_c(p,'ACCENT')}; font-weight:600; font-size:15px;")
        pv.addWidget(head)
        self._game_lbl = QLabel()
        self._game_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        pv.addWidget(self._game_lbl)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search modlists…"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _t: self._apply())
        pv.addWidget(self._search)
        self._sort_sel = SelectorButton(
            items=[self.tr(lbl) for lbl, _ in SORT_KEYS],
            current=self.tr("Recently updated"),
            prefix=self.tr("Sort: "), min_width=PANEL_W - 24, on_select=self._on_sort_changed)
        pv.addWidget(self._sort_sel)
        self._official_cb = QCheckBox(self.tr("Show official only"))
        self._adult_cb = QCheckBox(self.tr("Show NSFW"))
        self._adult_cb.setChecked(self._load_show_adult())
        self._unavailable_cb = QCheckBox(self.tr("Hide unavailable"))
        self._unavailable_cb.setChecked(True)
        for cb in (self._official_cb, self._adult_cb, self._unavailable_cb):
            cb.toggled.connect(lambda _on: self._apply())
            pv.addWidget(cb)
        self._adult_cb.toggled.connect(self._save_show_adult)
        tags_lbl = QLabel(self.tr("Tags:"))
        tags_lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
        pv.addWidget(tags_lbl)
        self._tags = QListWidget()
        self._tags.setSelectionMode(QAbstractItemView.MultiSelection)
        self._tags.itemSelectionChanged.connect(self._apply)
        pv.addWidget(self._tags, 1)
        clear = self._button(self.tr("Clear filters"), self._clear_filters)
        pv.addWidget(clear)
        pv.addWidget(self._button(self.tr("Open gallery website"), self._open_website))
        outer.addWidget(panel)

        # --- grid side ----------------------------------------------------------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        top = QWidget()
        top.setObjectName("HeaderBar")
        tb = QHBoxLayout(top)
        tb.setContentsMargins(10, 6, 10, 6)
        self._title = QLabel()
        self._title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        tb.addWidget(self._title)
        tb.addStretch(1)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        tb.addWidget(self._status)
        tb.addWidget(self._button(self.tr("Refresh"), lambda: self._load(force=True)))
        rv.addWidget(top)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(16, 12, 16, 12)
        self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignTop)
        self._empty = QLabel()
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:40px;")
        self._empty.hide()
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(0)
        bv.addWidget(self._empty)
        bv.addWidget(self._grid_host)
        bv.addStretch(1)
        self._scroll.setWidget(body)
        self._scroll.installEventFilter(self)
        from gui_qt.overlays.loading_overlay import LoadingOverlay
        self._loading_overlay = LoadingOverlay(self._scroll)
        rv.addWidget(self._scroll, 1)
        outer.addWidget(right, 1)
        self._update_title()

    def _update_title(self):
        name = getattr(self._game, "name", "") or ""
        self._title.setText(self.tr("Wabbajack modlists · {0}").format(name) if name
                            else self.tr("Wabbajack modlists"))
        self._game_lbl.setText(self.tr("Game: {0}").format(name or "—"))

    # -- settings ---------------------------------------------------------------
    @staticmethod
    def _load_show_adult() -> bool:
        try:
            from Utils.ui_config import load_nexus_show_adult
            return bool(load_nexus_show_adult())
        except Exception:
            return False

    @staticmethod
    def _save_show_adult(on: bool):
        try:
            from Utils.ui_config import save_nexus_show_adult
            save_nexus_show_adult(bool(on))
        except Exception:
            pass

    def _clear_filters(self):
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._official_cb.setChecked(False)
        self._unavailable_cb.setChecked(True)
        self._tags.clearSelection()
        self._apply()

    # -- loading ----------------------------------------------------------------
    def set_game(self, game):
        """Retarget at another game (switched while the tab is open)."""
        self._game = game
        self._update_title()
        self._rebuild_tags()
        self._apply()

    def _load(self, force: bool = False):
        self._token += 1
        token = self._token
        self._status.setText(self.tr("Loading…"))
        self._loading_overlay.show_over()
        cache_path, log = self._cache_path, self._log

        def worker():
            from Utils.wabbajack.wabbajack_gallery import load_gallery
            try:
                entries, error = load_gallery(cache_path, force=force, log=log), ""
            except Exception as exc:
                entries, error = [], str(exc)
                log(f"Wabbajack gallery: {exc}")
            safe_emit(self._gallery_ready, entries, error, token)

        threading.Thread(target=worker, daemon=True, name="wabbajack-gallery").start()

    def _on_gallery(self, entries, error, token):
        if token != self._token:
            return
        self._loading_overlay.hide_overlay()
        self._all = list(entries or [])
        self._ids = {e.machine_url or e.title: i for i, e in enumerate(self._all)}
        if error and not self._all:
            self._show_empty(self.tr("Couldn't load the Wabbajack gallery: {0}").format(error))
            self._status.setText("")
            return
        self._rebuild_tags()
        self._apply()

    def _rebuild_tags(self):
        name = getattr(self._game, "name", "") or ""
        selected = {i.text() for i in self._tags.selectedItems()}
        tags = sorted({t for e in self._all if e.mosaic_game == name for t in e.tags},
                      key=str.lower)
        self._tags.blockSignals(True)
        self._tags.clear()
        for t in tags:
            self._tags.addItem(t)
            if t in selected:
                self._tags.item(self._tags.count() - 1).setSelected(True)
        self._tags.blockSignals(False)

    # -- filter / sort ------------------------------------------------------------
    def _apply(self):
        name = getattr(self._game, "name", "") or ""
        self._shown = sort_entries(filter_entries(
            self._all, game_name=name, query=self._search.text(),
            show_adult=self._adult_cb.isChecked(),
            official_only=self._official_cb.isChecked(),
            hide_unavailable=self._unavailable_cb.isChecked(),
            tags={i.text() for i in self._tags.selectedItems()}), self._sort)
        if not self._all:
            return
        if not self._shown:
            if not any(e.mosaic_game == name for e in self._all):
                msg = self.tr("The Wabbajack gallery has no modlists for {0} that Mosaic "
                              "can install.").format(name or self.tr("this game"))
            else:
                msg = self.tr("No modlists match these filters.")
            self._show_empty(msg)
            self._status.setText(self.tr("0 modlists"))
            return
        self._empty.hide()
        self._grid_host.show()
        self._status.setText(self.tr("{0} modlist(s)").format(len(self._shown)))
        self._rebuild_cards()
        self._scroll.verticalScrollBar().setValue(0)

    def _show_empty(self, text: str):
        for c in self._cards:
            c.setParent(None)
        self._cards.clear()
        self._grid_host.hide()
        self._empty.setText(text)
        self._empty.show()

    def _on_sort_changed(self, label: str):
        key = next((k for lbl, k in SORT_KEYS if self.tr(lbl) == label), None)
        if key is None or key == self._sort:
            return
        self._sort = key
        self._apply()

    # -- cards ------------------------------------------------------------------
    def _rebuild_cards(self):
        for c in self._cards:
            c.setParent(None)
        self._cards.clear()
        for e in self._shown:
            card = WabbajackCard(e, self._open_details, on_context=self._show_card_menu)
            self._cards.append(card)
            self._thumbs.request(self._ids.get(e.machine_url or e.title, -1), e.image_url)
        self._cols = 0
        self._relayout()

    def _cols_for_width(self) -> int:
        vp = self._scroll.viewport().width()
        return max(1, (vp - 32) // (CARD_W + self._grid.spacing()))

    def _relayout(self):
        cols = self._cols_for_width()
        while self._grid.count():
            self._grid.takeAt(0)
        for i, card in enumerate(self._cards):
            self._grid.addWidget(card, i // cols, 1 + (i % cols), Qt.AlignTop | Qt.AlignHCenter)
            card.show()
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(cols + 1, 1)
        self._cols = cols

    def _on_thumb(self, entry_id, pm):
        for card in self._cards:
            if self._ids.get(card.entry.machine_url or card.entry.title) == entry_id:
                card.set_thumbnail(pm)

    def eventFilter(self, obj, event):
        if obj is self._scroll and event.type() == QEvent.Resize:
            if self._cols_for_width() != self._cols:
                self._relayout()
        return super().eventFilter(obj, event)

    # -- details / install -------------------------------------------------------
    def _open_details(self, entry):
        from gui_qt.wabbajack.wabbajack_details_overlay import WabbajackDetailsOverlay

        def done(path):
            self._details = None
            if path is not None:
                self._on_file_ready(path)

        self._details = WabbajackDetailsOverlay.show_over(
            self, entry, self._files_dir, done, log_fn=self._log)
        self._covers.request(self._ids.get(entry.machine_url or entry.title, -1), entry.image_url)

    def _on_cover(self, entry_id, pm):
        d = self._details
        if d is not None and self._ids.get(d.entry.machine_url or d.entry.title) == entry_id:
            d.set_cover(pm)

    # -- links --------------------------------------------------------------------
    def _open_url(self, url: str):
        if url:
            from Utils.xdg import open_url
            open_url(url, log_fn=self._log)

    def _open_website(self):
        from Utils.wabbajack.wabbajack_gallery import GALLERY_WEBSITE
        self._open_url(GALLERY_WEBSITE)

    def _show_card_menu(self, entry, global_pos):
        menu = QMenu(self)
        menu.addAction(self.tr("Details…"), lambda: self._open_details(entry))
        for label, url in ((self.tr("Read me"), entry.readme_url),
                           (self.tr("Website"), entry.website_url),
                           (self.tr("Discord"), entry.discord_url)):
            if url:
                menu.addAction(label, lambda u=url: self._open_url(u))
        menu.exec(global_pos)

"""mod_row_widgets.py — shared per-mod-row Qt widgets for the profile-export
table pattern: the borderless overlay base, Source and Version/File pickers,
source button styling, and the centered-checkbox cell helper.

Both ``export_profile_view.py`` (the ``.mosaic`` exporter) and
``create_collection_view.py`` (the Nexus Collection exporter) build their
per-mod table on this — extracted here so the two views share one copy
instead of two drifting ones.

``SOURCE_LABELS``/``SOURCE_COLORS``/``SourceOverlay`` include a fifth
``"browse"`` option (webpage, manual download) beyond the four Export
Profile originally needed (nexus/direct/bundle/ignore) — real,
installer-recognized Nexus-collection vocabulary (distinct from
``"direct"``, an auto-downloadable URL) that only the collection exporter
currently exposes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, QT_TRANSLATE_NOOP, QCoreApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QCheckBox, QFrame, QRadioButton, QButtonGroup, QListWidget,
    QListWidgetItem, QAbstractItemView,
)

from gui_qt.theme.theme_qt import active_palette, _c, contrast_text


SOURCE_LABELS = {
    "nexus":  QT_TRANSLATE_NOOP("ModRowWidgets", "Nexus"),
    "modio":  QT_TRANSLATE_NOOP("ModRowWidgets", "mod.io"),
    "direct": QT_TRANSLATE_NOOP("ModRowWidgets", "Direct"),
    "browse": QT_TRANSLATE_NOOP("ModRowWidgets", "Browse"),
    "bundle": QT_TRANSLATE_NOOP("ModRowWidgets", "Bundle"),
    "ignore": QT_TRANSLATE_NOOP("ModRowWidgets", "Ignore"),
}

# Per-source button colours — matched to the Tk workshop (_source_btn_style):
# Nexus orange, Direct green, Bundle purple, Ignore grey. Browse and mod.io
# (new) get their own distinct colours so neither is confused with Direct or
# each other. Text is white on all.
SOURCE_COLORS = {
    "nexus":  ("#c77a3a", "#d98c4c"),   # (base, hover)
    "modio":  ("#1f8a70", "#2ba384"),
    "direct": ("#5a7a5a", "#6b8b6b"),
    "browse": ("#3a6a7a", "#4c8a9a"),
    "bundle": ("#7a5a7a", "#8b6b8b"),
    "ignore": ("#555555", "#666666"),
}


def source_button_qss(source: str) -> str:
    base, hover = SOURCE_COLORS.get(source, SOURCE_COLORS["nexus"])
    return (f"QPushButton {{ background:{base}; color:{contrast_text(base)}; border:none;"
            f" border-radius:4px; padding:3px 12px; font-weight:600; }}"
            f"QPushButton:hover {{ background:{hover}; }}")


def card_qss(p) -> str:
    """Full stylesheet for the borderless overlay card and its contents.

    Everything is scoped by object name / type so nothing leaks onto the
    dimmed backdrop (``#CardBackdrop``) or renders with a stray black fill."""
    c = lambda k: _c(p, k)
    return f"""
    #CardBackdrop {{ background: rgba(0,0,0,150); }}
    #OverlayCard {{
        background: {c('BG_HEADER')};
        border: 1px solid {c('BORDER')};
        border-radius: 10px;
    }}
    #OverlayCard QLabel {{ background: transparent; }}
    #CardTitle {{ color: {c('TEXT_MAIN')}; font-weight: 700; font-size: 16px; }}
    #CardSub {{ color: {c('TEXT_DIM')}; font-size: 13px; }}
    /* Radio rows — a subtle pill that lights up on hover / selection. */
    #CardOption {{
        background: {c('BG_DEEP')};
        border: 1px solid transparent;
        border-radius: 6px;
    }}
    #CardOption:hover {{ border: 1px solid {c('BORDER')}; }}
    #CardOption QRadioButton {{
        background: transparent;
        color: {c('TEXT_MAIN')};
        padding: 8px 10px;
        font-size: 13px;
    }}
    #CardOption QRadioButton::indicator {{ width: 15px; height: 15px; }}
    #OverlayCard QLineEdit {{
        background: {c('BG_DEEP')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
        padding: 5px 8px;
    }}
    #OverlayCard QLineEdit:focus {{ border: 1px solid {c('ACCENT')}; }}
    #OverlayCard QListWidget {{
        background: {c('BG_DEEP')};
        border: 1px solid {c('BORDER')};
        border-radius: 6px;
    }}
    /* Buttons — Apply (green #GameSelectBtn) inherits the app QSS; give
       Cancel a real neutral style so it isn't a plain black rectangle. */
    #CardCancelBtn {{
        background: {c('BG_ROW')};
        color: {c('TEXT_MAIN')};
        border: 1px solid {c('BORDER')};
        border-radius: 5px;
        padding: 6px 16px;
        font-weight: 600;
    }}
    #CardCancelBtn:hover {{ background: {c('BG_ROW_HOVER')}; }}
    #GameSelectBtn {{
        background: {c('BTN_SUCCESS')};
        color: #ffffff;
        border: none;
        border-radius: 5px;
        padding: 6px 18px;
        font-weight: 600;
    }}
    #GameSelectBtn:hover {{ background: {c('BTN_SUCCESS_HOV')}; }}
    """


# ---------------------------------------------------------------------------
# Borderless overlay base (mirrors nexus_file_chooser.NexusFileChooser) — a
# dimmed, click-absorbing backdrop with a centered card, anchored to the
# top-level window so it covers the whole app (Steam Deck gaming-mode safe:
# a real top-level window can open BEHIND the app).
# ---------------------------------------------------------------------------

class CardOverlay(QWidget):
    CARD_W = 460
    CARD_H = 300

    def __init__(self, host: QWidget):
        super().__init__(host)
        self._host = host
        self._done = False
        p = active_palette()
        # Scope the dim backdrop to *this* widget by object name — an
        # unqualified ``background`` rule is inherited by every child widget
        # (radios/buttons render black otherwise).
        self.setObjectName("CardBackdrop")
        self.setStyleSheet(card_qss(p))
        self.setGeometry(host.rect())
        self._card = QFrame(self)
        self._card.setObjectName("OverlayCard")
        self._body = QVBoxLayout(self._card)
        self._body.setContentsMargins(20, 18, 20, 18)
        self._body.setSpacing(10)

    def _show_over(self):
        self._host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(300, w), max(200, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _finish(self):
        if self._done:
            return
        self._done = True
        self._host.removeEventFilter(self)
        self.hide()
        self.deleteLater()

    def mousePressEvent(self, event):
        if not self._card.geometry().contains(event.position().toPoint()):
            self._cancel()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._cancel()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

    # Subclasses override.
    def _cancel(self):
        self._finish()


def card_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("CardTitle")
    lbl.setWordWrap(True)
    return lbl


def card_button_bar(overlay, ok_text, on_ok, cancel_text=None):
    if cancel_text is None:
        cancel_text = QCoreApplication.translate("ModRowWidgets", "Cancel")
    bar = QHBoxLayout()
    bar.setSpacing(8)
    bar.addStretch(1)
    cancel = QPushButton(cancel_text)
    cancel.setObjectName("CardCancelBtn")
    cancel.setCursor(Qt.PointingHandCursor)
    cancel.clicked.connect(overlay._cancel)
    bar.addWidget(cancel)
    ok = QPushButton(ok_text)
    ok.setObjectName("GameSelectBtn")     # green, matches the file chooser
    ok.setCursor(Qt.PointingHandCursor)
    ok.clicked.connect(on_ok)
    bar.addWidget(ok)
    return bar


# ---------------------------------------------------------------------------
# Source picker
# ---------------------------------------------------------------------------

_SOURCE_OPTIONS = (
    ("nexus",  QT_TRANSLATE_NOOP("ModRowWidgets", "Nexus Mods"),
     QT_TRANSLATE_NOOP("ModRowWidgets", "Download mod from Nexus")),
    ("modio",  QT_TRANSLATE_NOOP("ModRowWidgets", "mod.io"),
     QT_TRANSLATE_NOOP("ModRowWidgets", "This mod's real mod.io page — the user downloads it from there")),
    ("direct", QT_TRANSLATE_NOOP("ModRowWidgets", "Direct URL"),
     QT_TRANSLATE_NOOP("ModRowWidgets", "For off-site mods with a directly downloadable link")),
    ("browse", QT_TRANSLATE_NOOP("ModRowWidgets", "Browse"),
     QT_TRANSLATE_NOOP("ModRowWidgets", "Off-site webpage — the user downloads it manually")),
    ("bundle", QT_TRANSLATE_NOOP("ModRowWidgets", "Bundle"),
     QT_TRANSLATE_NOOP("ModRowWidgets", "Include mod in the output (e.g. DynDOLOD output)")),
    ("ignore", QT_TRANSLATE_NOOP("ModRowWidgets", "Ignore"),
     QT_TRANSLATE_NOOP("ModRowWidgets", "Exclude this mod from the export entirely")),
)

_URL_VISIBLE_SOURCES = ("direct", "browse", "modio")


class SourceOverlay(CardOverlay):
    """Borderless in-window overlay: pick a mod's download source
    (Nexus / mod.io / Direct+URL / Browse / Bundle / Ignore). ``on_pick(source,
    url)`` on Apply. *allowed_sources* restricts which options are shown
    (default: everything but "modio", which only makes sense for a
    mod.io-identified row — Export Profile passes its original four, Create
    Collection adds "modio" only for rows where it applies)."""

    CARD_W = 480
    CARD_H = 480

    def __init__(self, host, mod_name, current_source, current_url, on_pick,
                allowed_sources=("nexus", "direct", "browse", "bundle", "ignore")):
        super().__init__(host)
        self._on_pick = on_pick
        self._body.addWidget(card_title(self.tr("Source — {0}").format(mod_name)))

        self._group = QButtonGroup(self)
        self._radios: dict[str, QRadioButton] = {}
        for value, label, desc in _SOURCE_OPTIONS:
            if value not in allowed_sources:
                continue
            rb = QRadioButton(self.tr("{0}   — {1}").format(self.tr(label), self.tr(desc)))
            rb.setChecked(value == current_source)
            self._group.addButton(rb)
            self._radios[value] = rb
            rb.toggled.connect(self._on_toggle)
            # Wrap in a styled pill row (#CardOption) for a cleaner look.
            row = QFrame()
            row.setObjectName("CardOption")
            row_l = QVBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.addWidget(rb)
            self._body.addWidget(row)

        url_row = QHBoxLayout()
        ulbl = QLabel(self.tr("Download URL:"))
        ulbl.setObjectName("CardSub")
        url_row.addWidget(ulbl)
        self._url = QLineEdit(current_url)
        self._url.setPlaceholderText(self.tr("https://…"))
        self._url.setMinimumHeight(30)
        url_row.addWidget(self._url, 1)
        self._url_row_w = QWidget()
        self._url_row_w.setLayout(url_row)
        self._body.addWidget(self._url_row_w)
        self._body.addStretch(1)
        self._body.addLayout(card_button_bar(self, self.tr("Apply"), self._apply))

        self._on_toggle()
        self._show_over()

    def _current(self) -> str:
        for value, rb in self._radios.items():
            if rb.isChecked():
                return value
        return "nexus"

    def _on_toggle(self):
        self._url_row_w.setVisible(self._current() in _URL_VISIBLE_SOURCES)

    def _apply(self):
        src = self._current()
        url = self._url.text().strip() if src in _URL_VISIBLE_SOURCES else ""
        cb = self._on_pick
        self._finish()
        if cb:
            cb(src, url)

    def _cancel(self):
        self._finish()


# ---------------------------------------------------------------------------
# Version / file picker
# ---------------------------------------------------------------------------

class VersionOverlay(CardOverlay):
    """Borderless in-window overlay: pick a preferred file version for a Nexus mod.
    Options are ``ver_options`` ({"label", "name", "size_bytes"}); ``on_pick(opt)``
    fires with the chosen option dict."""

    CARD_W = 460
    CARD_H = 380

    def __init__(self, host, mod_name, options, current_label, on_pick):
        super().__init__(host)
        self._on_pick = on_pick
        self._body.addWidget(card_title(self.tr("Version — {0}").format(mod_name)))
        sub = QLabel(self.tr("Preferred version (file id — version):"))
        sub.setObjectName("CardSub")
        self._body.addWidget(sub)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.set_options(options, current_label)
        self._list.itemDoubleClicked.connect(lambda _i: self._apply())
        self._body.addWidget(self._list, 1)
        self._body.addLayout(card_button_bar(self, self.tr("Select"), self._apply))
        self._show_over()

    def set_options(self, options, current_label):
        """(Re)populate the list — also used to refresh a still-open overlay
        when a background fetch completes after it was already shown (the
        first open always shows a single placeholder entry immediately,
        since the fetch hasn't had time to complete yet)."""
        self._list.clear()
        for opt in options:
            it = QListWidgetItem(opt.get("label", "—"))
            it.setData(Qt.UserRole, opt)
            self._list.addItem(it)
            if opt.get("label") == current_label:
                self._list.setCurrentItem(it)
        if self._list.currentRow() < 0 and self._list.count():
            self._list.setCurrentRow(0)

    def _apply(self):
        it = self._list.currentItem()
        result = it.data(Qt.UserRole) if it is not None else None
        cb = self._on_pick
        self._finish()
        if cb and result:
            cb(result)

    def _cancel(self):
        self._finish()


def center_checkbox(checked: bool, on_toggle) -> QWidget:
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignCenter)
    chk = QCheckBox()
    chk.setChecked(checked)
    chk.toggled.connect(on_toggle)
    lay.addWidget(chk)
    return wrap

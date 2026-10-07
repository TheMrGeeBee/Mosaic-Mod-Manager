"""Details window for one Wabbajack gallery modlist, opened by clicking its
card in the Wabbajack browser (modelled on Jackify's modlist detail dialog).

A large cover; version, author, game and maintainers; sizes; tag chips; the
full description; Discord / Readme / Website links; and Close /
Install Modlist. Install Modlist downloads the list's ``.wabbajack`` file
(``Utils.wabbajack.wabbajack_gallery.fetch_modlist_file``: Wabbajack's CDN,
GitHub or Google Drive, checked against the gallery's hash) with a progress
bar here -- some are close to a gigabyte -- and finishes with the file's path,
which the browser hands to the import screen. ``on_done(Path)`` once fetched,
``on_done(None)`` on Close / Esc (which also cancels a running download).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea, QWidget,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import _c, active_palette
from gui_qt.wabbajack.wabbajack_card import badges_for, fmt_bytes, size_line

IMG_W, IMG_H = 960, 380
MAX_TAGS = 10


class WabbajackDetailsOverlay(OverlayBase):
    CARD_W = 1000
    CARD_H = 820
    MIN_W = 560
    MIN_H = 460
    ESC_RESULT = None

    _progress = Signal(object, object)   # done, total
    _fetched = Signal(object, str)       # Path | None, error

    def __init__(self, host: QWidget, entry, files_dir, on_done, log_fn=None):
        super().__init__(host, on_done=on_done)
        self.entry = entry
        self._files_dir = files_dir
        self._log = log_fn or (lambda m: None)
        self._cancel = threading.Event()
        self._downloading = False
        p = active_palette()
        dim = _c(p, "TEXT_DIM")
        _card, v = self._make_card("WabbajackDetailsCard", margins=(0, 0, 0, 16), spacing=8)

        self._img = QLabel("…")
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setMinimumHeight(160)
        self._img.setMaximumHeight(IMG_H)
        self._img.setStyleSheet(f"background:{_c(p,'BG_DEEP')}; color:{dim};"
                                f" border-top-left-radius:8px; border-top-right-radius:8px;")
        v.addWidget(self._img, 3)

        def padded(widget, stretch=0):
            row = QHBoxLayout()
            row.setContentsMargins(20, 0, 20, 0)
            row.addWidget(widget, stretch)
            v.addLayout(row)

        title_row = QWidget()
        tr = QHBoxLayout(title_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(8)
        title = QLabel(entry.title)
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:18px;")
        tr.addWidget(title)
        for b in badges_for(entry, p):
            tr.addWidget(b)
        tr.addStretch(1)
        padded(title_row)

        bits = []
        if entry.version:
            bits.append(self.tr("version {0}").format(entry.version))
        if entry.author:
            bits.append(self.tr("by {0}").format(entry.author))
        meta = "  ".join(bits)
        extra = [entry.mosaic_game or entry.game]
        if entry.maintainers:
            extra.append(self.tr("Maintained by {0}").format(", ".join(entry.maintainers)))
        meta_lbl = QLabel(" • ".join([m for m in [meta] + extra if m]))
        meta_lbl.setWordWrap(True)
        meta_lbl.setStyleSheet(f"color:{dim}; font-size:13px;")
        padded(meta_lbl)

        sizes = QLabel(size_line(entry) + "  |  " + self.tr("{0} archives").format(
            entry.archive_count))
        sizes.setStyleSheet(f"color:{dim}; font-size:12px;")
        padded(sizes)

        if entry.tags:
            chips = QWidget()
            cl = QHBoxLayout(chips)
            cl.setContentsMargins(0, 2, 0, 2)
            cl.setSpacing(6)
            for t in entry.tags[:MAX_TAGS]:
                c = QLabel(t)
                c.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; background:{_c(p,'BG_DEEP')};"
                                f" border:1px solid {_c(p,'BORDER')}; border-radius:4px;"
                                f" padding:2px 8px; font-size:12px;")
                cl.addWidget(c)
            if len(entry.tags) > MAX_TAGS:
                more = QLabel(f"+{len(entry.tags) - MAX_TAGS}")
                more.setToolTip(", ".join(entry.tags[MAX_TAGS:]))
                more.setStyleSheet(f"color:{dim}; font-size:12px;")
                cl.addWidget(more)
            cl.addStretch(1)
            padded(chips)

        desc_head = QLabel(self.tr("Description:"))
        desc_head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        padded(desc_head)
        desc = QLabel(entry.description or self.tr("No description."))
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        desc.setTextInteractionFlags(Qt.TextSelectableByMouse)
        desc.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px; padding:8px;")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(desc)
        scroll.setMinimumHeight(70)
        scroll.setStyleSheet(f"QScrollArea {{ background:{_c(p,'BG_DEEP')};"
                             f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; }}")
        padded(scroll, 1)
        v.setStretch(v.count() - 1, 2)

        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(True)
        self._progress_bar.hide()
        padded(self._progress_bar)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{dim}; font-size:12px;")
        self._status.hide()
        padded(self._status)

        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        links = [(self.tr("Discord"), entry.discord_url), (self.tr("Readme"), entry.readme_url),
                 (self.tr("Website"), entry.website_url)]
        if any(url for _, url in links):
            lbl = QLabel(self.tr("Links:"))
            lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
            bl.addWidget(lbl)
        for text, url in links:
            if url and not (text == self.tr("Website") and url == entry.readme_url):
                b = QPushButton(text)
                b.setObjectName("FormButton")
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, u=url: self._open_url(u))
                bl.addWidget(b)
        bl.addStretch(1)
        self._close = QPushButton(self.tr("Close"))
        self._close.setObjectName("FormButton")
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.clicked.connect(self._on_close)
        bl.addWidget(self._close)
        self._install = QPushButton(self.tr("Install Modlist"))
        self._install.setObjectName("PrimaryButton")
        self._install.setCursor(Qt.PointingHandCursor)
        self._install.clicked.connect(self._start_fetch)
        if entry.force_down:
            self._install.setEnabled(False)
            self._install.setToolTip(self.tr("The authors have marked this modlist unavailable."))
        elif not entry.mosaic_game:
            self._install.setEnabled(False)
            self._install.setToolTip(self.tr("Mosaic doesn't support this game yet."))
        bl.addWidget(self._install)
        padded(bar)

        self._progress.connect(self._on_progress)
        self._fetched.connect(self._on_fetched)
        self._present()

    @classmethod
    def show_over(cls, host, entry, files_dir, on_done, log_fn=None):
        top = host.window() if host is not None else None
        return cls(top or host, entry, files_dir, on_done, log_fn=log_fn)

    def set_cover(self, pm) -> None:
        if pm is None or pm.isNull():
            return
        self._img.setText("")
        self._img.setPixmap(pm.scaled(self._img.width(), self._img.height(),
                                      Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))

    def _open_url(self, url: str) -> None:
        from Utils.xdg import open_url
        open_url(url, log_fn=self._log)

    # -- download -----------------------------------------------------------------
    def _start_fetch(self) -> None:
        if self._downloading:
            return
        self._downloading = True
        self._cancel.clear()
        self._install.setEnabled(False)
        self._install.setText(self.tr("Downloading…"))
        self._close.setText(self.tr("Cancel"))
        self._progress_bar.setRange(0, 0)
        self._progress_bar.show()
        self._status.setText(self.tr("Downloading the modlist file ({0})…").format(
            fmt_bytes(self.entry.file_size)))
        self._status.show()
        entry, folder, cancel = self.entry, self._files_dir, self._cancel

        def work():
            from Utils.wabbajack.wabbajack_gallery import fetch_modlist_file
            try:
                path = fetch_modlist_file(
                    entry, folder, cancel=cancel,
                    progress_cb=lambda done, total: safe_emit(self._progress, done, total))
                safe_emit(self._fetched, path, "")
            except Exception as exc:
                safe_emit(self._fetched, None, str(exc))

        threading.Thread(target=work, daemon=True, name="wabbajack-fetch-list").start()

    def _on_progress(self, done, total) -> None:
        total = total or self.entry.file_size
        if total:
            self._progress_bar.setRange(0, 1000)
            self._progress_bar.setValue(int(min(done, total) * 1000 / total))
            self._progress_bar.setFormat(f"{fmt_bytes(done)} / {fmt_bytes(total)}")

    def _on_fetched(self, path, error) -> None:
        self._downloading = False
        if self._done:
            return
        if path is not None:
            self._finish(path)
            return
        self._progress_bar.hide()
        self._install.setEnabled(True)
        self._install.setText(self.tr("Install Modlist"))
        self._close.setText(self.tr("Close"))
        if self._cancel.is_set():
            self._status.setText(self.tr("Download cancelled."))
        else:
            self._log(f"Wabbajack gallery: {error}")
            self._status.setText(self.tr("Couldn't download the modlist file: {0}").format(error))

    def _on_close(self) -> None:
        if self._downloading:
            self._cancel.set()
            return
        self._finish(None)

    def _finish(self, result=None):
        self._cancel.set()
        super()._finish(result)

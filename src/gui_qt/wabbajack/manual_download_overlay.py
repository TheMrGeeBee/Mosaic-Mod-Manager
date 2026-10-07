"""In-window prompt for one Wabbajack archive that needs a manual download.

Shown (one archive at a time) for an archive with no automatic downloader --
a ``ManualDownloader`` page -- or whose automatic download failed. Offers to
open the archive's web page; while open it watches the user's download
folders (``Utils.wabbajack.manual_downloads``) and finishes by itself once a
file with the right size and hash appears there, which is how LoversLab and
MediaFire files in real lists get downloaded. Picking the file by hand still
works. ``on_done(Path)`` with the file, ``on_done(None)`` to skip it,
``on_done(CANCEL_INSTALL)`` to stop the whole install. The installer checks
the file against the modlist's hash.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from gui_qt.overlay_base import OverlayBase
from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import _c, active_palette
from Utils.collections.collection_manifest import fmt_size
from Utils.wabbajack.downloaders.nexus_source import nexus_domain_for
from Utils.wabbajack.wabbajack_manifest import (
    Archive,
    GoogleDriveState,
    HttpState,
    LoversLabState,
    ManualState,
    MediaFireState,
    MegaState,
    NexusState,
)


CANCEL_INSTALL = "cancel-install"   # on_done() value: stop the whole install


def archive_page_url(archive: Archive) -> str:
    """Where a person would go to download ``archive`` by hand ('' if unknown)."""
    s = archive.state
    if isinstance(s, NexusState) and s.mod_id:
        url = f"https://www.nexusmods.com/{nexus_domain_for(s.game_name)}/mods/{s.mod_id}?tab=files"
        return url + (f"&file_id={s.file_id}" if s.file_id else "")
    if isinstance(s, GoogleDriveState) and s.file_id:
        return f"https://drive.google.com/file/d/{s.file_id}/view"
    if isinstance(s, (ManualState, HttpState, MediaFireState, MegaState, LoversLabState)):
        # Curators sometimes leave a note after the link (real case:
        # ".../MiscHairstyle1.6_by_Atherisz.7z/file # MiscHairstyles1.6").
        parts = (s.url or "").split()
        return parts[0] if parts else ""
    return ""


class ManualDownloadOverlay(OverlayBase):
    CARD_W = 620
    CARD_H = 330
    ESC_RESULT = None

    _picked = Signal(object)  # Path | None from the portal picker (worker thread)
    _found = Signal(object)   # Path | None from a download-folder scan (worker thread)
    SCAN_MS = 1500

    def __init__(self, host: QWidget, archive: Archive, reason: str, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        self._url = archive_page_url(archive)
        _card, v = self._make_card("WabbajackManualCard")

        title = QLabel(self.tr("Download needed"))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        name = QLabel(f"{archive.name}  ({fmt_size(archive.size)})")
        name.setWordWrap(True)
        name.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        v.addWidget(name)

        prompt = getattr(archive.state, "prompt", "")
        lines = [self.tr("Mosaic couldn't download this file automatically ({0}).").format(reason)
                 if reason else self.tr("This file has to be downloaded by hand.")]
        if prompt:
            lines.append(prompt)
        lines.append(self.tr("Download it in your browser: Mosaic notices it in your "
                             "downloads and carries on. Or choose the file yourself. "
                             "Either way it's checked against what the modlist expects."))
        body = QLabel("\n\n".join(lines))
        body.setWordWrap(True)
        body.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        v.addWidget(body)

        from Utils.wabbajack.manual_downloads import DownloadFinder, download_folders
        folders = download_folders()
        self._finder = DownloadFinder(archive.size, archive.hash, folders)
        self._scanning = False
        self._watch = QLabel(
            self.tr("Watching {0} for it…").format(", ".join(str(f) for f in folders))
            if folders else
            self.tr("No download folder to watch (see Settings ▸ Downloads): "
                    "choose the file when it's downloaded."))
        self._watch.setWordWrap(True)
        self._watch.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px; font-style:italic;")
        v.addWidget(self._watch)
        v.addStretch(1)

        bar = QHBoxLayout()
        if self._url:
            open_btn = QPushButton(self.tr("Open download page"))
            open_btn.setObjectName("FormButton")
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.clicked.connect(self._open_page)
            bar.addWidget(open_btn)
        bar.addStretch(1)
        stop = QPushButton(self.tr("Cancel install"))
        stop.setObjectName("FormButton")
        stop.setCursor(Qt.PointingHandCursor)
        stop.setToolTip(self.tr("Stop installing this modlist. Files downloaded so far "
                                "stay in the download cache."))
        stop.clicked.connect(lambda: self._finish(CANCEL_INSTALL))
        bar.addWidget(stop)
        skip = QPushButton(self.tr("Skip"))
        skip.setObjectName("FormButton")
        skip.setCursor(Qt.PointingHandCursor)
        skip.clicked.connect(lambda: self._finish(None))
        bar.addWidget(skip)
        self._choose = QPushButton(self.tr("Choose file…"))
        self._choose.setObjectName("PrimaryButton")
        self._choose.setCursor(Qt.PointingHandCursor)
        self._choose.clicked.connect(self._choose_file)
        bar.addWidget(self._choose)
        v.addLayout(bar)

        self._picked.connect(self._on_picked)
        self._found.connect(self._on_found)
        self._timer = QTimer(self)
        self._timer.setInterval(self.SCAN_MS)
        self._timer.timeout.connect(self._scan)
        if folders:
            self._timer.start()
        self._present()

    @classmethod
    def show_over(cls, host, archive, reason, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, archive, reason, on_done)

    def _open_page(self):
        from Utils.xdg import open_url
        open_url(self._url)

    def _choose_file(self):
        from Utils.wine_proton.portal_filechooser import pick_file
        self._choose.setEnabled(False)
        pick_file(self.tr("Choose the downloaded file"),
                  lambda path: safe_emit(self._picked, path),
                  filters=[("All files", ["*"])])

    def _scan(self):
        if self._done or self._scanning:
            return
        self._scanning = True
        finder = self._finder

        def work():
            try:
                found = finder.find()
            except Exception:
                found = None
            safe_emit(self._found, found)

        threading.Thread(target=work, daemon=True, name="wabbajack-watch-downloads").start()

    def _on_found(self, path):
        self._scanning = False
        if path is not None and not self._done:
            self._finish(Path(path))

    def _finish(self, result=None):
        self._timer.stop()
        super()._finish(result)

    def _on_picked(self, path):
        if self._done:
            return
        self._choose.setEnabled(True)
        if path:
            self._finish(Path(path))

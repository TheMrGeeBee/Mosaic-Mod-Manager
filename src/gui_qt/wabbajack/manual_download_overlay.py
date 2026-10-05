"""In-window prompt for one Wabbajack archive that needs a manual download.

Shown (one archive at a time) for an archive with no automatic downloader --
a ``ManualDownloader`` page -- or whose automatic download failed. Offers to
open the archive's web page and to pick the downloaded file.
``on_done(Path)`` with the chosen file, ``on_done(None)`` to skip it. The
installer checks the chosen file against the modlist's hash.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
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


def archive_page_url(archive: Archive) -> str:
    """Where a person would go to download ``archive`` by hand ('' if unknown)."""
    s = archive.state
    if isinstance(s, NexusState) and s.mod_id:
        url = f"https://www.nexusmods.com/{nexus_domain_for(s.game_name)}/mods/{s.mod_id}?tab=files"
        return url + (f"&file_id={s.file_id}" if s.file_id else "")
    if isinstance(s, GoogleDriveState) and s.file_id:
        return f"https://drive.google.com/file/d/{s.file_id}/view"
    if isinstance(s, (ManualState, HttpState, MediaFireState, MegaState, LoversLabState)):
        return s.url
    return ""


class ManualDownloadOverlay(OverlayBase):
    CARD_W = 520
    CARD_H = 290
    ESC_RESULT = None

    _picked = Signal(object)  # Path | None from the portal picker (worker thread)

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
        lines.append(self.tr("Download it, then choose the downloaded file. "
                             "Mosaic checks it matches what the modlist expects."))
        body = QLabel("\n\n".join(lines))
        body.setWordWrap(True)
        body.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        v.addWidget(body)
        v.addStretch(1)

        bar = QHBoxLayout()
        if self._url:
            open_btn = QPushButton(self.tr("Open download page"))
            open_btn.setObjectName("FormButton")
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.clicked.connect(self._open_page)
            bar.addWidget(open_btn)
        bar.addStretch(1)
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

    def _on_picked(self, path):
        if self._done:
            return
        self._choose.setEnabled(True)
        if path:
            self._finish(Path(path))

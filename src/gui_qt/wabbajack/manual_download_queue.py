"""The manual-download queue shown inside the Wabbajack preflight screen.

A real modlist can need a lot of files that no downloader can fetch -- the
VtMB list "Fashionista - Steam" has 77. The install-time prompt
(``manual_download_overlay``) handles those strictly one at a time: it asks
for archive 1, waits, then asks for archive 2. That makes archive 5 invisible
even when it is already sitting in the Downloads folder, and one archive that
never arrives blocks every other one.

This pane queues them instead. It watches the download folders for *all*
outstanding archives at once (:class:`MultiDownloadFinder`), so files are
picked up in whatever order the user downloads them, and it can open the next
few pages in the browser by itself so there is no click needed per file.

Downloads themselves are still one-at-a-time -- the browser does them, and
the user saves each one as it finishes. What this removes is the clicking,
not the waiting.

The pane resolves nothing by itself: it reports each file it finds through
``on_resolved`` and the controller re-runs preflight, exactly as the
LoversLab login fix does.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import _c, active_palette
from gui_qt.wabbajack.manual_download_overlay import archive_page_url
from Utils.collections.collection_manifest import fmt_size

SCAN_MS = 1500

# A scan still "in flight" after this long is treated as lost, not pending.
# Carried over from manual_download_overlay: a single dropped signal used to
# latch the scanning flag on for good and hang an install for 40+ minutes.
_SCAN_STUCK_MS = 60_000


def pages_to_open(pending: "list", already_open: "set[str]", limit: int) -> "list":
    """The next archives whose pages should be opened in the browser.

    *pending* is every archive still wanted, in display order; *already_open*
    holds the hashes whose page is open and whose file has not arrived or been
    skipped yet; *limit* is how many pages may be open at once.

    A slot frees when a file lands or the user skips it, so with ``limit=1``
    this hands back one page at a time -- the behaviour before the setting
    existed. Returns ``[]`` when every slot is busy.
    """
    free = max(0, int(limit) - len(already_open))
    if free <= 0:
        return []
    out = []
    for archive in pending:
        if len(out) >= free:
            break
        if archive.hash not in already_open:
            out.append(archive)
    return out


class ManualDownloadQueue(QWidget):
    """Queue of archives the user must fetch in a browser.

    ``on_resolved(hash, path)`` fires for each file found or chosen, and
    ``on_changed()`` whenever the counts move, so the host can re-render.
    """

    _found = Signal(object)      # {hash: Path} from a worker scan
    _verified = Signal(object)   # (archive, Path | None) from a hand-picked file

    def __init__(self, archives, *, on_resolved=None, on_changed=None, parent=None):
        super().__init__(parent)
        from Utils.ui_config import (WJ_TABS_AT_ONCE_MAX,
                                     load_wabbajack_manual_settings)
        from Utils.wabbajack.manual_downloads import (MultiDownloadFinder,
                                                      download_folders)

        self._p = active_palette()
        self._on_resolved = on_resolved
        self._on_changed = on_changed
        self._archives = list(archives)
        self._total = len(self._archives)
        self._resolved: "dict[str, Path]" = {}
        self._skipped: "set[str]" = set()
        self._opened: "set[str]" = set()
        self._scanning = False
        self._scan_started = 0.0
        self._expanded = False
        self._rows: "dict[str, QLabel]" = {}

        self._folders = [Path(f) for f in download_folders()]
        self._finder = MultiDownloadFinder(self._archives, self._folders)

        settings = load_wabbajack_manual_settings()
        self._tabs_max = WJ_TABS_AT_ONCE_MAX

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 6, 0, 0)
        outer.setSpacing(6)

        card = QFrame()
        card.setObjectName("WabbajackManualQueue")
        card.setStyleSheet(
            f"#WabbajackManualQueue{{background:{_c(self._p,'BG_CARD')};"
            f"border:1px solid {_c(self._p,'BORDER')};border-radius:8px;}}")
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        outer.addWidget(card)

        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        self._head = QLabel()
        self._head.setStyleSheet(
            f"color:{_c(self._p,'TEXT_MAIN')}; font-weight:600; font-size:13px;")
        head_row.addWidget(self._head)
        head_row.addStretch(1)
        self._toggle = QPushButton()
        self._toggle.setObjectName("FormButton")
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.clicked.connect(self._toggle_all)
        head_row.addWidget(self._toggle)
        v.addLayout(head_row)

        self._name = QLabel()
        self._name.setWordWrap(True)
        self._name.setStyleSheet(f"color:{_c(self._p,'TEXT_MAIN')}; font-size:13px;")
        v.addWidget(self._name)

        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setStyleSheet(f"color:{_c(self._p,'TEXT_DIM')}; font-size:12px;")
        v.addWidget(self._note)

        # -- auto-open row ---------------------------------------------------
        auto = QHBoxLayout()
        auto.setSpacing(8)
        self._auto = QCheckBox(self.tr("Open the next one automatically"))
        self._auto.setChecked(bool(settings.get("auto_open")))
        self._auto.setCursor(Qt.PointingHandCursor)
        self._auto.setStyleSheet(f"color:{_c(self._p,'TEXT_MAIN')}; font-size:12px;")
        self._auto.toggled.connect(self._settings_changed)
        auto.addWidget(self._auto)
        tabs_label = QLabel(self.tr("tabs open at once:"))
        tabs_label.setStyleSheet(f"color:{_c(self._p,'TEXT_DIM')}; font-size:12px;")
        auto.addWidget(tabs_label)
        self._tabs = QSpinBox()
        self._tabs.setRange(1, self._tabs_max)
        self._tabs.setValue(int(settings.get("tabs_at_once", 1)))
        self._tabs.setFixedWidth(60)
        self._tabs.setToolTip(self.tr(
            "How many download pages may be open in your browser at the same "
            "time. A slot frees up when a file arrives or you skip it."))
        self._tabs.valueChanged.connect(self._settings_changed)
        auto.addWidget(self._tabs)
        auto.addStretch(1)
        v.addLayout(auto)

        # -- watched folders -------------------------------------------------
        self._watch = QLabel()
        self._watch.setWordWrap(True)
        self._watch.setStyleSheet(
            f"color:{_c(self._p,'TEXT_DIM')}; font-size:12px; font-style:italic;")
        v.addWidget(self._watch)

        # -- buttons ---------------------------------------------------------
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._open_btn = QPushButton(self.tr("Open page"))
        self._open_btn.setObjectName("PrimaryButton")
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.clicked.connect(self._open_current)
        bar.addWidget(self._open_btn)
        self._folder_btn = QPushButton(self.tr("Change folder"))
        self._folder_btn.setObjectName("FormButton")
        self._folder_btn.setCursor(Qt.PointingHandCursor)
        self._folder_btn.clicked.connect(self._change_folder)
        bar.addWidget(self._folder_btn)
        bar.addStretch(1)
        self._skip_btn = QPushButton(self.tr("Skip for now"))
        self._skip_btn.setObjectName("FormButton")
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.setToolTip(self.tr(
            "Leave this one for the install to ask about again."))
        self._skip_btn.clicked.connect(self._skip_current)
        bar.addWidget(self._skip_btn)
        self._pick_btn = QPushButton(self.tr("I downloaded it elsewhere"))
        self._pick_btn.setObjectName("FormButton")
        self._pick_btn.setCursor(Qt.PointingHandCursor)
        self._pick_btn.clicked.connect(self._choose_file)
        bar.addWidget(self._pick_btn)
        v.addLayout(bar)

        # -- the full queue, hidden until "Show all N" ------------------------
        self._list = QScrollArea()
        self._list.setWidgetResizable(True)
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setMaximumHeight(220)
        self._list.setVisible(False)
        holder = QWidget()
        self._list_box = QVBoxLayout(holder)
        self._list_box.setContentsMargins(0, 0, 0, 0)
        self._list_box.setSpacing(2)
        for archive in self._archives:
            self._list_box.addWidget(self._list_row(archive))
        self._list_box.addStretch(1)
        self._list.setWidget(holder)
        v.addWidget(self._list)

        self._found.connect(self._on_found)
        self._verified.connect(self._on_verified)
        self._timer = QTimer(self)
        self._timer.setInterval(SCAN_MS)
        self._timer.timeout.connect(self._scan)
        if self._folders and self._archives:
            self._timer.start()
            # Catch files already downloaded before the pane opened, without
            # waiting out the first interval.
            QTimer.singleShot(0, self._scan)
        self._render()

    # -- state ---------------------------------------------------------------

    def resolved(self) -> "dict[str, Path]":
        return dict(self._resolved)

    def skipped_count(self) -> int:
        """How many archives the user explicitly gave up on.

        Preflight blocks the install while manual archives are outstanding, so
        a skip has to count as an answer -- otherwise one dead download page
        would block the install for good.
        """
        return len(self._skipped)

    def pending(self) -> list:
        """Still wanted, in display order: not found and not skipped."""
        return [a for a in self._archives
                if a.hash not in self._resolved and a.hash not in self._skipped]

    def _current(self):
        pend = self.pending()
        return pend[0] if pend else None

    def stop(self) -> None:
        """Stop watching -- the pane is going away or the install has begun."""
        self._timer.stop()

    # -- rendering -----------------------------------------------------------

    def _list_row(self, archive) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        mark = QLabel("·")
        mark.setFixedWidth(14)
        mark.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        h.addWidget(mark)
        self._rows[archive.hash] = mark
        name = QLabel(f"{archive.name or archive.hash}  ({fmt_size(archive.size)})")
        name.setWordWrap(True)
        name.setStyleSheet(f"color:{_c(self._p,'TEXT_DIM')}; font-size:12px;")
        h.addWidget(name, 1)
        url = archive_page_url(archive)
        if url:
            btn = QPushButton(self.tr("Open page"))
            btn.setObjectName("FormButton")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, a=archive: self._open_page(a))
            h.addWidget(btn)
        return row

    def _render(self) -> None:
        done = len(self._resolved)
        current = self._current()
        pend = self.pending()

        if current is None:
            if done >= self._total:
                self._head.setText(self.tr("All {0} downloads are here").format(self._total))
            else:
                self._head.setText(
                    self.tr("{0} of {1} downloaded, {2} skipped").format(
                        done, self._total, len(self._skipped)))
            self._name.setText("")
            self._note.setText(self.tr(
                "Skipped files are asked for again during the install.")
                if self._skipped else "")
            self._watch.setText("")
            for btn in (self._open_btn, self._skip_btn, self._pick_btn):
                btn.setEnabled(False)
        else:
            self._head.setText(self.tr("Manual download {0} of {1}").format(
                self._total - len(pend) + 1, self._total))
            self._name.setText(
                f"<b>{_escape(current.name or current.hash)}</b>"
                f"&nbsp;&nbsp;{_escape(fmt_size(current.size))}")
            self._name.setTextFormat(Qt.RichText)
            note = getattr(current.state, "prompt", "") or self.tr(
                "The list's author marked this file as one to download in a browser.")
            self._note.setText(
                note + "\n" + self.tr(
                    "Start the download in your browser; the file is picked up "
                    "here automatically."))
            self._watch.setText(
                self.tr("Watching {0} for it…").format(
                    ", ".join(str(f) for f in self._folders))
                if self._folders else
                self.tr("No download folder to watch — use Change folder, or "
                        "pick the file once it's downloaded."))
            self._open_btn.setEnabled(bool(archive_page_url(current)))
            self._skip_btn.setEnabled(True)
            self._pick_btn.setEnabled(True)

        self._toggle.setText(
            self.tr("Hide the list ⌃") if self._expanded
            else self.tr("Show all {0} ⌄").format(self._total))

    def _mark(self, archive_hash: str, glyph: str, key: str) -> None:
        label = self._rows.get(archive_hash)
        if label is not None:
            label.setText(glyph)
            label.setStyleSheet(f"color:{_c(self._p, key)}; font-weight:700; font-size:13px;")

    # -- actions -------------------------------------------------------------

    def _toggle_all(self) -> None:
        self._expanded = not self._expanded
        self._list.setVisible(self._expanded)
        self._render()

    def _settings_changed(self, *_a) -> None:
        from Utils.ui_config import save_wabbajack_manual_settings
        try:
            save_wabbajack_manual_settings(self._auto.isChecked(), self._tabs.value())
        except Exception:
            pass
        self._maybe_auto_open()

    def _open_page(self, archive) -> None:
        from Utils.xdg import open_url
        url = archive_page_url(archive)
        if url:
            self._opened.add(archive.hash)
            open_url(url)

    def _open_current(self) -> None:
        current = self._current()
        if current is not None:
            self._open_page(current)

    def _maybe_auto_open(self) -> None:
        """Open as many pages as the tab budget allows, when asked to."""
        if not self._auto.isChecked():
            return
        # Only pages still outstanding hold a slot.
        self._opened &= {a.hash for a in self.pending()}
        for archive in pages_to_open(self.pending(), self._opened, self._tabs.value()):
            self._open_page(archive)

    def _skip_current(self) -> None:
        current = self._current()
        if current is None:
            return
        self._skipped.add(current.hash)
        self._finder.resolve(current)
        self._mark(current.hash, "–", "TEXT_DIM")
        self._changed()

    def _change_folder(self) -> None:
        from Utils.wine_proton.portal_filechooser import pick_folder
        self._folder_btn.setEnabled(False)

        def done(path):
            self._folder_btn.setEnabled(True)
            if not path:
                return
            self._add_folder(Path(path))

        pick_folder(self.tr("Choose the folder your browser downloads to"), done)

    def _add_folder(self, folder: Path) -> None:
        from Utils.downloads.download_locations import (
            load_extra_download_locations, save_extra_download_locations)
        if folder in self._folders:
            return
        try:
            extras = list(load_extra_download_locations())
            if str(folder) not in extras:
                save_extra_download_locations(extras + [str(folder)])
        except Exception:
            pass
        self._folders.append(folder)
        self._finder.folders = list(self._folders)
        if not self._timer.isActive():
            self._timer.start()
        self._scan()
        self._render()

    def _choose_file(self) -> None:
        from Utils.wine_proton.portal_filechooser import pick_file
        current = self._current()
        if current is None:
            return
        self._pick_btn.setEnabled(False)
        pick_file(self.tr("Choose the downloaded file"),
                  lambda path: self._verify(current, path),
                  filters=[("All files", ["*"])])

    def _verify(self, archive, path) -> None:
        """Hash a hand-picked file off the UI thread -- these run to 1.4 GB."""
        def work():
            ok = None
            try:
                if path:
                    from Utils.wabbajack.wabbajack_hash import hash_file, hashes_match
                    if hashes_match(archive.hash, hash_file(Path(path))):
                        ok = Path(path)
            except Exception:
                ok = None
            safe_emit(self._verified, (archive, ok))

        threading.Thread(target=work, daemon=True,
                         name="wabbajack-verify-manual").start()

    def _on_verified(self, payload) -> None:
        archive, path = payload
        self._pick_btn.setEnabled(True)
        if path is None:
            self._note.setText(self.tr(
                "That file doesn't match what the modlist expects — "
                "check you picked the right one."))
            return
        self._finder.resolve(archive)
        self._resolve(archive.hash, path)

    # -- scanning ------------------------------------------------------------

    def _scan(self) -> None:
        if not self.pending():
            self._timer.stop()
            return
        if self._scanning:
            if (time.monotonic() - self._scan_started) * 1000 < _SCAN_STUCK_MS:
                return
        self._scanning = True
        self._scan_started = time.monotonic()
        finder = self._finder

        def work():
            hits = {}
            try:
                hits = finder.find_any()
            except Exception:
                hits = {}
            finally:
                # In the worker, so the flag clears even if delivery fails.
                self._scanning = False
                try:
                    safe_emit(self._found, hits)
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True,
                         name="wabbajack-watch-downloads").start()

    def _on_found(self, hits) -> None:
        self._scanning = False
        if not hits:
            return
        for archive_hash, path in hits.items():
            self._resolve(archive_hash, Path(path), quiet=True)
        self._changed()

    def _resolve(self, archive_hash: str, path: Path, quiet: bool = False) -> None:
        if archive_hash in self._resolved:
            return
        self._resolved[archive_hash] = path
        self._skipped.discard(archive_hash)
        self._mark(archive_hash, "✓", "TEXT_OK_BRIGHT")
        if self._on_resolved is not None:
            self._on_resolved(archive_hash, path)
        if not quiet:
            self._changed()

    def _changed(self) -> None:
        if not self.pending():
            self._timer.stop()
        self._render()
        # A file landing or being skipped is what frees a tab slot. Deliberately
        # not called from _render(): that runs on first paint, and a remembered
        # "open automatically" would then throw tabs at the user the moment the
        # preflight screen appeared, before they had asked for anything.
        self._maybe_auto_open()
        if self._on_changed is not None:
            self._on_changed()


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

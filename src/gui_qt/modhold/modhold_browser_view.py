"""Modhold browse tab: search, sort and page through a game's mods, then install.

Network work runs on worker threads and reaches the UI through signals
(``safe_emit``), as in the Nexus browser. A finished download is handed to the
app's install queue exactly like a Nexus one: ``install_fn([archive], {archive: meta})``
with a prebuilt meta that carries ``modhold_info`` (see Utils/mods/mod_install.py).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import requests
from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui_qt.modhold.modhold_file_chooser import ModholdFileChooser
from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import _c, active_palette, button_qss, err_text
from Modhold import modhold_key
from Modhold.browse_util import (
    SORTS,
    default_file_selection,
    is_gated,
    meta_from_detail,
    mod_badges,
    needs_chooser,
    numbers_line,
    page_count,
    prebuilt_nexus_meta,
)
from Modhold.modhold_api import ModholdAPI, ModholdError
from Modhold.modhold_download import ModholdDownloader, failure_reason
from Utils.config_paths import get_download_cache_dir_for_game

# Marks the sort labels (Modhold/browse_util.SORTS) for translation extraction.
_SORT_MARKERS = (
    QT_TRANSLATE_NOOP("ModholdBrowserView", "Recently updated"),
    QT_TRANSLATE_NOOP("ModholdBrowserView", "Newest"),
    QT_TRANSLATE_NOOP("ModholdBrowserView", "Most downloaded"),
    QT_TRANSLATE_NOOP("ModholdBrowserView", "Most vouched"),
    QT_TRANSLATE_NOOP("ModholdBrowserView", "Rising"),
    QT_TRANSLATE_NOOP("ModholdBrowserView", "Name"),
)

_COVER_W, _COVER_H = 128, 72
_SUMMARY_MAX = 220


class _ModCard(QFrame):
    """One mod row. Emits nothing itself; the view wires the buttons."""

    def __init__(self, mod: dict, on_install: Callable[[dict], None],
                 on_open: Callable[[dict], None], parent: QWidget | None = None):
        super().__init__(parent)
        p = active_palette()
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(f"_ModCard {{ border:1px solid {_c(p, 'BORDER_FAINT')}; "
                           f"border-radius:4px; }}")
        self.mod = mod
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(10)

        self.cover = QLabel()
        self.cover.setFixedSize(_COVER_W, _COVER_H)
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setStyleSheet(f"background:{_c(p, 'BG_PANEL')};")
        row.addWidget(self.cover)

        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel(f"<b>{_esc(mod.get('name', ''))}</b>")
        title.setWordWrap(True)
        title.setTextFormat(Qt.RichText)
        col.addWidget(title)
        author = (mod.get("author") or {}).get("name") or (mod.get("author") or {}).get("handle", "")
        sub = " · ".join(x for x in (self.tr("by {0}").format(author) if author else "",
                                     f"v{mod['version']}" if mod.get("version") else "",
                                     mod.get("category") or "") if x)
        col.addWidget(_dim_label(sub, p))
        summary = (mod.get("summary") or "").strip()
        if len(summary) > _SUMMARY_MAX:
            summary = summary[:_SUMMARY_MAX].rstrip() + "…"
        if summary:
            s = QLabel(summary)
            s.setWordWrap(True)
            col.addWidget(s)
        col.addWidget(_dim_label(numbers_line(mod), p))
        badges = mod_badges(mod)
        if badges:
            b = QLabel(" · ".join(badges))
            b.setStyleSheet(f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600;")
            col.addWidget(b)
        row.addLayout(col, 1)

        btns = QVBoxLayout()
        btns.setSpacing(4)
        install = QPushButton(self.tr("Install"))
        install.setCursor(Qt.PointingHandCursor)
        install.setStyleSheet(button_qss("BTN_INFO"))
        install.clicked.connect(lambda: on_install(mod))
        page = QPushButton(self.tr("Open page"))
        page.setCursor(Qt.PointingHandCursor)
        page.clicked.connect(lambda: on_open(mod))
        btns.addWidget(install)
        btns.addWidget(page)
        btns.addStretch(1)
        row.addLayout(btns)

    def set_cover(self, pm: QPixmap) -> None:
        self.cover.setPixmap(pm.scaled(_COVER_W, _COVER_H, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation))


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _dim_label(text: str, p) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')};")
    lbl.setWordWrap(True)
    return lbl


class ModholdBrowserView(QWidget):
    """Browse and install mods for one game from Modhold."""

    _page_sig = Signal(object, str)        # (result dict | None, error)
    _detail_sig = Signal(object, str)      # (mod detail dict | None, error)
    _cover_sig = Signal(int, bytes)        # (mod id, image bytes)
    # Byte counts: 64-bit, because a mod can be bigger than 2 GB (a C int would overflow).
    _progress_sig = Signal(str, str, "qlonglong", "qlonglong")
    _installed_sig = Signal(str, object, str)   # (archive path, prebuilt meta, error)

    def __init__(self, game, slug: str, install_fn, log_fn=None, progress_fn=None,
                 open_account_fn=None, api_factory=None, parent=None):
        super().__init__(parent)
        self._game = game
        self._slug = slug
        self._install_fn = install_fn
        self._log = log_fn or (lambda *_: None)
        self._progress_fn = progress_fn or (lambda key, name, d, t: None)
        self._open_account = open_account_fn or (lambda: None)
        self._api_factory = api_factory or (
            lambda: ModholdAPI(modhold_key.load_modhold_key()))
        self._closing = False
        self._page = 1
        self._total = 0
        self._per_page = 50
        self._gen = 0                      # drops results of a superseded search
        self._cards: dict[int, _ModCard] = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="modhold-cover")
        self._pending_detail: dict | None = None

        self._page_sig.connect(self._guard(self._on_page))
        self._detail_sig.connect(self._guard(self._on_detail))
        self._cover_sig.connect(self._guard(self._on_cover))
        self._progress_sig.connect(self._guard(self._progress_fn))
        self._installed_sig.connect(self._guard(self._on_installed))
        self.destroyed.connect(self._on_destroyed)

        self._build()
        self._refresh_account()
        self._load(1)

    # ------------------------------------------------------------------- UI
    def _guard(self, fn):
        return lambda *a: None if self._closing else fn(*a)

    def _build(self):
        p = active_palette()
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search {0} on Modhold…").format(
            getattr(self._game, "name", "")))
        self._search.returnPressed.connect(lambda: self._load(1))
        top.addWidget(self._search, 1)
        self._sort = QComboBox()
        for key, label in SORTS:
            self._sort.addItem(self.tr(label), key)
        self._sort.currentIndexChanged.connect(lambda *_: self._load(1))
        top.addWidget(self._sort)
        self._adult = QCheckBox(self.tr("Show adult mods"))
        self._adult.toggled.connect(lambda *_: self._render())
        top.addWidget(self._adult)
        go = QPushButton(self.tr("Search"))
        go.clicked.connect(lambda: self._load(1))
        top.addWidget(go)
        root.addLayout(top)

        acct = QHBoxLayout()
        self._account = QLabel("")
        self._account.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')};")
        acct.addWidget(self._account, 1)
        self._account_btn = QPushButton(self.tr("Account…"))
        self._account_btn.clicked.connect(self._open_account_clicked)
        acct.addWidget(self._account_btn)
        root.addLayout(acct)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._list_host = QWidget()
        self._list = QVBoxLayout(self._list_host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(6)
        self._list.addStretch(1)
        self._scroll.setWidget(self._list_host)
        root.addWidget(self._scroll, 1)

        nav = QHBoxLayout()
        self._prev = QPushButton(self.tr("◀ Previous"))
        self._prev.clicked.connect(lambda: self._load(self._page - 1))
        self._next = QPushButton(self.tr("Next ▶"))
        self._next.clicked.connect(lambda: self._load(self._page + 1))
        self._page_lbl = QLabel("")
        self._page_lbl.setAlignment(Qt.AlignCenter)
        nav.addWidget(self._prev)
        nav.addWidget(self._page_lbl, 1)
        nav.addWidget(self._next)
        root.addLayout(nav)

    def _set_status(self, text: str, error: bool = False):
        self._status.setStyleSheet(f"color:{err_text()};" if error else "")
        self._status.setText(text)

    def _refresh_account(self):
        user = modhold_key.load_modhold_user()
        if modhold_key.load_modhold_key():
            self._account.setText(self.tr("Connected as {0}").format(user) if user
                                  else self.tr("Connected to Modhold"))
        else:
            self._account.setText(self.tr(
                "Not connected — browsing is open, but downloads need your Modhold account."))

    def _open_account_clicked(self):
        self._open_account()

    # ------------------------------------------------------------- loading
    def _load(self, page: int):
        page = max(1, page)
        self._page = page
        self._gen += 1
        gen = self._gen
        q = self._search.text().strip()
        sort = self._sort.currentData() or "updated"
        self._set_status(self.tr("Loading…"))
        self._prev.setEnabled(False)
        self._next.setEnabled(False)

        def worker():
            try:
                data = self._api_factory().list_mods(self._slug, sort=sort, q=q, page=page)
                safe_emit(self._page_sig, {"gen": gen, "data": data}, "")
            except ModholdError as e:
                safe_emit(self._page_sig, {"gen": gen, "data": None}, str(e))
            except Exception as e:      # never let a worker die silently
                safe_emit(self._page_sig, {"gen": gen, "data": None}, str(e))

        threading.Thread(target=worker, daemon=True, name="modhold-list").start()

    def _on_page(self, result, error: str):
        if result and result.get("gen") != self._gen:
            return                                    # an older search finished late
        data = (result or {}).get("data")
        if data is None:
            self._set_status(self.tr("Could not load Modhold: {0}").format(error), error=True)
            self._prev.setEnabled(self._page > 1)
            return
        self._mods = list(data.get("mods") or [])
        self._total = int(data.get("total") or 0)
        self._per_page = int(data.get("per_page") or 50)
        self._render()

    def _render(self):
        mods = getattr(self, "_mods", [])
        show_adult = self._adult.isChecked()
        while self._list.count() > 1:
            item = self._list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards = {}
        shown = [m for m in mods if show_adult or not m.get("adult")]
        hidden = len(mods) - len(shown)
        for m in shown:
            card = _ModCard(m, self._install_clicked, self._open_page)
            self._list.insertWidget(self._list.count() - 1, card)
            self._cards[int(m.get("id") or 0)] = card
            if m.get("cover"):
                self._fetch_cover(int(m.get("id") or 0), str(m["cover"]))
        pages = page_count(self._total, self._per_page)
        self._page_lbl.setText(self.tr("Page {0} of {1} · {2} mods").format(
            self._page, pages, self._total))
        self._prev.setEnabled(self._page > 1)
        self._next.setEnabled(self._page < pages)
        if not shown:
            self._set_status(self.tr("No mods found."))
        elif hidden:
            self._set_status(self.tr("{0} adult mod(s) hidden on this page.").format(hidden))
        else:
            self._set_status("")

    def _fetch_cover(self, mod_id: int, url: str):
        def work():
            try:
                r = requests.get(url, timeout=15)
                if r.ok and r.content:
                    safe_emit(self._cover_sig, mod_id, r.content)
            except requests.RequestException:
                pass
        if not self._closing:
            self._pool.submit(work)

    def _on_cover(self, mod_id: int, data: bytes):
        card = self._cards.get(mod_id)
        if card is None:
            return
        pm = QPixmap()
        if pm.loadFromData(data):
            card.set_cover(pm)

    def _open_page(self, mod: dict):
        url = mod.get("page") or ""
        if url:
            from Utils.xdg import open_url
            open_url(url, log_fn=self._log)

    # -------------------------------------------------------------- install
    def _install_clicked(self, mod: dict):
        if not modhold_key.load_modhold_key():
            self._set_status(self.tr("Connect your Modhold account first, then press "
                                     "Install again."), error=True)
            self._open_account()
            return
        mod_id = int(mod.get("id") or 0)
        self._set_status(self.tr("Fetching files for {0}…").format(mod.get("name", "")))

        def worker():
            try:
                detail = self._api_factory().get_mod(mod_id)
                safe_emit(self._detail_sig, detail, "")
            except Exception as e:
                safe_emit(self._detail_sig, None, str(e))

        threading.Thread(target=worker, daemon=True, name="modhold-detail").start()

    def _on_detail(self, detail, error: str):
        if detail is None:
            self._set_status(self.tr("Could not load the mod: {0}").format(error), error=True)
            return
        self._set_status("")
        name = detail.get("name", "")
        files = [f for f in (detail.get("files") or []) if f.get("id")]
        if not files:
            self._set_status(self.tr("{0} has no downloadable files.").format(name), error=True)
            return
        if detail.get("can_download") is False:
            why = (self.tr("It is a Pass, paid or beta release, and this account cannot "
                           "download it.") if is_gated(detail)
                   else self.tr("This account cannot download it."))
            self._set_status(self.tr("{0}: {1}").format(name, why), error=True)
            return
        chosen = default_file_selection(files)
        if needs_chooser(files):
            dlg = ModholdFileChooser(name, files, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            chosen = dlg.selected_files()
        for f in chosen:
            self._start_download(detail, f)

    def _start_download(self, detail: dict, file: dict):
        file_id = int(file.get("id") or 0)
        key = f"modhold:{file_id}"
        label = str(file.get("name") or detail.get("name") or key)
        dest = get_download_cache_dir_for_game(getattr(self._game, "name", ""))
        meta = meta_from_detail(detail, file, self._slug)
        self._log(f"Modhold: downloading {label}…")
        safe_emit(self._progress_sig, key, label, 0, 0)

        def worker():
            try:
                api = self._api_factory()
                dl = ModholdDownloader(api, dest)
                res = dl.download_file(
                    file_id, known_name=str(file.get("name") or ""),
                    known_size=int(file.get("size") or 0),
                    known_sha256=str(file.get("sha256") or ""),
                    progress_cb=lambda d, t: safe_emit(self._progress_sig, key, label, d, t))
            except Exception as e:
                safe_emit(self._progress_sig, key, "", 0, -1)
                safe_emit(self._installed_sig, "", None, str(e))
                return
            safe_emit(self._progress_sig, key, "", 0, -1)
            if res.success and res.file_path:
                safe_emit(self._installed_sig, str(res.file_path), meta, "")
            else:
                safe_emit(self._installed_sig, "", None, f"{label}: {failure_reason(res)}")

        threading.Thread(target=worker, daemon=True, name="modhold-download").start()

    def _on_installed(self, archive: str, meta, error: str):
        if error:
            self._set_status(error, error=True)
            self._log(f"Modhold: {error}")
            return
        self._log(f"Modhold: downloaded → {archive}; installing…")
        prebuilt = prebuilt_nexus_meta(meta)
        self._install_fn([archive], {archive: prebuilt})

    # ------------------------------------------------------------- teardown
    def _on_destroyed(self, *_):
        self._closing = True
        self._pool.shutdown(wait=False, cancel_futures=True)

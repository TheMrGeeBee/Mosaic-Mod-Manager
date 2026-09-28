"""In-window overlay for the Nexus browser's "Download selected": one review
screen listing every selected mod with its files, so a batch is decided up
front instead of one file-chooser popup per mod.

A mod with exactly one MAIN file (or only one installable file at all) has it
ticked already. A mod with several MAIN files gets nothing ticked: those are
usually variants of the same mod (SE/AE, 1K/2K, male/female) and picking the
newest one would silently install the wrong variant. Optional/misc files can
be ticked on top. A mod left with nothing ticked is skipped.

Like NexusFileChooser this is a borderless child widget with a dimmed backdrop
(not a QDialog — on Steam Deck gaming mode a top-level window can open behind
the app). Unlike it, a backdrop click does NOT cancel: a batch can take a while
to review and losing every choice to a stray click would be worse.

Usage:
    NexusBatchChooser.show_over(host, mods, on_done)
*mods* is a list of ``(entry, files)`` — *files* is the mod's NexusModFile
list, or None when it couldn't be fetched. ``on_done(plan_or_None)`` receives
``[(entry, [file, ...]), ...]`` (files in install order, main first) for every
mod with at least one ticked file, or None on cancel.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QHeaderView,
)

from gui_qt.theme.theme_qt import (
    active_palette, _c, _tinted_icon_url, contrast_text,
)
from gui_qt.nexus.nexus_file_chooser import (
    installable_files, _fmt_size_bytes, _plain_text, _CATEGORY_LABEL,
)


def plan_mod_files(files) -> tuple[list, set]:
    """Return ``(offered, preticked_file_ids)`` for one mod in a batch.

    *offered* is the same list the single-mod chooser shows (main first, then
    optional, then misc, newest first). A file is pre-ticked only when the
    choice is unambiguous: exactly one MAIN file, or a single offered file."""
    offered = installable_files(list(files or []))
    mains = [f for f in offered if (f.category_name or "").upper() == "MAIN"]
    if len(mains) == 1:
        return offered, {mains[0].file_id}
    if len(offered) == 1:
        return offered, {offered[0].file_id}
    return offered, set()


class NexusBatchChooser(QWidget):
    CARD_W = 780
    CARD_H = 660

    def __init__(self, host: QWidget, mods: list, on_done):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._finished = False
        # (entry, top item, [child items]) per mod, in selection order.
        self._rows: list[tuple] = []
        p = active_palette()
        self._pal = p

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("_BatchChooserCard")
        self._card.setStyleSheet(
            f"#_BatchChooserCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)

        hdr = QLabel(self.tr("Download {0} selected mod(s)").format(len(mods)))
        hdr.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(hdr)
        sub = QLabel(self.tr(
            "Mods with a single main file have it ticked already. Mods with "
            "several main files (usually different versions or variants) need "
            "you to tick the one you want. Tick any optional files you also "
            "want. A mod with nothing ticked is skipped."))
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        v.addWidget(sub)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(
            [self.tr("File"), self.tr("Version"), self.tr("Size")])
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(False)
        hh = self._tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        # No `color` on ::item — it would override the per-item foreground the
        # mod rows use for their status.
        self._tree.setStyleSheet(
            f"QTreeWidget {{ font-size:13px; background:{_c(p,'BG_LIST')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};"
            f" border-radius:6px; }}"
            f"QTreeWidget::item {{ padding:4px 2px; }}"
            # Same box as the app's QCheckBox indicators (the global QSS only
            # styles QCheckBox, so item-view checkboxes need it spelled out).
            f"QTreeWidget::indicator {{ width:16px; height:16px;"
            f" border:1px solid {_c(p,'BORDER_FAINT')}; border-radius:3px;"
            f" background:{_c(p,'BG_DEEP')}; }}"
            f"QTreeWidget::indicator:hover {{ border:1px solid {_c(p,'CHECK_FILL')}; }}"
            f"QTreeWidget::indicator:checked {{ background:{_c(p,'CHECK_FILL')};"
            f" border:1px solid {_c(p,'CHECK_FILL')}; image:url("
            f"{_tinted_icon_url('check_white.png', contrast_text(_c(p,'CHECK_FILL')))}); }}"
            f"QTreeWidget::item:selected {{ background:{_c(p,'BG_SELECT')};"
            f" color:{_c(p,'TEXT_ON_ACCENT')}; }}")
        bold = QFont(self._tree.font())
        bold.setBold(True)

        for entry, files in mods:
            name = entry.name or f"Mod {entry.mod_id}"
            top = QTreeWidgetItem(self._tree)
            top.setFlags(Qt.ItemIsEnabled)
            top.setFirstColumnSpanned(True)
            top.setFont(0, bold)
            top.setData(0, Qt.UserRole, name)
            children = []
            if files is None:
                top.setData(0, Qt.UserRole + 1,
                            self.tr("couldn't load the file list, skipped"))
            else:
                offered, ticked = plan_mod_files(files)
                if not offered:
                    top.setData(0, Qt.UserRole + 1,
                                self.tr("no downloadable files, skipped"))
                for f in offered:
                    children.append(self._add_file_row(top, f, f.file_id in ticked))
                # Open the mods that need a decision; the rest stay folded
                # (their optional files are one click away).
                top.setExpanded(bool(offered) and not ticked)
            self._rows.append((entry, top, children))
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.currentItemChanged.connect(self._on_row_changed)
        v.addWidget(self._tree, 1)

        self._desc = QTextEdit()
        self._desc.setReadOnly(True)
        self._desc.setFixedHeight(90)
        self._desc.setStyleSheet(
            f"QTextEdit {{ font-size:13px; background:{_c(p,'BG_LIST')};"
            f" color:{_c(p,'TEXT_DIM')}; border:1px solid {_c(p,'BORDER')};"
            f" border-radius:6px; padding:6px; }}")
        self._desc.setPlainText(self.tr("Select a file to see its description."))
        v.addWidget(self._desc)

        bar = QHBoxLayout()
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        bar.addWidget(self._summary, 1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        self._go = QPushButton()
        self._go.setObjectName("PrimaryButton")
        self._go.setCursor(Qt.PointingHandCursor)
        self._go.clicked.connect(lambda: self._finish(self.plan()))
        bar.addWidget(self._go)
        v.addLayout(bar)

        self._refresh()

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()
        self.setFocus()

    @classmethod
    def show_over(cls, host, mods, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, mods, on_done)

    # -- rows ---------------------------------------------------------------
    def _add_file_row(self, top, f, checked: bool) -> QTreeWidgetItem:
        cat = (f.category_name or "").upper()
        label = self.tr(_CATEGORY_LABEL[cat]) if cat in _CATEGORY_LABEL else cat.title()
        name = f.name or f.file_name or f"File {f.file_id}"
        size = (f.size_in_bytes or 0) or (f.size_kb * 1024 if f.size_kb else 0)
        item = QTreeWidgetItem(top, [
            f"[{label}]  {name}" if label else name,
            f"v{f.version}" if f.version else "",
            _fmt_size_bytes(size),
        ])
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
        item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        item.setData(0, Qt.UserRole, f)
        return item

    def plan(self) -> list:
        """``[(entry, [ticked files in offered order]), ...]``, skipping mods
        with nothing ticked."""
        out = []
        for entry, _top, children in self._rows:
            picked = [c.data(0, Qt.UserRole) for c in children
                      if c.checkState(0) == Qt.Checked]
            if picked:
                out.append((entry, picked))
        return out

    def _refresh(self):
        """Update every mod row's status text and the button/summary."""
        p = self._pal
        dim = QBrush(QColor(_c(p, "TEXT_DIM")))
        warn = QBrush(QColor(_c(p, "BTN_WARN")))
        main = QBrush(QColor(_c(p, "TEXT_MAIN")))
        skipped = []
        n_files = 0
        self._tree.blockSignals(True)
        for entry, top, children in self._rows:
            name = top.data(0, Qt.UserRole)
            fixed = top.data(0, Qt.UserRole + 1)
            ticked = sum(1 for c in children if c.checkState(0) == Qt.Checked)
            n_files += ticked
            if fixed:
                status, brush = fixed, dim
            elif ticked == 0:
                status, brush = self.tr("nothing ticked, will be skipped"), warn
            elif ticked == 1:
                only = next(c for c in children if c.checkState(0) == Qt.Checked)
                f = only.data(0, Qt.UserRole)
                status = f.name or f.file_name or self.tr("1 file")
                brush = main
            else:
                status, brush = self.tr("{0} files").format(ticked), main
            if ticked == 0:
                skipped.append(name)
            top.setText(0, f"{name}   —   {status}")
            top.setForeground(0, brush)
        self._tree.blockSignals(False)

        mods_in = len(self._rows) - len(skipped)
        self._go.setText(self.tr("Download {0} file(s)").format(n_files))
        self._go.setEnabled(n_files > 0)
        if skipped:
            self._summary.setText(
                self.tr("{0} mod(s), {1} skipped: {2}").format(
                    mods_in, len(skipped), ", ".join(skipped)))
        else:
            self._summary.setText(self.tr("{0} mod(s)").format(mods_in))

    def _on_item_changed(self, item, column):
        if column == 0 and item.parent() is not None:
            self._refresh()

    def _on_row_changed(self, cur, _prev):
        f = cur.data(0, Qt.UserRole) if (cur is not None and cur.parent() is not None) else None
        if f is None:
            self._desc.setPlainText(self.tr("Select a file to see its description."))
            return
        text = _plain_text(getattr(f, "description", ""))
        self._desc.setPlainText(text or self.tr("No description provided."))

    # -- overlay plumbing ---------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(360, w), max(300, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _finish(self, result):
        if self._finished:
            return
        self._finished = True
        self._host.removeEventFilter(self)
        cb = self._on_done
        self.hide()
        self.deleteLater()
        if cb is not None:
            cb(result)

    def mousePressEvent(self, event):
        # Swallow backdrop clicks (don't pass them to the app underneath), but
        # don't cancel — see the module docstring.
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._finish(None)
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

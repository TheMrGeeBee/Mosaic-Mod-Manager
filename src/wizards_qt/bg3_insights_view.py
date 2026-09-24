"""Qt view: BG3 load-order insights.

Lists every place two or more enabled mods define the same thing (stats
entries, treasure tables, UI screens, shared files, declared conflicts,
variants of one mod) and which one currently wins.  The user can make a mod
win (moves it in the modlist + saves a rule) or ignore a finding.  All
scanning/analysis lives in ``Utils.mods.bg3_pak_index``; this view only
renders findings and calls ``apply_winner`` / ``ignore_finding``.

Modeled on ``wizards_qt.bg3_sort_view.BG3SortView``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QSplitter,
    QTreeWidget, QTreeWidgetItem, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_MAX_KEYS_SHOWN = 200


class BG3InsightsView(WizardViewBase):
    """Show what enabled BG3 mods override in common, and who wins."""

    _ready_sig = Signal(object)          # Insights
    _error_sig = Signal(str)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Load Order Insights — {0}").format(game.name))
        self._insights = None
        self._profile_dir = None

        self._ready_sig.connect(self._guard(self._on_ready))
        self._error_sig.connect(self._guard(
            lambda t: self._set_status(self._summary, t, RED)))

        self._stack.addWidget(self._build_page())
        self._stack.setCurrentIndex(0)
        self._rescan()

    # ---- layout -----------------------------------------------------------------
    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("What your mods override in common"))
        self._make_note(lay, self.tr(
            "Mods that define the same stats entries, treasure tables, UI "
            "screens or files. The one that loads later wins. Nothing changes "
            "until you pick a winner or ignore a finding."))
        self._summary = self._make_status(lay)

        p = active_palette()
        box_qss = (f"background:{_c(p,'BG_PANEL')}; color:{_c(p,'TEXT_MAIN')};"
                   f" border:1px solid {_c(p,'BORDER')};")

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels([self.tr("Mods (top = wins in your list)"),
                                    self.tr("Items"),
                                    self.tr("Currently wins"),
                                    self.tr("Status")])
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setStyleSheet(f"QTreeWidget{{{box_qss}}}")
        self._tree.currentItemChanged.connect(self._on_select)

        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._detail.setStyleSheet(f"QPlainTextEdit{{{box_qss}}}")

        split = QSplitter(Qt.Vertical)
        split.addWidget(self._tree)
        split.addWidget(self._detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        lay.addWidget(split, 1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        self._show_harmless = QCheckBox(self.tr("Show harmless, intended and ignored"))
        self._show_harmless.toggled.connect(lambda _c=False: self._populate())
        rh.addWidget(self._show_harmless)
        rh.addStretch(1)
        rh.addWidget(QLabel(self.tr("Winner:")))
        self._winner_box = QComboBox()
        self._winner_box.setMinimumWidth(260)
        rh.addWidget(self._winner_box)
        self._win_btn = self._green_btn(self.tr("Make it win"))
        self._win_btn.clicked.connect(lambda _c=False: self._make_win())
        rh.addWidget(self._win_btn)
        self._patch_btn = self._accent_btn(self.tr("Accept as patch"))
        self._patch_btn.setToolTip(self.tr(
            "Make this patch win every overlap it has with the mods it patches"))
        self._patch_btn.clicked.connect(lambda _c=False: self._accept_patch())
        rh.addWidget(self._patch_btn)
        self._ignore_btn = self._orange_btn(self.tr("Ignore"))
        self._ignore_btn.clicked.connect(lambda _c=False: self._ignore())
        rh.addWidget(self._ignore_btn)
        self._rescan_btn = self._accent_btn(self.tr("Rescan"))
        self._rescan_btn.clicked.connect(lambda _c=False: self._rescan())
        rh.addWidget(self._rescan_btn)
        done = self._green_btn()
        done.clicked.connect(lambda _c=False: self._finish())
        rh.addWidget(done)
        lay.addWidget(row)
        self._set_actions_enabled(False)
        return page

    def _set_actions_enabled(self, on: bool):
        for w in (self._winner_box, self._win_btn, self._ignore_btn,
                  self._patch_btn):
            w.setEnabled(on)

    # ---- scanning ---------------------------------------------------------------
    def _rescan(self):
        self._set_status(self._summary, self.tr("Reading mod .pak files…"))
        self._rescan_btn.setEnabled(False)
        threading.Thread(target=self._worker, daemon=True,
                         name="bg3-insights").start()

    def _worker(self):
        from Utils.mods.bg3_import import resolve_profile_modlist
        from Utils.mods.bg3_pak_index import compute_insights
        try:
            profile = getattr(self._ctx, "profile_name", "") if self._ctx else ""
            modlist = resolve_profile_modlist(self._game, profile)
            if modlist is None:
                raise RuntimeError("Could not determine the active profile.")
            self._profile_dir = modlist.parent
            insights = compute_insights(self._game, self._profile_dir,
                                        log_fn=self._log)
            safe_emit(self._ready_sig, insights)
        except Exception as exc:
            self._log(f"BG3 Insights: error: {exc}")
            safe_emit(self._error_sig, self.tr("Error: {0}").format(exc))

    def _on_ready(self, insights):
        from Utils.mods.bg3_pak_index import unresolved_count
        self._insights = insights
        self._rescan_btn.setEnabled(True)
        n = unresolved_count(insights)
        total = len(insights.findings)
        if n:
            self._set_status(self._summary, self.tr(
                "{0} finding(s) need a decision, {1} in total.").format(n, total))
        else:
            self._set_status(self._summary, self.tr(
                "Nothing needs a decision ({0} finding(s) in total).").format(total),
                GREEN)
        self._populate()

    # ---- rendering ---------------------------------------------------------------
    def _status_text(self, f, ignored: bool) -> str:
        if ignored:
            return self.tr("Ignored")
        if f.kind == "identical":
            return self.tr("Harmless")
        if f.rule_violated:
            return self.tr("Your rule is broken")
        if f.intended:
            return self.tr("Intended (patch for the other)")
        if f.resolved_by_rule:
            return self.tr("Decided")
        if f.suggested_patch:
            return self.tr("Looks like a patch — accept?")
        return self.tr("Needs a decision")

    def _populate(self):
        from Utils.mods.bg3_pak_index import KIND_LABELS
        self._tree.clear()
        self._detail.clear()
        self._set_actions_enabled(False)
        if self._insights is None:
            return
        show_all = self._show_harmless.isChecked()
        rows = [(f, False) for f in self._insights.findings]
        if show_all:
            rows += [(f, True) for f in self._insights.ignored]
        groups: dict[str, QTreeWidgetItem] = {}
        for f, ignored in rows:
            if not show_all and (f.kind == "identical" or f.intended):
                continue
            parent = groups.get(f.kind)
            if parent is None:
                parent = QTreeWidgetItem([KIND_LABELS.get(f.kind, f.kind), "", "", ""])
                parent.setFirstColumnSpanned(False)
                parent.setFlags(parent.flags() & ~Qt.ItemIsSelectable)
                self._tree.addTopLevelItem(parent)
                groups[f.kind] = parent
            item = QTreeWidgetItem([
                "  ⟶  ".join(f.mods),
                str(len(f.keys)),
                f.winner or ("—" if f.kind != "gui_state" else self.tr("check in-game")),
                self._status_text(f, ignored),
            ])
            item.setData(0, Qt.UserRole, (f, ignored))
            parent.addChild(item)
        for parent in groups.values():
            parent.setText(1, str(parent.childCount()))
            parent.setExpanded(True)
        self._tree.resizeColumnToContents(1)
        self._tree.setColumnWidth(0, max(520, self._tree.columnWidth(0)))
        self._tree.setColumnWidth(2, 260)

    def _selected(self):
        item = self._tree.currentItem()
        data = item.data(0, Qt.UserRole) if item is not None else None
        return data if data else (None, False)

    def _on_select(self, *_a):
        f, ignored = self._selected()
        self._winner_box.clear()
        if f is None:
            self._detail.clear()
            self._set_actions_enabled(False)
            return
        lines = [f"{m}" + (f"   (load position {self._insights.load_rank[m] + 1})"
                           if m in self._insights.load_rank else
                           "   (not in the load order)")
                 for m in f.mods]
        text = "\n".join(lines)
        if f.note:
            text += f"\n\n{f.note}"
        keys = f.keys[:_MAX_KEYS_SHOWN]
        text += "\n\n" + "\n".join(keys)
        if len(f.keys) > _MAX_KEYS_SHOWN:
            text += f"\n… (+{len(f.keys) - _MAX_KEYS_SHOWN} more)"
        self._detail.setPlainText(text)
        self._winner_box.addItems(f.mods)
        pending_patch = (f.suggested_patch and not ignored
                         and (not f.resolved_by_rule or f.rule_violated))
        if f.suggested_patch:
            self._winner_box.setCurrentText(f.suggested_patch)
            text = self.tr("Suggested patch: {0} (its name says it is a "
                           "patch, but it doesn't declare the mods it patches)"
                           ).format(f.suggested_patch) + "\n\n" + text
            self._detail.setPlainText(text)
        self._patch_btn.setEnabled(bool(pending_patch))
        can_order = f.kind not in ("identical", "declared_conflict", "variant_group")
        self._winner_box.setEnabled(can_order)
        self._win_btn.setEnabled(can_order)
        self._ignore_btn.setEnabled(not ignored)

    # ---- actions -----------------------------------------------------------------
    def _make_win(self):
        from Utils.mods.bg3_pak_index import RuleConflict, apply_winner
        f, _ignored = self._selected()
        winner = self._winner_box.currentText()
        if f is None or not winner or self._profile_dir is None:
            return
        try:
            apply_winner(self._profile_dir, f, winner, self._insights.depends_on)
        except RuleConflict as exc:
            self._set_status(self._summary, str(exc), RED)
            return
        except Exception as exc:
            self._log(f"BG3 Insights: could not apply: {exc}")
            self._set_status(self._summary, self.tr("Error: {0}").format(exc), RED)
            return
        self._log(f"BG3 Insights: {winner} now wins over "
                  f"{', '.join(m for m in f.mods if m != winner)}")
        self._ran = True        # modlist changed → refresh on close
        self._rescan()

    def _accept_patch(self):
        from Utils.mods.bg3_pak_index import RuleConflict, accept_patch
        f, _ignored = self._selected()
        if f is None or not f.suggested_patch or self._profile_dir is None:
            return
        try:
            n = accept_patch(self._profile_dir, f.suggested_patch,
                             self._insights.findings, self._insights.depends_on)
        except RuleConflict as exc:
            self._set_status(self._summary, str(exc), RED)
            return
        except Exception as exc:
            self._log(f"BG3 Insights: could not accept patch: {exc}")
            self._set_status(self._summary, self.tr("Error: {0}").format(exc), RED)
            return
        self._log(f"BG3 Insights: accepted {f.suggested_patch} as a patch "
                  f"({n} finding(s))")
        self._ran = True
        self._rescan()

    def _ignore(self):
        from Utils.mods.bg3_pak_index import ignore_finding
        f, ignored = self._selected()
        if f is None or ignored or self._profile_dir is None:
            return
        ignore_finding(self._profile_dir, f)
        self._rescan()

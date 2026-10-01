"""Qt view: TW3 load-order insights.

Lists every place two or more enabled mods ship the same staged file (the
path TW3's mods.settings Priority decides a winner for -- see
Utils.mods.tw3_mods_settings) and which one currently wins. The user can
make a mod win (moves it in the modlist + saves a rule) or ignore a
finding. All scanning/analysis lives in Utils.mods.tw3_load_index; this
view only renders findings and calls apply_winner / ignore_finding.

Simpler than BG3's equivalent (wizards_qt.bg3_insights_view, which this is
modeled on): TW3 has exactly one generic finding shape (same_file, plus
its identical-content special case), no dependency graph, no author notes,
no patch-acceptance workflow.
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

_SCRIPT_MERGER_NOTE = (
    "This conflict is in content/scripts/ -- if the mods below each add "
    "DIFFERENT functionality the game actually uses, picking a winner "
    "silently drops the others' changes. Running Script Merger (Wizard > "
    "Run Script Merger) can combine all of them into one merged file "
    "instead of choosing just one.")


class TW3InsightsView(WizardViewBase):
    """Show what enabled TW3 mods ship in common, and who wins."""

    _ready_sig = Signal(object)          # Insights
    _error_sig = Signal(str)
    _progress_sig = Signal(str)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Load Order Insights — {0}").format(game.name))
        self._insights = None
        self._profile_dir = None

        self._ready_sig.connect(self._guard(self._on_ready))
        self._error_sig.connect(self._guard(
            lambda t: self._set_status(self._summary, t, RED)))
        self._progress_sig.connect(self._guard(
            lambda t: self._set_status(self._summary, t)))

        self._stack.addWidget(self._build_page())
        self._stack.setCurrentIndex(0)
        self._rescan()

    # ---- layout -----------------------------------------------------------------
    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("What your mods ship in common"))
        self._make_note(lay, self.tr(
            "Mods that ship the same file under content/ or bin/. The one "
            "that wins is decided by mods.settings' Priority (see Configure "
            "Game / deploy log). Nothing changes until you pick a winner or "
            "ignore a finding."))
        self._summary = self._make_status(lay)

        p = active_palette()
        box_qss = (f"background:{_c(p,'BG_PANEL')}; color:{_c(p,'TEXT_MAIN')};"
                   f" border:1px solid {_c(p,'BORDER')};")

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels([self.tr("Mods (top = wins in your list)"),
                                    self.tr("Files"),
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
        self._keep_btn = self._accent_btn(self.tr("Keep current order"))
        self._keep_btn.setToolTip(self.tr(
            "It already looks right in-game (or you've already merged it "
            "with Script Merger): save the current winner as your decision. "
            "Nothing moves."))
        self._keep_btn.clicked.connect(lambda _c=False: self._keep_current())
        rh.addWidget(self._keep_btn)
        self._never_together_btn = self._orange_btn(self.tr("Never Together"))
        self._never_together_btn.setToolTip(self.tr(
            "Mark these as deliberately mutually-exclusive alternatives (e.g. "
            "the same mod from Nexus and mod.io) — keeps showing this finding "
            "on future rescans, with its own status, instead of dismissing it"))
        self._never_together_btn.clicked.connect(lambda _c=False: self._never_together())
        rh.addWidget(self._never_together_btn)
        self._ignore_btn = self._orange_btn(self.tr("Ignore"))
        self._ignore_btn.clicked.connect(lambda _c=False: self._ignore())
        rh.addWidget(self._ignore_btn)
        self._rescan_btn = self._accent_btn(self.tr("Rescan"))
        self._rescan_btn.clicked.connect(lambda _c=False: self._rescan())
        rh.addWidget(self._rescan_btn)
        self._clear_decisions_btn = self._orange_btn(self.tr("Clear Decisions"))
        self._clear_decisions_btn.setToolTip(self.tr(
            "Discard every saved winner, ignored finding, and Never Together "
            "marking for this profile (e.g. to clear a stale \"Your rule is "
            "broken\")"))
        self._clear_decisions_btn.clicked.connect(
            lambda _c=False: self._clear_decisions())
        rh.addWidget(self._clear_decisions_btn)
        done = self._green_btn()
        done.clicked.connect(lambda _c=False: self._finish())
        rh.addWidget(done)
        lay.addWidget(row)
        self._set_actions_enabled(False)
        return page

    def _set_actions_enabled(self, on: bool):
        for w in (self._winner_box, self._win_btn, self._ignore_btn,
                  self._keep_btn, self._never_together_btn):
            w.setEnabled(on)

    # ---- scanning ---------------------------------------------------------------
    def _rescan(self):
        self._set_status(self._summary, self.tr("Reading staged mod files…"))
        self._rescan_btn.setEnabled(False)
        self._clear_decisions_btn.setEnabled(False)
        threading.Thread(target=self._worker, daemon=True,
                         name="tw3-insights").start()

    def _worker(self):
        from Utils.mods.bg3_import import resolve_profile_modlist
        from Utils.mods.tw3_load_index import compute_insights
        try:
            profile = getattr(self._ctx, "profile_name", "") if self._ctx else ""
            modlist = resolve_profile_modlist(self._game, profile)
            if modlist is None:
                raise RuntimeError("Could not determine the active profile.")
            self._profile_dir = modlist.parent
            safe_emit(self._progress_sig, self.tr("Reading staged mod files…"))
            insights = compute_insights(self._game, self._profile_dir,
                                        log_fn=self._log)
            safe_emit(self._ready_sig, insights)
        except Exception as exc:
            self._log(f"TW3 Insights: error: {exc}")
            safe_emit(self._error_sig, self.tr("Error: {0}").format(exc))

    def _on_ready(self, insights):
        from Utils.mods.tw3_load_index import unresolved_count
        self._insights = insights
        self._rescan_btn.setEnabled(True)
        self._clear_decisions_btn.setEnabled(True)
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
        if f.never_together:
            return self.tr("Marked — never together")
        if f.rule_violated:
            return self.tr("Your rule is broken")
        if f.intended:
            return self.tr("Intended (collection's order)")
        if f.resolved_by_rule:
            return self.tr("Decided")
        if self._insights and not all(m in self._insights.load_rank for m in f.mods):
            return self.tr("Can't be decided — not in mods.settings")
        return self.tr("Needs a decision")

    def _populate(self):
        from Utils.mods.tw3_load_index import KIND_LABELS
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
                f.winner or "—",
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
        lines = [f"{m}" + (f"   (load rank {self._insights.load_rank[m]})"
                           if m in self._insights.load_rank else
                           "   (installed, but not in mods.settings)")
                 for m in f.mods]
        text = "\n".join(lines)
        if f.note:
            text += f"\n\n{f.note}"
        keys = f.keys[:_MAX_KEYS_SHOWN]
        text += "\n\n" + "\n".join(keys)
        if len(f.keys) > _MAX_KEYS_SHOWN:
            text += f"\n… (+{len(f.keys) - _MAX_KEYS_SHOWN} more)"
        if any("/scripts/" in k for k in f.keys):
            text += "\n\n" + self.tr(_SCRIPT_MERGER_NOTE)
        self._detail.setPlainText(text)
        self._winner_box.addItems(f.mods)
        all_ranked = all(m in self._insights.load_rank for m in f.mods)
        can_order = all_ranked and f.kind != "identical"
        self._winner_box.setEnabled(can_order)
        self._win_btn.setEnabled(can_order)
        self._keep_btn.setEnabled(
            can_order and bool(f.winner) and not ignored
            and (not f.resolved_by_rule or f.rule_violated))
        self._ignore_btn.setEnabled(not ignored)
        self._never_together_btn.setEnabled(not ignored and len(f.mods) >= 2)
        self._never_together_btn.setText(
            self.tr("Un-mark Never Together") if f.never_together
            else self.tr("Never Together"))

    # ---- actions -----------------------------------------------------------------
    def _make_win(self):
        from Utils.mods.tw3_load_index import RuleConflict, apply_winner
        f, _ignored = self._selected()
        winner = self._winner_box.currentText()
        if f is None or not winner or self._profile_dir is None:
            return
        try:
            apply_winner(self._profile_dir, f, winner, self._insights.collection_mods)
        except RuleConflict as exc:
            self._set_status(self._summary, str(exc), RED)
            return
        except Exception as exc:
            self._log(f"TW3 Insights: could not apply: {exc}")
            self._set_status(self._summary, self.tr("Error: {0}").format(exc), RED)
            return
        self._log(f"TW3 Insights: {winner} now wins over "
                  f"{', '.join(m for m in f.mods if m != winner)}")
        self._ran = True        # modlist changed → refresh on close
        self._rescan()

    def _keep_current(self):
        from Utils.mods.tw3_load_index import RuleConflict, keep_current_order
        f, _ignored = self._selected()
        if f is None or self._profile_dir is None:
            return
        try:
            winner = keep_current_order(self._profile_dir, f)
        except RuleConflict as exc:
            self._set_status(self._summary, str(exc), RED)
            return
        self._log(f"TW3 Insights: kept current order — {winner} wins over "
                  f"{', '.join(m for m in f.mods if m != winner)}")
        self._rescan()

    def _ignore(self):
        from Utils.mods.tw3_load_index import ignore_finding
        f, ignored = self._selected()
        if f is None or ignored or self._profile_dir is None:
            return
        ignore_finding(self._profile_dir, f)
        self._rescan()

    def _never_together(self):
        from Utils.mods.tw3_load_index import mark_never_together, unmark_never_together
        f, _ignored = self._selected()
        if f is None or self._profile_dir is None or len(f.mods) < 2:
            return
        if f.never_together:
            unmark_never_together(f, self._profile_dir)
            self._log(f"TW3 Insights: un-marked never-together — "
                      f"{', '.join(f.mods)}")
        else:
            mark_never_together(f, self._profile_dir)
            self._log(f"TW3 Insights: marked never-together — "
                      f"{', '.join(f.mods)}")
        self._rescan()

    def _clear_decisions(self):
        if self._profile_dir is None:
            return
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Clear Decisions"),
            self.tr("Discard every saved winner, ignored finding, and Never "
                    "Together marking for this profile? All of it will need "
                    "to be decided again on the next scan."),
            lambda ok: self._do_clear_decisions() if ok else None,
            confirm_label=self.tr("Clear"))

    def _do_clear_decisions(self):
        from Utils.mods.tw3_load_index import clear_decisions
        clear_decisions(self._profile_dir)
        self._log("TW3 Insights: cleared all saved decisions "
                  "(including Never Together markings)")
        self._rescan()

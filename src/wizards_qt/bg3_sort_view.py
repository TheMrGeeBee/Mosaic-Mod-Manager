"""Qt view: Baldur's Gate 3 "Sort Load Order".

Computes the layered sort on open (``Utils.mods.bg3_sort.compute_layered_plan``:
layers from ``Utils.mods.bg3_layers``, with collection order, dependencies and
the user's Load Order Insights decisions taking priority) → preview grouped by
layer → optionally move single mods to another layer (saved per profile) →
apply.  All sorting logic lives in the Utils modules; the view renders the plan
and calls ``apply_plan`` / ``bg3_layers.set_override``.

Replaces the dependency-only view; deploy still runs the dependency-only sort
(``compute_sort_plan_for_modlist``), which moves nothing after this one.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_PREVIEW, _PG_DONE = range(2)


class BG3SortView(WizardViewBase):
    """Sort the active profile's load order into layers."""

    _preview_ready_sig = Signal(object)     # SortPlan
    _preview_error_sig = Signal(str)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Sort Load Order — {0}").format(game.name))
        self._plan = None
        self._modlist_path = None
        self._load_after_map: dict[str, list[str]] = {}

        self._preview_ready_sig.connect(self._guard(self._on_preview_ready))
        self._preview_error_sig.connect(self._guard(
            lambda t: self._set_status(self._preview_summary, t, RED)))

        self._stack.addWidget(self._build_preview_page())
        self._stack.addWidget(self._build_done_page())
        self._stack.setCurrentIndex(_PG_PREVIEW)
        self._compute_preview()

    # ---- page 1: preview --------------------------------------------------------
    def _build_preview_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Review the sorted load order"))
        self._make_note(lay, self.tr(
            "Mods are arranged in layers, loading first to last: frameworks & "
            "UI, libraries, gameplay, story, items, visuals, miscellaneous, "
            "patches, late loaders. Inside a layer your current order is kept. "
            "Dependencies, your Load Order Insights decisions and a "
            "collection's own order always win over layers. Nothing changes "
            "until you click Apply."))
        self._preview_summary = self._make_status(lay)

        p = active_palette()
        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels([self.tr("Mod (in load order)"),
                                    self.tr("Position"), self.tr("Why")])
        self._tree.setUniformRowHeights(True)
        self._tree.setStyleSheet(
            f"QTreeWidget{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};}}")
        self._tree.currentItemChanged.connect(self._on_select)
        lay.addWidget(self._tree, 1)

        # Row 1: "load after" — the intuitive fix for an add-on or patch that
        # must follow one specific mod (saved like an Insights decision).
        after_row = QWidget()
        ah = QHBoxLayout(after_row); ah.setContentsMargins(0, 8, 0, 0); ah.setSpacing(8)
        ah.addWidget(QLabel(self.tr("Load after:")))
        self._after_box = QComboBox()
        self._after_box.setMinimumWidth(320)
        ah.addWidget(self._after_box)
        self._after_btn = self._accent_btn(self.tr("Load after"))
        self._after_btn.setToolTip(self.tr(
            "Always load the selected mod after this one (saved for this profile)"))
        self._after_btn.clicked.connect(lambda _c=False: self._load_after(True))
        ah.addWidget(self._after_btn)
        self._clear_after_btn = self._orange_btn(self.tr("Clear load-after"))
        self._clear_after_btn.clicked.connect(lambda _c=False: self._load_after(False))
        ah.addWidget(self._clear_after_btn)
        ah.addStretch(1)
        lay.addWidget(after_row)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addWidget(QLabel(self.tr("Layer:")))
        self._layer_box = QComboBox()
        from Utils.mods.bg3_layers import LAYERS
        for layer in LAYERS:
            self._layer_box.addItem(self.tr(layer.label), layer.id)
        rh.addWidget(self._layer_box)
        self._move_btn = self._accent_btn(self.tr("Move to layer"))
        self._move_btn.setToolTip(self.tr(
            "Always put the selected mod in this layer (saved for this profile)"))
        self._move_btn.clicked.connect(lambda _c=False: self._set_layer(True))
        rh.addWidget(self._move_btn)
        self._reset_btn = self._orange_btn(self.tr("Reset layer"))
        self._reset_btn.clicked.connect(lambda _c=False: self._set_layer(False))
        rh.addWidget(self._reset_btn)
        rh.addStretch(1)
        self._apply_btn = self._green_btn(self.tr("Apply Order"))
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(lambda _c=False: self._apply())
        rh.addWidget(self._apply_btn)
        lay.addWidget(row)
        self._set_mod_actions(None)
        return page

    def _set_mod_actions(self, mod):
        on = mod is not None and self._plan is not None and mod in self._plan.layers
        for w in (self._layer_box, self._move_btn, self._after_box, self._after_btn):
            w.setEnabled(on)
        self._reset_btn.setEnabled(
            on and self._plan.layers[mod][1] == "your choice")
        self._clear_after_btn.setEnabled(on and bool(self._load_after_map.get(mod)))

    def _compute_preview(self):
        self._apply_btn.setEnabled(False)
        self._set_status(self._preview_summary,
                         self.tr("Reading installed mods…"))
        threading.Thread(target=self._compute_preview_worker, daemon=True,
                         name="bg3-sort-preview").start()

    def _compute_preview_worker(self):
        from Utils.mods.bg3_import import resolve_profile_modlist
        from Utils.mods.bg3_sort import compute_layered_plan
        try:
            profile = getattr(self._ctx, "profile_name", "") if self._ctx else ""
            modlist = resolve_profile_modlist(self._game, profile)
            if modlist is None:
                raise RuntimeError("Could not determine the active profile.")
            self._modlist_path = modlist
            plan = compute_layered_plan(self._game, modlist, log_fn=self._log)
            from Utils.mods.bg3_pak_index import LOAD_AFTER, read_rules
            la: dict[str, list[str]] = {}
            for r in read_rules(modlist.parent)["rules"]:
                if r.get("reason") == LOAD_AFTER:
                    la.setdefault(r["winner"], []).append(r["loser"])
            self._load_after_map = la
            safe_emit(self._preview_ready_sig, plan)
        except Exception as exc:
            self._log(f"BG3 Sort: preview error: {exc}")
            safe_emit(self._preview_error_sig, self.tr("Error: {0}").format(exc))

    def _on_preview_ready(self, plan):
        from Utils.mods.bg3_layers import LAYER_LABEL, LAYERS
        self._plan = plan
        self._tree.clear()
        old_pos = {m: i for i, m in enumerate(plan.previous_load_order)}
        new_pos = {m: i for i, m in enumerate(plan.load_order)}
        moved = {m.name for m in plan.moves}

        if plan.unresolved:
            head = QTreeWidgetItem([self.tr("Could not be fully satisfied"),
                                    str(len(plan.unresolved)), ""])
            self._tree.addTopLevelItem(head)
            for text in plan.unresolved:
                head.addChild(QTreeWidgetItem([text, "", ""]))
            head.setExpanded(True)

        groups: dict[str, QTreeWidgetItem] = {}
        for layer in LAYERS:
            groups[layer.id] = QTreeWidgetItem([self.tr(layer.label), "", ""])
        move_reason = {m.name: m.reason for m in plan.moves}
        for mod in plan.load_order:
            if mod not in plan.layers:
                continue
            layer, reason = plan.layers[mod]
            o, n = old_pos.get(mod), new_pos[mod]
            pos = f"{o + 1} → {n + 1}" if o is not None and o != n else str(n + 1)
            text = move_reason.get(mod, f"{LAYER_LABEL[layer]} ({reason})")
            if self._load_after_map.get(mod) and "you chose" not in text:
                text += "; " + self.tr("loads after {0} (your choice)").format(
                    ", ".join(self._load_after_map[mod]))
            item = QTreeWidgetItem([("• " if mod in moved else "   ") + mod,
                                    pos, text])
            item.setData(0, Qt.UserRole, mod)
            groups[layer].addChild(item)
        for layer in LAYERS:
            g = groups[layer.id]
            if g.childCount():
                g.setText(1, str(g.childCount()))
                self._tree.addTopLevelItem(g)
                g.setExpanded(True)
        self._tree.resizeColumnToContents(1)
        self._tree.setColumnWidth(0, 420)

        summary = self.tr("{0} mod(s) move (marked •).").format(len(plan.moves))
        if plan.collection_count:
            summary += "   " + self.tr(
                "{0} mod(s) follow your collection's order and are not moved."
            ).format(plan.collection_count)
        if plan.unresolved:
            summary += "   " + self.tr("{0} issue(s) listed at the top.").format(
                len(plan.unresolved))
        self._set_status(self._preview_summary, summary,
                         GREEN if not plan.moves and not plan.unresolved else "")
        self._apply_btn.setEnabled(plan.changed)
        self._after_box.clear()
        self._after_box.addItems(sorted(plan.layers, key=str.casefold))
        self._set_mod_actions(None)

    def _selected_mod(self):
        item = self._tree.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def _on_select(self, *_a):
        mod = self._selected_mod()
        self._set_mod_actions(mod)
        if mod and self._plan and mod in self._plan.layers:
            idx = self._layer_box.findData(self._plan.layers[mod][0])
            if idx >= 0:
                self._layer_box.setCurrentIndex(idx)

    def _set_layer(self, pin: bool):
        from Utils.mods.bg3_layers import set_override
        mod = self._selected_mod()
        if not mod or self._modlist_path is None:
            return
        layer = self._layer_box.currentData() if pin else None
        try:
            set_override(self._modlist_path.parent, mod, layer)
        except Exception as exc:
            self._log(f"BG3 Sort: could not save layer: {exc}")
            return
        self._log(f"BG3 Sort: {mod} → "
                  f"{layer if layer else 'automatic layer'}")
        self._compute_preview()

    def _load_after(self, add: bool):
        from Utils.mods.bg3_pak_index import (
            RuleConflict, add_load_after, clear_load_after)
        mod = self._selected_mod()
        if not mod or self._modlist_path is None:
            return
        profile_dir = self._modlist_path.parent
        try:
            if add:
                after = self._after_box.currentText()
                add_load_after(profile_dir, mod, after)
                self._log(f"BG3 Sort: {mod} will load after {after}")
            else:
                n = clear_load_after(profile_dir, mod)
                self._log(f"BG3 Sort: cleared {n} load-after choice(s) for {mod}")
        except RuleConflict as exc:
            self._set_status(self._preview_summary, str(exc), RED)
            return
        self._compute_preview()

    # ---- apply ------------------------------------------------------------------
    def _apply(self):
        if not self._plan:
            return
        from Utils.mods.bg3_sort import apply_plan
        try:
            path = apply_plan(self._plan)
            self._log(f"BG3 Sort: wrote sorted load order to {path} "
                      f"({len(self._plan.moves)} mod(s) moved)")
            self._ran = True     # refresh_modlist on _finish
            self._stack.setCurrentIndex(_PG_DONE)
        except Exception as exc:
            self._log(f"BG3 Sort: apply error: {exc}")
            self._apply_btn.setText(self.tr("Failed"))

    # ---- page 2: done -----------------------------------------------------------
    def _build_done_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Load order sorted"))
        self._make_note(lay,
                        self.tr("The mod list has been sorted.\n"
                        "Deploy to push the new load order to the game."))
        lay.addStretch(1)
        done = self._green_btn(self.tr("Done"))
        done.clicked.connect(self._finish)
        lay.addWidget(done, 0, Qt.AlignHCenter)
        return page

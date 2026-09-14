"""Qt view: sort the active BG3 profile's load order by mod dependency.

Computes the reorder on open (no file to pick — unlike the BG3MM import
wizard, the dependency data comes from the installed mods' own meta.lsx) →
preview the moves → apply.  All the sorting/planning logic lives in
``Utils.mods.bg3_sort``; the view just handles the preview text and calling
``ctx.refresh_modlist`` after apply.

Modeled on ``wizards_qt.bg3_import_view.BG3ImportView``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import active_palette, _c
from wizards_qt._view_base import RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_PREVIEW, _PG_DONE = range(2)


class BG3SortView(WizardViewBase):
    """Sort the active profile's modlist by mod dependency (meta.lsx)."""

    _preview_ready_sig = Signal(str, str)     # summary, detail
    _preview_error_sig = Signal(str)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Sort Load Order — {0}").format(game.name))
        self._plan = None

        self._preview_ready_sig.connect(self._guard(self._on_preview_ready))
        self._preview_error_sig.connect(self._guard(
            lambda t: self._set_status(self._preview_summary, t, RED)))

        self._stack.addWidget(self._build_preview_page())
        self._stack.addWidget(self._build_done_page())
        self._stack.setCurrentIndex(_PG_PREVIEW)
        self._compute_preview()

    # ---- page 1: preview --------------------------------------------------------
    def _build_preview_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Review changes"))
        self._make_note(lay,
                        self.tr("Reorders enabled mods so each mod's declared "
                        "dependencies (from meta.lsx) load before it. Mods with "
                        "no ordering opinion (separators, disabled mods, mods "
                        "with no .pak metadata) are left exactly where they are."))
        self._preview_summary = self._make_status(lay)
        p = active_palette()
        self._preview_box = QPlainTextEdit()
        self._preview_box.setReadOnly(True)
        self._preview_box.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._preview_box.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};}}")
        lay.addWidget(self._preview_box, 1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._apply_btn = self._green_btn(self.tr("Apply Order"))
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._apply)
        rh.addWidget(self._apply_btn)
        lay.addWidget(row)
        return page

    def _compute_preview(self):
        self._set_status(self._preview_summary,
                         self.tr("Scanning installed mods…"))
        threading.Thread(target=self._compute_preview_worker, daemon=True,
                         name="bg3-sort-preview").start()

    def _compute_preview_worker(self):
        from Utils.mods.bg3_sort import compute_sort_plan, format_preview
        try:
            profile = getattr(self._ctx, "profile_name", "") if self._ctx else ""
            plan = compute_sort_plan(self._game, profile)
            self._plan = plan
            summary, detail = format_preview(plan)
            safe_emit(self._preview_ready_sig, summary, detail)
        except Exception as exc:
            self._log(f"BG3 Sort: preview error: {exc}")
            safe_emit(self._preview_error_sig, self.tr("Error: {0}").format(exc))

    def _on_preview_ready(self, summary: str, detail: str):
        self._set_status(self._preview_summary, summary)
        self._preview_box.setPlainText(detail)
        self._apply_btn.setEnabled(bool(self._plan) and self._plan.changed)

    # ---- apply ------------------------------------------------------------------
    def _apply(self):
        if not self._plan:
            return
        from Utils.mods.bg3_sort import apply_plan
        try:
            path = apply_plan(self._plan)
            self._log(f"BG3 Sort: wrote new load order to {path}")
            self._ran = True     # refresh_modlist on _finish
            self._stack.setCurrentIndex(_PG_DONE)
        except Exception as exc:
            self._log(f"BG3 Sort: apply error: {exc}")
            self._apply_btn.setText(self.tr("Failed"))

    # ---- page 2: done -----------------------------------------------------------
    def _build_done_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Load order sorted"))
        self._make_note(lay,
                        self.tr("The modlist has been reordered by dependency.\n"
                        "Deploy to push the new load order to the game."))
        lay.addStretch(1)
        done = self._green_btn(self.tr("Done"))
        done.clicked.connect(self._finish)
        lay.addWidget(done, 0, Qt.AlignHCenter)
        return page

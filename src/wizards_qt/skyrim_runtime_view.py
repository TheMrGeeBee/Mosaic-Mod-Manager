"""Skyrim Runtime wizard — see and undo a game-version switch Mosaic made.

A collection built for the older Skyrim runtime SKSE64 supports (1.6.1170) makes
Mosaic switch the game from Steam's current build (1.7.104) when you install it —
automatically, before anything is downloaded (see
``Utils.modding_tools.skyrim_runtime``). This wizard shows which runtime the game
is on and gives the way back: a hash-verified revert from the backup taken at the
time. (Steam's own "Verify integrity of game files" also restores the current
build.)

Pages: Check → Revert. The Check page also offers Restore, because the files
can't be changed while Mosaic's mods are deployed.
"""

from __future__ import annotations

import html
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import err_text, ok_text
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_CHECK, _PG_RUN = range(2)


class SkyrimRuntimeView(WizardViewBase):
    """Show the installed Skyrim runtime and revert a Mosaic switch."""

    _done_enable_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Skyrim Runtime — {0}").format(game.name))
        from Utils.config_paths import get_game_config_dir
        self._game_root = game.get_game_path()
        self._state_dir = get_game_config_dir(game.name)
        self._info = None
        self._done_enable_sig.connect(self._guard(
            lambda: self._done_btn.setEnabled(True)))
        self._stack.addWidget(self._build_check_page())
        self._stack.addWidget(self._build_run_page(self.tr("Reverting")))
        self._stack.setCurrentIndex(_PG_CHECK)
        self._refresh_check()

    # ---- check page ------------------------------------------------------------
    def _build_check_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Game version"))
        self._make_note(lay, self.tr(
            "Collections built for SKSE64 need Skyrim 1.6.1170, but Steam serves a newer\n"
            "build. Mosaic switches the game for you when you install such a collection,\n"
            "backing up the original files first. From here you can see which version the\n"
            "game is on and undo that switch."))
        self._check_label = QLabel("")
        self._check_label.setTextFormat(Qt.RichText)
        self._check_label.setWordWrap(True)
        self._check_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lay.addSpacing(8)
        lay.addWidget(self._check_label)
        self._check_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 8, 0, 0)
        rh.setSpacing(8)
        rh.addStretch(1)
        recheck = QPushButton(self.tr("Re-check"))
        recheck.setCursor(Qt.PointingHandCursor)
        recheck.clicked.connect(self._refresh_check)
        rh.addWidget(recheck)
        self._restore_btn = self._orange_btn(self.tr("Restore game files"))
        self._restore_btn.clicked.connect(self._on_restore_clicked)
        rh.addWidget(self._restore_btn)
        self._revert_btn = self._accent_btn(self.tr("Revert to the original version"))
        self._revert_btn.clicked.connect(self._on_revert_clicked)
        rh.addWidget(self._revert_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    @staticmethod
    def _row(ok: bool, text: str) -> str:
        color = ok_text() if ok else err_text()
        return (f'<span style="color:{color}">{"✓" if ok else "✗"}</span> '
                f"{html.escape(text)}")

    def _refresh_check(self):
        from Utils.exe_launch.exe_launch import _wine_process_alive
        from Utils.modding_tools import skyrim_runtime as sr
        from Utils.wizard_support.pe_version import format_version

        if self._game_root is None:
            self._check_label.setText(self._row(False, self.tr("Game path is not configured.")))
            self._restore_btn.setVisible(False)
            self._revert_btn.setVisible(False)
            return
        info = sr.describe(self._game_root, self._state_dir,
                           game_running=_wine_process_alive("SkyrimSE.exe"))
        self._info = info
        ver = format_version(info.version) if info.version else self.tr("unreadable")
        if info.swapped:
            rows = [self._row(True, self.tr(
                "Skyrim is {0} — switched by Mosaic from {1}. The original files are "
                "backed up and can be restored.").format(
                    ver, format_version(info.recorded_from) if info.recorded_from else "?"))]
        else:
            rows = [self._row(True, self.tr(
                "Skyrim is {0}. Mosaic has not switched it, so there is nothing to revert "
                "here. (If Steam updated the game since a switch, the switch is gone too.)"
            ).format(ver))]
        if info.swapped:
            rows += [self._row(False, b) for b in info.revert_blockers]
        self._check_label.setText("<br>".join(rows))
        self._set_status(self._check_status, "")
        self._restore_btn.setVisible(sr.is_deployed(self._game_root))
        self._revert_btn.setVisible(info.swapped)
        self._revert_btn.setEnabled(info.can_revert)

    def _on_restore_clicked(self):
        run_restore = getattr(self._ctx, "run_restore", None)
        if run_restore is None:
            self._set_status(self._check_status, self.tr(
                "Restore is unavailable here — use Restore in the main window, "
                "then press Re-check."), RED)
            return
        self._restore_btn.setEnabled(False)
        self._set_status(self._check_status, self.tr("Restoring…"))

        def _done(ok: bool):
            if self._closing:
                return
            self._restore_btn.setEnabled(True)
            self._refresh_check()
            if not ok:
                self._set_status(self._check_status,
                                 self.tr("Restore failed — see the log."), RED)

        if not run_restore(_done):
            self._restore_btn.setEnabled(True)
            self._set_status(self._check_status, self.tr(
                "Couldn't start Restore right now (a deploy or restore may be running). "
                "Try again in a moment."), RED)

    # ---- revert ----------------------------------------------------------------
    def _on_revert_clicked(self):
        self._stack.setCurrentIndex(_PG_RUN)
        self._done_btn.setEnabled(False)
        self._set_status(self._run_status, self.tr("Restoring the original files…"))
        threading.Thread(target=self._revert_worker, daemon=True,
                         name="skyrim-runtime-revert").start()

    def _revert_worker(self):
        from Utils.modding_tools import skyrim_runtime as sr
        try:
            sr.revert_transition(
                self._game_root, self._state_dir,
                log_fn=lambda m: self._log(f"Skyrim Runtime Wizard: {m}"),
                prefix_path=self._game.get_prefix_path())
            safe_emit(self._run_status_sig, self.tr(
                "Skyrim is back to its original version.\n\nThe next collection install "
                "that needs the older runtime will switch it again.\n\nClick Done to close."),
                GREEN)
        except sr.RuntimeSwapError as exc:
            self._log(f"Skyrim Runtime Wizard: {exc}")
            safe_emit(self._run_status_sig, str(exc), RED)
        except Exception as exc:                       # noqa: BLE001 — surface, don't crash the worker
            self._log(f"Skyrim Runtime Wizard: unexpected error: {exc}")
            safe_emit(self._run_status_sig, self.tr("Unexpected error: {0}").format(exc), RED)
        finally:
            safe_emit(self._done_enable_sig)

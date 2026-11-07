"""Skyrim Runtime wizard — apply or undo a game-version switch.

A modlist built for the older Skyrim runtime SKSE64 supports (1.6.1170) needs
the exe patched back from Steam's current build (1.7.104) while the game's data
files stay where they are (see ``Utils.modding_tools.skyrim_runtime``).

A *Nexus collection* gets that automatically: its preflight names the runtime-swap
mod it lists, fetches it and applies it before anything is downloaded. A
*Wabbajack* modlist does not — it carries its own patched copy of the game in a
"Stock Game" folder, which Mosaic doesn't build, so all its preflight can do is
say which runtime the list's script extender needs. That left the swap with no
way to be applied at all outside a collection install, and an installed list
whose SKSE64 silently refuses to load. This wizard is that way: point it at a
runtime-swap mod's archive and it applies the same hash-verified patches.

It is also the way back, from the backup taken at the time. (Steam's own "Verify
integrity of game files" also restores the current build.)

Pages: Check → Get the patch → Locate → Apply/Revert. Check also offers Restore,
because the game's files can't be patched while Mosaic's mods are deployed.
"""

from __future__ import annotations

import html
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme.theme_qt import err_text, ok_text
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_CHECK, _PG_RUN, _PG_GET, _PG_LOCATE = range(4)

# The Nexus mod whose RuntimeSwap/ folder Mosaic can apply natively. No file is
# pinned: the mod ships several variants (plain, "Best of All Worlds", "BoAW and
# Cleaned"), the choice between them is the user's, and what makes an archive
# usable is the manifest inside it rather than its file id.
_SWAP_MOD_ID = 189855
_SWAP_URL = f"https://www.nexusmods.com/skyrimspecialedition/mods/{_SWAP_MOD_ID}?tab=files"


class SkyrimRuntimeView(WizardViewBase):
    """Show the installed Skyrim runtime and revert a Mosaic switch."""

    _done_enable_sig = Signal()
    _prepared_sig = Signal(object, object)     # (PreparedSwap | None, error)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Skyrim Runtime — {0}").format(game.name))
        from Utils.config_paths import get_game_config_dir
        self._game_root = game.get_game_path()
        self._state_dir = get_game_config_dir(game.name)
        self._info = None
        self._prepared = None
        self._done_enable_sig.connect(self._guard(
            lambda: self._done_btn.setEnabled(True)))
        self._prepared_sig.connect(self._guard(self._on_prepared))
        self._stack.addWidget(self._build_check_page())
        self._stack.addWidget(self._build_run_page(self.tr("Switching")))
        self._stack.addWidget(self._build_get_page())
        self._stack.addWidget(self._build_locate_page(self.tr("Locate the runtime swap")))
        self._stack.setCurrentIndex(_PG_CHECK)
        self._refresh_check()

    # ---- check page ------------------------------------------------------------
    def _build_check_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Game version"))
        self._make_note(lay, self.tr(
            "Modlists built for SKSE64 need Skyrim 1.6.1170, but Steam serves a newer\n"
            "build. Only the exe and a few files it ships with are patched — the game's\n"
            "data stays on the build you have, which is what these lists are built\n"
            "against. The original files are backed up first, so the switch can be undone."))
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
        self._apply_btn = self._accent_btn(self.tr("Switch to the older runtime…"))
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        rh.addWidget(self._apply_btn)
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
            self._apply_btn.setVisible(False)
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
            rows.append(self._row(True, self.tr(
                "To switch it, point Mosaic at a runtime-swap mod's archive — the patches "
                "inside say which version they go from and to.")))
        if info.swapped:
            rows += [self._row(False, b) for b in info.revert_blockers]
        self._check_label.setText("<br>".join(rows))
        self._set_status(self._check_status, "")
        self._restore_btn.setVisible(sr.is_deployed(self._game_root))
        self._revert_btn.setVisible(info.swapped)
        self._revert_btn.setEnabled(info.can_revert)
        # Nothing to apply on top of a swap that is already in place; revert first.
        self._apply_btn.setVisible(not info.swapped)

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

    # ---- apply: get the patch, locate it, run it -------------------------------
    def _build_get_page(self) -> QWidget:
        return self._build_manual_download_page(
            self.tr("Get the runtime swap"),
            self.tr(
                "The patches live in a Nexus mod. Download the file you want with\n"
                "MANUAL download — any of its variants works, and which one is your\n"
                "choice: the plain swapper changes the least, the \"Best of All Worlds\"\n"
                "and \"Cleaned\" files also fix the game's own masters.\n\n"
                "A copy already in your Downloads folder or Mosaic's download cache is\n"
                "found on the next page, so a file you have is not downloaded again."),
            _SWAP_URL, self._goto_locate,
            button_text=self.tr("Open the Nexus page"),
            next_text=self.tr("I have it →"))

    def _on_apply_clicked(self):
        self._stack.setCurrentIndex(_PG_GET)

    def _goto_locate(self):
        from Utils.config_paths import list_all_cache_dirs
        # An nxm:// download is handled by Mosaic, not the browser, so the
        # archive may be in a download cache rather than in Downloads.
        self._locate_extra_dirs = list_all_cache_dirs(self._game.name)
        self._stack.setCurrentIndex(_PG_LOCATE)
        self._enter_locate(
            [str(_SWAP_MOD_ID)],
            self.tr("Select the runtime swap archive"),
            self.tr("No runtime swap archive found. Download it, or use Browse… to "
                    "pick the file yourself."),
            self._on_swap_archive)

    def _on_swap_archive(self, path):
        """An archive to try. Whether it really is a runtime swap, and whether it
        fits this install, is settled by reading it — not by its name."""
        if path is None:
            return
        self._stack.setCurrentIndex(_PG_RUN)
        self._done_btn.setEnabled(False)
        self._set_status(self._run_status, self.tr("Reading {0}…").format(Path(path).name))
        threading.Thread(target=self._prepare_worker, args=(Path(path),), daemon=True,
                         name="skyrim-runtime-prepare").start()

    def _prepare_worker(self, archive: Path):
        from Utils.collections import runtime_swap_fix as fix
        from Utils.modding_tools import skyrim_runtime as sr
        log = lambda m: self._log(f"Skyrim Runtime Wizard: {m}")      # noqa: E731
        try:
            hpatchz = sr.ensure_hpatchz(log_fn=log)
            prepared = fix.prepare_swap_from_archive(archive, log_fn=log)
            safe_emit(self._prepared_sig, (prepared, hpatchz), None)
        except sr.RuntimeSwapError as exc:
            log(str(exc))
            safe_emit(self._prepared_sig, None, str(exc))
        except Exception as exc:                       # noqa: BLE001 — surface, don't kill the worker
            log(f"unexpected error: {exc}")
            safe_emit(self._prepared_sig, None, self.tr("Unexpected error: {0}").format(exc))

    def _on_prepared(self, loaded, err):
        """UI thread: the patches are read. Restore first if the mods are
        deployed — the game's own files can't be patched underneath them."""
        from Utils.modding_tools import skyrim_runtime as sr
        from Utils.wizard_support.pe_version import format_version
        if err or loaded is None:
            self._set_status(self._run_status, err or self.tr("Couldn't read the patches."), RED)
            self._done_btn.setEnabled(True)
            return
        prepared, hpatchz = loaded
        self._prepared = prepared
        going = self.tr("Switching Skyrim from {0} to {1}…").format(
            format_version(prepared.transition.source),
            format_version(prepared.transition.target))
        if not sr.is_deployed(self._game_root):
            self._set_status(self._run_status, going)
            self._start_apply(hpatchz)
            return
        run_restore = getattr(self._ctx, "run_restore", None)
        if run_restore is None:
            self._fail_apply(self.tr(
                "Mosaic's mods are deployed and Restore isn't available here. Use Restore "
                "in the main window, then run this wizard again."))
            return
        self._set_status(self._run_status, self.tr("Restoring the game to vanilla first…"))

        def _restored(ok: bool):
            if self._closing:
                return
            if not ok:
                self._fail_apply(self.tr("Restore failed — see the log. Nothing was patched."))
                return
            self._set_status(self._run_status, going)
            self._start_apply(hpatchz)

        if not run_restore(_restored):
            self._fail_apply(self.tr(
                "Couldn't start Restore right now (a deploy or restore may be running). "
                "Try again in a moment."))

    def _start_apply(self, hpatchz: str):
        threading.Thread(target=self._apply_worker, args=(hpatchz,), daemon=True,
                         name="skyrim-runtime-apply").start()

    def _apply_worker(self, hpatchz: str):
        from Utils.modding_tools import skyrim_runtime as sr
        from Utils.wizard_support.pe_version import format_version
        prepared = self._prepared
        try:
            version = sr.apply_transition(
                self._game_root, prepared.transition, self._state_dir, hpatchz=hpatchz,
                log_fn=lambda m: self._log(f"Skyrim Runtime Wizard: {m}"),
                prefix_path=self._game.get_prefix_path())
            self._ran = True
            safe_emit(self._run_status_sig, self.tr(
                "Skyrim is now {0}.\n\nDeploy your mods again and the patched files are "
                "snapshotted like any other vanilla file. Don't verify the game's files in "
                "Steam — that puts the newer build back.\n\nClick Done to close.")
                .format(format_version(version)), GREEN)
        except sr.RuntimeSwapError as exc:
            self._log(f"Skyrim Runtime Wizard: {exc}")
            safe_emit(self._run_status_sig, str(exc), RED)
        except Exception as exc:                       # noqa: BLE001 — surface, don't kill the worker
            self._log(f"Skyrim Runtime Wizard: unexpected error: {exc}")
            safe_emit(self._run_status_sig, self.tr("Unexpected error: {0}").format(exc), RED)
        finally:
            self._cleanup_prepared()
            safe_emit(self._done_enable_sig)

    def _fail_apply(self, text: str):
        self._set_status(self._run_status, text, RED)
        self._cleanup_prepared()
        self._done_btn.setEnabled(True)

    def _cleanup_prepared(self):
        prepared, self._prepared = self._prepared, None
        if prepared is not None:
            prepared.cleanup()

    def _finish(self):
        self._cleanup_prepared()        # the extracted patches are a temp folder
        super()._finish()

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

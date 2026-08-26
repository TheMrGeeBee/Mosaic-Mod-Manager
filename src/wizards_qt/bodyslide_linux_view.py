"""BodySlide / Outfit Studio (Native Linux) wizard.

Uses the managed portable build from ChrisDKN/BodySlide-and-Outfit-Studio-
Appimage (Utils.bodyslide_linux) — one shared install across every game,
driven entirely by BSOS_TARGET_GAME / BSOS_GAME_DATA_PATH /
BSOS_OUTPUT_DATA_PATH environment variables, which win over the stored
Config.xml on every launch. No Proton, no Config.xml patching, unlike the
Windows-via-Proton wizard (wizards_qt/bodyslide_view.py).

Flow: install-or-update the shared build -> deploy (with an output-mod-name
entry, same shape as the Proton wizard) -> run.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QWidget

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.modding_tools.bodyslide_tools import sanitize_output_name

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_TOOLS = {
    "bodyslide":    ("BodySlide", "BodySlide", "BodySlide_files"),
    "outfitstudio": ("Outfit Studio", "OutfitStudio", "OutfitStudio_files"),
}

_PG_INSTALL, _PG_DEPLOY, _PG_RUN = range(3)


class BodySlideLinuxView(WizardViewBase):
    """Install/update the shared native build, deploy mods, and run
    BodySlide / Outfit Studio with the current game targeted via env vars."""

    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, tool: str = "bodyslide", **_extra):
        self._name, self._exe_name, self._output_default = _TOOLS[tool]
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("{0} (Native Linux) — {1}").format(
                             self._name, game.name))
        self._output_mod_name = self._output_default

        self._install_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._install_status, t, c)))
        self._install_done_sig.connect(self._guard(self._on_install_done))

        self._stack.addWidget(self._build_install_page())
        self._stack.addWidget(self._build_bs_deploy_page())
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 3: Run {0}").format(self._name)))
        self._goto_step(_PG_INSTALL)

    # ---- step 1: install/update the shared build -----------------------------
    def _build_install_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Install / Update"))
        self._make_note(lay, self.tr(
            "Downloads and keeps up to date a shared, portable build of "
            "BodySlide and Outfit Studio — no Wine/Proton prefix needed. "
            "Shared across every game."))
        self._install_status = self._make_status(lay)
        lay.addStretch(1)
        return page

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_INSTALL:
            self._start_install()
        elif idx == _PG_RUN:
            self._set_status(self._run_status, self.tr("Launching {0}…").format(self._name))
            self._start_run()

    def _start_install(self):
        self._set_status(self._install_status, self.tr("Checking for updates…"))

        def worker():
            import Utils.bodyslide_linux as bl
            ok = bl.install_or_update(log_fn=self._log)
            safe_emit(self._install_status_sig,
                     self.tr("Ready.") if ok else
                     self.tr("Install failed — see log."),
                     GREEN if ok else RED)
            safe_emit(self._install_done_sig, ok)

        threading.Thread(target=worker, daemon=True,
                         name="bodyslide-linux-install").start()

    def _on_install_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_DEPLOY)

    # ---- step 2: deploy --------------------------------------------------------
    def _build_bs_deploy_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Deploy Modlist"))
        self._make_note(lay, self.tr(
            '{0} must be run from the deployed Data folder.\n\nDeploy your modlist first, then click Run.').format(self._name))

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 4, 0, 4); rh.setSpacing(8)
        rh.addStretch(1)
        lbl = QLabel(self.tr("Output mod name:"))
        lbl.setStyleSheet(self._dim)
        rh.addWidget(lbl)
        self._output_name_entry = QLineEdit()
        self._output_name_entry.setPlaceholderText(self._output_default)
        self._output_name_entry.setMinimumWidth(220)
        rh.addWidget(self._output_name_entry)
        rh.addStretch(1)
        lay.addWidget(row)

        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        from PySide6.QtWidgets import QPushButton
        from PySide6.QtCore import Qt
        brow = QWidget()
        bh = QHBoxLayout(brow); bh.setContentsMargins(0, 8, 0, 0); bh.setSpacing(8)
        bh.addStretch(1)
        self._deploy_skip_btn = QPushButton(self.tr("Skip"))
        self._deploy_skip_btn.setCursor(Qt.PointingHandCursor)
        self._deploy_skip_btn.clicked.connect(self._skip_deploy)
        bh.addWidget(self._deploy_skip_btn)
        self._deploy_btn = self._accent_btn(self.tr("Deploy"))
        self._deploy_btn.clicked.connect(self._start_bs_deploy)
        bh.addWidget(self._deploy_btn)
        bh.addStretch(1)
        lay.addWidget(brow)
        return page

    def _capture_output_mod_name(self):
        self._output_mod_name = sanitize_output_name(
            self._output_name_entry.text(), self._output_default)

    def _profile(self) -> str:
        return getattr(self._ctx, "profile_name", None) or "default"

    def _ensure_output_mod(self):
        from Utils.modding_tools.bodyslide_tools import ensure_output_mod
        try:
            path = ensure_output_mod(self._game, self._profile(), self._output_mod_name)
            self._ran = True   # modlist gained the output mod — refresh on close
            return path
        except Exception as exc:
            self._log(f"{self._name} Wizard: could not create output mod: {exc}")
            return None

    def _skip_deploy(self):
        self._capture_output_mod_name()
        self._ensure_output_mod()
        self._goto_step(_PG_RUN)

    def _start_bs_deploy(self):
        self._capture_output_mod_name()
        self._deploy_btn.setEnabled(False)
        self._deploy_skip_btn.setEnabled(False)
        # Materialize the output-capture mod BEFORE the deploy so the
        # filemap picks it up (same ordering as the Proton wizard).
        self._ensure_output_mod()

        def _re_enable():
            self._deploy_btn.setEnabled(True)
            self._deploy_skip_btn.setEnabled(True)

        if not self._run_ctx_deploy(self._deploy_status,
                                    lambda: self._goto_step(_PG_RUN),
                                    _re_enable):
            _re_enable()

    # ---- step 3: run ------------------------------------------------------------
    def _start_run(self):
        game, name, exe_name = self._game, self._name, self._exe_name
        output_mod_name, profile = self._output_mod_name, self._profile()

        def worker():
            import subprocess
            import Utils.bodyslide_linux as bl
            from Utils.modding_tools.bodyslide_tools import ensure_output_mod
            _wlog = lambda m: self._log(f"{name} Wizard: {m}")
            try:
                try:
                    output_mod_path = ensure_output_mod(game, profile, output_mod_name)
                except Exception as exc:
                    safe_emit(self._run_status_sig,
                              self.tr("Could not create output mod: {0}").format(exc), RED)
                    return

                resolved = bl.launch_env(exe_name, game, output_mod_path)
                if resolved is None:
                    safe_emit(self._run_status_sig,
                              self.tr("{0} does not support this game, or the "
                              "build is not installed — reopen this wizard.")
                              .format(name), RED)
                    return
                launcher, env = resolved

                _wlog(f"launching {launcher} (cwd={launcher.parent})")
                safe_emit(self._run_status_sig,
                          self.tr("{0} is running.\nClose it when you are done, "
                          "then click Done.").format(name), GREEN)
                safe_emit(self._run_started_sig)

                proc = subprocess.Popen([str(launcher)], env=env,
                                        cwd=str(launcher.parent))
                proc.wait()
                _wlog(f"{launcher.name} closed.")
                safe_emit(self._run_status_sig, self.tr("{0} finished.").format(name), GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig, self.tr("Launch error: {0}").format(exc), RED)
                self._log(f"{name} Wizard: launch error: {exc}")

        threading.Thread(target=worker, daemon=True,
                         name="bodyslide-linux-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)

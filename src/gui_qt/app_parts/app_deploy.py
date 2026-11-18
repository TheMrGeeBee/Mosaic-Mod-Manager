"""Deploy and restore: buttons, deploy/restore workers, close-time restore and operation progress/done handling.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QTimer


class MainWindow:

    def _set_deploy_buttons_enabled(self, enabled: bool):
        for b in (getattr(self, "_deploy_btn", None), getattr(self, "_restore_btn", None),
                  getattr(self, "_play_btn", None)):
            if b is not None:
                b.setEnabled(enabled)

    def _prompt_mewgenics_deploy(self, game):
        """Ask whether to generate a Steam launch command or repack the gpak,
        then act on the choice (Tk parity: gui/mewgenics_dialogs.py)."""
        from gui_qt.overlays.mewgenics_deploy_overlay import (
            MewgenicsDeployChoiceOverlay,
            MewgenicsLaunchCommandOverlay,
        )
        profile = self._gs.profile

        def _on_choice(result):
            if result is None:
                return
            if result == "steam":
                try:
                    launch_string, modpaths_file = \
                        game.get_modpaths_launch_string(profile)
                except Exception as exc:
                    self._notify(
                        self.tr("Could not build launch command: {0}")
                        .format(exc), "warning")
                    return
                MewgenicsLaunchCommandOverlay(
                    self.window(), launch_string, modpaths_file)
                return
            # result == "repack" -> run the normal deploy pipeline
            self._start_deploy(game, profile)

        MewgenicsDeployChoiceOverlay(self.window(), _on_choice)

    def _start_deploy(self, game, profile, silent: bool = False):
        """Run the deploy worker for *game* / *profile* without re-prompting
        (used by the Mewgenics 'repack' choice, which has already decided)."""
        if self._deploy_running:
            self._deploy_rerun_pending = True
            self._auto_deploy_in_progress = False
            return
        # A manual install, a collection install, or a detached-wizard staging
        # job mutates the shared game (staging root + active profile) — a
        # deploy started now could resolve the wrong profile / race the
        # staging tree, same hazard _install_paths guards against on the
        # install side (see its comment for the full explanation). Defer
        # until all of them drain; released by _drain_pending_after_staged.
        # Coalesce duplicate deferrals per game.
        if getattr(self, "_install_running", False) \
                or getattr(self, "_col_install_running", False) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            self._auto_deploy_in_progress = False
            if not any(getattr(cb, "_kind", None) == "deploy"
                       for cb in self._pending_after_staged):
                def _later(g=game, p=profile, s=silent):
                    self._start_deploy(g, p, silent=s)
                _later._kind = "deploy"
                self._pending_after_staged.append(_later)
            return
        self._deploy_running = True
        self._op_silent = silent
        self._op_is_restore = False
        self._op_title = "Deploying"
        self._set_deploy_buttons_enabled(False)
        if not silent:
            self._ensure_feedback()
            self._notify(self.tr("Deploying {0}…").format(game.name), "info")
        rf_enabled = True


        def worker():
            from Utils.deploy.deploy_pipeline import run_deploy_pipeline
            ok = False
            warns = []
            try:
                ok = run_deploy_pipeline(
                    game, profile,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, p=None: self._op_progress.emit(d, t, p),
                    root_folder_enabled=rf_enabled,
                    confirm_cet=self._make_confirm_cet_cb(game),
                    do_backup=True,
                )
            except Exception as exc:
                self._op_log.emit(f"Deploy error: {exc}")
            finally:
                try:
                    warns = list(game.pop_deploy_warnings())
                except Exception:
                    warns = []
                self._op_done.emit("deploy", bool(ok), warns)

        threading.Thread(target=worker, daemon=True).start()

    def _on_deploy(self, silent: bool = False):
        """Deploy the current game/profile. *silent* (used by auto-deploy)
        suppresses the progress popup + interim toast so rapid mod toggles
        don't flash the UI; log lines + the final success/warning toasts still
        surface (Tk parity: top_bar._run_deploy silent=)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._auto_deploy_in_progress = False
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if not hasattr(game, "deploy"):
            self._auto_deploy_in_progress = False
            self._notify(self.tr("'{0}' does not support deployment.").format(game.name), "warning")
            return

        # A game exposing get_modpaths_launch_string (Mewgenics) offers a Steam
        # launch command as an alternative to repacking its archive (Tk parity:
        # top_bar._on_deploy). Only prompt for an explicit user deploy —
        # auto-deploy (silent) falls straight through to the repack path.
        if not silent and hasattr(game, "get_modpaths_launch_string"):
            self._prompt_mewgenics_deploy(game)
            return

        self._start_deploy(game, self._gs.profile, silent=silent)

    def _restore_all_on_close(self):
        """Restore every configured game that has an active deployment back to
        vanilla. Ported from gui.py `_restore_all_on_close`."""
        from gui_qt.game_state import _GAMES
        from Utils.deploy.deploy import restore_root_folder

        games = [g for g in _GAMES.values()
                 if g.is_configured() and g.get_deploy_active()
                 and getattr(g, "restore_on_close_eligible", True)]
        if not games:
            return
        log_fn = lambda m: print(f"[restore-on-close] {m}", flush=True)
        log_fn(f"restoring {len(games)} game(s)...")
        for game in games:
            try:
                game_root = game.get_game_path()
                last_deployed = game.get_last_deployed_profile()
                original_profile_dir = getattr(game, "_active_profile_dir", None)
                if last_deployed:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / last_deployed)
                    # Reload so the last-deployed profile's path overrides drive
                    # the restore (it may target a different game folder).
                    game.load_paths()
                    game_root = game.get_game_path()
                try:
                    if hasattr(game, "restore"):
                        game.restore(log_fn=log_fn)
                    root_folder_dir = game.get_effective_root_folder_path()
                    if root_folder_dir.is_dir() and game_root:
                        restore_root_folder(root_folder_dir, game_root, log_fn=log_fn)
                    game.clear_deploy_active()
                finally:
                    # Restored unconditionally — skipping this when the entry
                    # value was None left the game on the LAST-DEPLOYED profile,
                    # so later staging/meta reads resolved to the wrong folder.
                    game.set_active_profile_dir(original_profile_dir)
                    game.load_paths()
            except Exception as e:
                log_fn(f"error for {game.name}: {e}")

    def _on_restore(self, *, ignore_col_install: bool = False):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._deploy_running:
            self._notify(self.tr("A deploy is in progress — try again shortly."), "warning")
            return
        # Defer behind a manual install, a collection install, or a
        # detached-wizard staging job (all mutate the shared game's active
        # profile — see _install_paths / _start_deploy for the full
        # explanation). Released by _drain_pending_after_staged. Coalesce
        # duplicate restore requests.
        # *ignore_col_install* — the collection preflight (see
        # _preflight_restore_then_apply) runs its own Restore step WHILE
        # _col_install_running is True, before any profile exists or any
        # file is staged. Queuing behind "the current install" there would
        # deadlock: the install is itself blocked waiting on this restore.
        if getattr(self, "_install_running", False) \
                or (getattr(self, "_col_install_running", False) and not ignore_col_install) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            if not any(getattr(cb, "_kind", None) == "restore"
                       for cb in self._pending_after_staged):
                def _later():
                    self._on_restore()
                _later._kind = "restore"
                self._pending_after_staged.append(_later)
            self._notify(self.tr("Restore queued — it will run after the "
                                 "current install finishes."), "info")
            return
        self._deploy_running = True
        self._op_is_restore = True
        self._op_title = "Restoring"
        self._set_deploy_buttons_enabled(False)
        self._ensure_feedback()
        self._notify(self.tr("Restoring {0}…").format(game.name), "info")
        profile = self._gs.profile

        from Utils.deploy.deploy import restore_root_folder

        def worker():
            ok = True
            try:
                from Utils.deploy.deploy_pipeline import check_paths_mounted
                err = check_paths_mounted(game)
                if err:
                    self._op_log.emit(f"Restore aborted: {err}")
                    ok = False
                else:
                    last = game.get_last_deployed_profile()
                    if last:
                        game.set_active_profile_dir(
                            game.get_profile_root() / "profiles" / last)
                        game.load_paths()
                    game_root = game.get_game_path()
                    if hasattr(game, "restore"):
                        game.restore(
                            log_fn=lambda m: self._op_log.emit(str(m)),
                            progress_fn=lambda d, t, p=None: self._op_progress.emit(d, t, p))
                    rf = game.get_effective_root_folder_path()
                    if rf.is_dir() and game_root:
                        restore_root_folder(
                            rf, game_root,
                            log_fn=lambda m: self._op_log.emit(str(m)),
                            data_deploy_dirs=(game.root_restore_protect_dirs()
                                              if hasattr(game, "root_restore_protect_dirs") else None))
            except Exception as exc:
                ok = False
                self._op_log.emit(f"Restore error: {exc}")
            finally:
                # Always restore the active profile dir to the selected profile.
                try:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / profile)
                    game.load_paths()
                    if ok and hasattr(game, "clear_deploy_active"):
                        game.clear_deploy_active()
                except Exception:
                    pass
                self._op_done.emit("restore", ok, [])

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_op_clear(self, delay_ms: int = 1200):
        """Hide the shared "op" progress card after *delay_ms*. Cancellable —
        if another operation emits progress before it fires, the pending clear
        is dropped so it can't hide the NEW operation's card (e.g. a queued
        install batch that starts right after the previous one finished)."""
        t = getattr(self, "_op_clear_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(
                lambda: self._progress_popup.clear()
                if self._progress_popup is not None else None)
            self._op_clear_timer = t
        t.start(delay_ms)

    def _on_op_progress(self, done: int, total: int, phase):
        if getattr(self, "_op_silent", False):
            return   # silent auto-deploy: no progress popup
        if self._progress_popup is not None:
            # A new/ongoing op is reporting — a clear scheduled by the previous
            # op's completion must not hide this op's card.
            t = getattr(self, "_op_clear_timer", None)
            if t is not None:
                t.stop()
            title = getattr(self, "_op_title", "Working")
            self._progress_popup.set_progress(done, total, phase, title=title)

    def _on_op_done(self, kind: str, success: bool, warnings):
        self._deploy_running = False
        self._op_silent = False
        self._set_deploy_buttons_enabled(True)
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        # Any deploy (manual OR auto) is followed by a modlist reload → conflict
        # rebuild → _on_conflicts_ready. With auto_deploy on, that rebuild would
        # otherwise start a *fresh* auto-deploy every time (an endless loop, and
        # an extra deploy even after a manual Deploy press). Arm the guard so the
        # rebuild caused by THIS deploy's reload is swallowed. (Tk parity: the
        # gui.py entry path + top_bar._on_deploy_finished both set this flag.)
        game = self._gs.game
        if (kind == "deploy" and game is not None and game.is_configured()
                and getattr(game, "auto_deploy", False)):
            self._auto_deploy_in_progress = True
        # Refresh the modlist/conflicts + deployed-profile highlight after the op.
        self._reload_modlist()
        self._update_deployed_profile_highlight()
        self._refresh_framework_banner()
        # Deploy/restore adds/removes framework launchers (script extenders)
        # in the game root — reflect that in the play-bar dropdown.
        self._refresh_play_selector()
        game_name = self._gs.game.name if self._gs.game else self.tr("Game")
        if success:
            msg = (self.tr("{0} Deployed") if kind == "deploy"
                   else self.tr("{0} Restored")).format(game_name)
            self._notify(msg, "success")
        else:
            self._notify(self.tr("Deploy failed — see log.") if kind == "deploy"
                         else self.tr("Restore failed — see log."), "error")
        for w in (warnings or []):
            self._notify(w, "warning")
        # Drain any install batch that was queued behind this deploy/restore
        # (see _install_paths). Deferred so it starts after this handler +
        # the coalesced re-deploy check below settle. Skipped if a coalesced
        # re-deploy is about to run — the install stays queued until it too
        # finishes (the drain guards on _deploy_running/_install_running).
        if self._pending_install_batches and not self._deploy_rerun_pending:
            QTimer.singleShot(0, self._drain_pending_installs)
        # A detached-wizard staging job may have queued behind this deploy —
        # drain it once the deploy is fully settled (skipped if a coalesced
        # re-deploy is about to run; _run_staged_finish re-guards on
        # _deploy_running so it stays queued until that finishes too).
        if self._staged_finish_queue and not self._deploy_rerun_pending:
            QTimer.singleShot(0, self._run_staged_finish)
        # Coalesced re-deploy if mod state changed mid-deploy.
        if kind == "deploy" and self._deploy_rerun_pending:
            self._deploy_rerun_pending = False
            # A coalesced re-deploy already reflects a decided deploy — run it
            # directly (no fresh prompt/toast; matters for the Mewgenics choice).
            game = self._gs.game
            if game is not None and game.is_configured():
                QTimer.singleShot(
                    0, lambda g=game, p=self._gs.profile: self._start_deploy(g, p))
            else:
                # The re-deploy won't run (game gone/unconfigured) — the drains
                # skipped above on _deploy_rerun_pending would otherwise strand
                # the queues. Release them now.
                if self._pending_install_batches:
                    QTimer.singleShot(0, self._drain_pending_installs)
                if self._staged_finish_queue:
                    QTimer.singleShot(0, self._run_staged_finish)
        elif kind == "deploy":
            # Play-button deploy-before-launch: fire the pending launch only
            # after the FINAL (non-coalesced) deploy, and only on success.
            action, self._post_deploy_action = self._post_deploy_action, None
            if action is not None and success:
                action()
            elif action is not None:
                self._append_log(
                    "Play: deploy failed — the pending launch was cancelled.")
            # Wizard deploy steps: one-shot completion hooks (get the outcome
            # either way so the wizard can show failure and re-enable Deploy).
            hooks, self._deploy_done_hooks = self._deploy_done_hooks, []
            for h in hooks:
                try:
                    h(success)
                except Exception as exc:
                    self._append_log(f"Wizards: deploy hook error: {exc}")
        elif kind == "restore":
            hooks, self._restore_done_hooks = self._restore_done_hooks, []
            for h in hooks:
                try:
                    h(success)
                except Exception as exc:
                    self._append_log(f"Wizards: restore hook error: {exc}")

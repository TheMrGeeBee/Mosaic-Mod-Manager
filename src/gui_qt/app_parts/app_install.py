"""Mod install pipeline: prompts, batch/parallel install, wizard finish, previous-version cleanup, rename and completion.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QTimer

# Sentinel emitted on _one_install_done when a FOMOD/BAIN archive was handed off
# to a detached wizard tab: the pipeline advances to the next queued archive
# without counting or renaming it (the detached wizard owns its own outcome).
_WIZARD_HANDOFF = object()


class MainWindow:

    def _install_paths(self, paths: list[str], metas: dict | None = None,
                       previous_mod_name: str | None = None,
                       preferred_names: dict | None = None,
                       on_all_done=None, clear_archives: bool = True):
        """Queue + install a list of archive paths (shared by the Install Mod
        button and the Downloads tab). FOMODs pause for the wizard mid-queue.
        *metas* optionally maps an archive path → a prebuilt NexusModMeta (the
        Nexus browser supplies the real mod_id/file_id so meta.ini is correct).
        *previous_mod_name* — when set (Change Version updating an existing mod),
        and a single install lands under a DIFFERENT folder name, offer to remove
        that previous version (it inherits its modlist slot).
        *preferred_names* — optional archive-path → forced mod-folder name. Forces
        the install into that folder and SILENTLY replaces it (no Mod-Already-Exists
        dialog). Used by Quick Update, where the name match is already confirmed.
        *on_all_done* — optional no-arg callback fired once the whole batch finishes
        (after the summary), so a caller can chain post-install work (Quick Update
        re-checks flags + reports its own summary).
        *clear_archives* — False for archives the USER supplied (Install Mod
        button, Downloads tab, reinstall-from-archive): 'Clear archive after
        install' only applies to archives the app downloaded itself."""
        if not paths:
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # Queue behind a running install OR deploy OR a detached-wizard staging
        # job. All three mutate the SAME shared game object (_active_profile_dir +
        # load_paths → get_effective_*_path); a deploy swaps the active profile to
        # last_deployed/target mid-flight (deploy_pipeline), so an install started
        # concurrently can stage its mod + write modindex.bin into the WRONG
        # profile's tree — the mod then appears only after a Refresh (the
        # intermittent "manual install didn't take" bug). Serialize instead.
        # _on_install_done, _on_op_done (deploy) AND _on_wizard_finish_done (staged
        # finish) all drain the queue.
        if getattr(self, "_install_running", False) \
                or getattr(self, "_deploy_running", False) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            self._pending_install_batches.append({
                "paths": list(paths), "metas": metas,
                "previous_mod_name": previous_mod_name,
                "preferred_names": preferred_names,
                "on_all_done": on_all_done,
                "clear_archives": clear_archives})
            _busy = self.tr("install") if getattr(self, "_install_running", False) \
                else (self.tr("deploy") if getattr(self, "_deploy_running", False)
                      else self.tr("install"))
            self._notify(
                self.tr("Install queued — {0} will install after the current "
                        "{1} finishes.").format(Path(paths[0]).name, _busy), "info")
            return
        # A just-finished background worker (deploy/restore/collection install)
        # may have left the game's _active_profile_dir out of sync with the
        # selected profile — re-assert it so staging + the mod index resolve
        # against the profile the dropdown shows (Tk parity: gui.py re-ran
        # load_paths before installing).
        self._gs.reassert_active_profile()
        profile_dir = self._gs.profile_dir()
        if profile_dir is None:
            self._notify(self.tr("No active profile."), "warning")
            return
        self._install_running = True
        self._op_is_restore = False
        self._op_title = "Installing"
        if hasattr(self, "_install_btn"):
            self._install_btn.setEnabled(False)
        self._ensure_feedback()
        # Process archives ONE AT A TIME so a FOMOD can pause for the wizard.
        self._install_queue = list(paths)
        self._install_total = len(paths)
        self._install_ok = []
        # Archives handed off to a detached FOMOD/BAIN wizard: they leave the
        # pipeline immediately and report their own outcome later, so they count
        # toward neither ok nor the failure tally (see _on_install_done summary).
        self._install_handoffs = 0
        self._install_game = game
        self._install_profile_dir = profile_dir
        self._install_metas = dict(metas or {})
        self._install_prev_name = previous_mod_name
        self._install_preferred = dict(preferred_names or {})
        self._install_all_done_cb = on_all_done
        self._install_clear_archives = clear_archives
        self._notify(self.tr("Installing {0} mod(s)…").format(len(paths)) if len(paths) > 1
                     else self.tr("Installing {0}…").format(Path(paths[0]).name), "info")
        # Multi-archive batch (Downloads tab multi-select, Nexus batches):
        # extract several archives at once like a collection install, deferring
        # FOMOD/BAIN wizards to a sequential phase at the end. Change Version
        # (previous_mod_name) is always a single archive; keep it sequential.
        if len(paths) > 1 and previous_mod_name is None:
            self._install_batch_parallel()
        else:
            self._install_next()

    def _make_need_prefix_cb(self):
        """Return an on_need_prefix(required, file_list, mod_name) callback for the
        install worker. It runs on the WORKER thread, so it asks the UI thread to
        show the Set-Prefix overlay (via _need_prefix) and BLOCKS on an Event
        until the user responds — mirroring the Tk non-main-thread prefix dialog.
        Returns the prefix str ("" = as-is), or None (cancel)."""

        def _cb(required, file_list, mod_name):
            holder = {"result": None}
            ev = threading.Event()
            self._need_prefix.emit({
                "required": required, "file_list": file_list,
                "mod_name": mod_name, "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_need_prefix_ui(self, payload):
        """UI thread: show the Set-Prefix overlay; unblock the worker when done.
        The progress popup is hidden while the user decides (no work running)."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.overlays.set_prefix_overlay import SetPrefixOverlay

        def _done(result):
            payload["holder"]["result"] = result
            payload["event"].set()

        SetPrefixOverlay.show_over(
            self, payload["mod_name"], payload["required"],
            payload["file_list"], _done)

    def _make_exists_cb(self):
        """Return an on_exists(mod_name, conflict) callback for finish_install.
        Runs on the WORKER thread → shows the Mod-Already-Exists overlay on the
        UI thread and BLOCKS until the user picks (replace / rename:<n> / cancel),
        mirroring _make_need_prefix_cb."""

        def _cb(mod_name, conflict=False):
            holder = {"result": "cancel"}
            ev = threading.Event()
            self._mod_exists.emit({
                "mod_name": mod_name, "conflict": bool(conflict),
                "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_mod_exists_ui(self, payload):
        """UI thread: show the Mod-Already-Exists overlay; unblock the worker."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.overlays.mod_exists_overlay import ModExistsOverlay

        def _done(result):
            payload["holder"]["result"] = result or "cancel"
            payload["event"].set()

        ModExistsOverlay.show_over(
            self, payload["mod_name"], payload["conflict"], _done)

    def _make_variant_choice_cb(self):
        """Return an on_variant_choice(existing_name, new_name) callback for
        prepare_archive. Runs on the WORKER thread → shows the Same-Mod-Page-
        Different-File overlay on the UI thread and BLOCKS until the user
        picks (replace / rename:<n> / cancel), mirroring _make_exists_cb.
        There is no reliable local signal that distinguishes a genuine
        hotfix/version-bump update (should merge into the existing folder)
        from an unrelated alternate file on the same Nexus mod page (should
        install separately — e.g. a Male/Female variant pair, real report),
        so this always asks rather than silently guessing either way."""

        def _cb(existing_name, new_name):
            holder = {"result": "cancel"}
            ev = threading.Event()
            self._mod_variant_choice.emit({
                "existing_name": existing_name, "new_name": new_name,
                "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_mod_variant_choice_ui(self, payload):
        """UI thread: show the Same-Mod-Page-Different-File overlay; unblock
        the worker."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.overlays.mod_exists_overlay import ModExistsOverlay

        def _done(result):
            payload["holder"]["result"] = result or "cancel"
            payload["event"].set()

        ModExistsOverlay.show_over(
            self, payload["new_name"], False, _done,
            variant_of=payload["existing_name"])

    def _make_confirm_cet_cb(self, game):
        """Return a confirm_cet() callback for run_deploy_pipeline. Runs on the
        deploy WORKER thread: if Cyberpunk 2077 is being deployed in symlink mode
        with CET staged, asks the UI thread to show the warning and BLOCKS on an
        Event until the user chooses. Returns True to proceed, False to cancel.
        No conflict → returns True immediately without prompting (Tk parity:
        gui.dialogs.confirm_cet_symlink)."""
        from Utils.modding_tools.cet_check import cet_symlink_conflict

        def _cb() -> bool:
            try:
                if not cet_symlink_conflict(game):
                    return True
            except Exception:
                return True
            holder = {"result": True}
            ev = threading.Event()
            self._confirm_cet.emit({"holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_confirm_cet_ui(self, payload):
        """UI thread: show the CET-requires-Hardlink warning; unblock the worker.
        The progress popup is cleared while the user decides (no work running)."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay

        def _done(ok):
            payload["holder"]["result"] = bool(ok)
            payload["event"].set()

        ConfirmOverlay.show_over(
            self,
            self.tr("Cyber Engine Tweaks requires Hardlink mode"),
            self.tr(
                "Cyber Engine Tweaks is enabled, but cyber_engine_tweaks.asi "
                "will be symlinked.\n\nCET will not load from a symlinked "
                "asi. This happens when the deploy mode is Symlink, or when it "
                "is Hardlink but the game folder and mod staging folder are on "
                "different drives (hardlinks fall back to symlinks across "
                "drives).\n\nUse Hardlink mode with both folders on the same "
                "drive for CET to work.\n\nDeploy anyway?"
            ),
            _done,
            confirm_label=self.tr("Deploy anyway"),
            cancel_label=self.tr("Cancel"),
            card_h=340,
        )

    def _install_next(self):
        """Pop the next queued archive: prepare it on a worker; the prepared
        result comes back on _prepared_ready (UI thread)."""
        if not self._install_queue:
            self._install_done.emit(len(self._install_ok), self._install_total,
                                    self._install_ok)
            return
        path = self._install_queue.pop(0)
        idx = self._install_total - len(self._install_queue)
        self._op_log.emit(f"Installing ({idx}/{self._install_total}): {Path(path).name}")


        meta = getattr(self, "_install_metas", {}).get(path)
        forced_name = getattr(self, "_install_preferred", {}).get(path, "")

        def worker():
            from Utils.mods.mod_install import prepare_archive
            try:
                prepared = prepare_archive(
                    path, self._install_game, self._install_profile_dir,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    prebuilt_meta=meta,
                    preferred_name=forced_name,
                    on_need_prefix=self._make_need_prefix_cb(),
                    on_variant_choice=self._make_variant_choice_cb())
            except Exception as exc:
                self._op_log.emit(f"Prepare error ({Path(path).name}): {exc}")
                prepared = None
            self._prepared_ready.emit(prepared)

        threading.Thread(target=worker, daemon=True).start()

    def _install_batch_parallel(self):
        """Phase 1 of a multi-archive install: prepare + install every
        NON-interactive archive on a small worker pool (collection-install
        style: max_extract_workers threads gated by the extraction memory
        budget). Archives that need a wizard (FOMOD with steps, BAIN) are
        deferred; _on_install_batch_stage_done then runs them one-by-one
        through the existing sequential wizard handshake. Blocking UI prompts
        (need-prefix, mod-already-exists) are serialised so only one overlay
        shows at a time."""

        paths = list(self._install_queue)
        self._install_queue = []
        total = self._install_total
        ui_lock = threading.Lock()
        raw_prefix_cb = self._make_need_prefix_cb()
        raw_exists_cb = self._make_exists_cb()
        raw_variant_cb = self._make_variant_choice_cb()

        def _serialized(cb):
            def inner(*a, **k):
                with ui_lock:
                    return cb(*a, **k)
            return inner

        prefix_cb = _serialized(raw_prefix_cb)
        exists_cb = _serialized(raw_exists_cb)
        variant_cb = _serialized(raw_variant_cb)

        def driver():
            from Utils.extract_budget import (
                ExtractionMemoryBudget,
                get_uncompressed_size,
            )
            from Utils.mods.mod_install import (
                _archive_lists_fomod_config,
                finish_install,
                prepare_archive,
            )
            try:
                from Utils.ui_config import load_collection_settings
                workers = int(load_collection_settings().get(
                    "max_extract_workers", 4))
            except Exception:
                workers = 4
            workers = max(1, min(workers, len(paths)))
            budget = ExtractionMemoryBudget(max_workers=workers)
            lock = threading.Lock()
            ok_names: list = []
            deferred: list = []
            counters = {"done": 0}

            def one(path):
                name_for_log = Path(path).name
                meta = self._install_metas.get(path)
                forced = self._install_preferred.get(path, "")
                est = 0
                acquired = False
                try:
                    self._op_log.emit(f"Installing: {name_for_log}")
                    # Archive listing already shows fomod/ModuleConfig.xml →
                    # defer NOW, skipping an extract that would be thrown away
                    # and repeated in the deferred phase (collection parity).
                    if _archive_lists_fomod_config(path):
                        self._op_log.emit(
                            f"FOMOD installer detected: {name_for_log} — "
                            "deferred to the end of the batch.")
                        with lock:
                            deferred.append(path)
                        return
                    est = get_uncompressed_size(path)
                    budget.acquire(est)
                    acquired = True
                    prepared = prepare_archive(
                        path, self._install_game, self._install_profile_dir,
                        log_fn=lambda m: self._op_log.emit(str(m)),
                        prebuilt_meta=meta, preferred_name=forced,
                        on_need_prefix=prefix_cb,
                        on_variant_choice=None if forced else variant_cb)
                    if prepared is None:
                        return
                    if (prepared.is_fomod() and prepared.fomod_has_steps()) \
                            or prepared.is_bain():
                        kind = "FOMOD" if prepared.is_fomod() else "BAIN"
                        self._op_log.emit(
                            f"{kind} installer detected: {prepared.mod_name} "
                            "— deferred to the end of the batch.")
                        prepared.cleanup()
                        with lock:
                            deferred.append(path)
                        return
                    if prepared.is_fomod():
                        # FOMOD with no install steps → nothing to choose;
                        # install headlessly (parity with _on_prepared_ready).
                        self._op_log.emit(
                            f"FOMOD has no install options: "
                            f"{prepared.mod_name} — installing required files.")
                    name = finish_install(
                        prepared, None,
                        log_fn=lambda m: self._op_log.emit(str(m)),
                        on_exists=None if forced else exists_cb)
                    if name:
                        self._maybe_clear_archive(prepared)
                        with lock:
                            ok_names.append(name)
                except Exception as exc:
                    self._op_log.emit(f"Install error ({name_for_log}): {exc}")
                finally:
                    if acquired:
                        budget.release(est)
                    with lock:
                        counters["done"] += 1
                        done = counters["done"]
                    self._op_progress.emit(done, total, "Installing")

            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(one, paths))
            self._install_batch_stage_done.emit(ok_names, deferred)

        threading.Thread(target=driver, daemon=True).start()

    def _on_install_batch_stage_done(self, names, deferred):
        """UI thread: the parallel phase of a batch install finished. Run the
        optional rename-after-install prompts for the phase-1 installs (one at
        a time, as the sequential path does), then push the deferred FOMOD/BAIN
        archives through the normal sequential queue — an empty deferred list
        goes straight to the batch summary via _install_next."""
        names = list(names or [])
        deferred = list(deferred or [])

        def _proceed():
            if deferred:
                self._op_log.emit(
                    f"Installing {len(deferred)} deferred FOMOD/BAIN mod(s)…")
            self._install_queue = deferred
            self._install_next()

        try:
            from Utils.ui_config import load_rename_mod_after_install
            rename_on = load_rename_mod_after_install()
        except Exception:
            rename_on = False
        if not rename_on:
            self._install_ok.extend(names)
            _proceed()
            return

        def _chain(i):
            if i >= len(names):
                _proceed()
                return

            def _named(final, _next=i + 1):
                self._install_ok.append(final)
                _chain(_next)

            self._maybe_prompt_rename(names[i], _named)

        _chain(0)

    def _on_prepared_ready(self, prepared):
        if prepared is None:
            self._one_install_done.emit(None)
            return
        if prepared.is_fomod() and not prepared.fomod_has_steps():
            # FOMOD with no install steps (only requiredInstallFiles /
            # conditionalFileInstalls) → nothing for the user to choose. Install
            # headlessly with default selections instead of opening the wizard
            # on an empty step list (which crashed with IndexError).
            self._op_log.emit(
                f"FOMOD has no install options: {prepared.mod_name} — "
                "installing required files.")
            self._run_finish_install(prepared, None)
        elif prepared.is_fomod() or prepared.is_bain():
            # Interactive install (FOMOD wizard / BAIN picker): the user may take
            # a long time choosing, but that wait does NO staging work. Hand the
            # prepared archive off to a DETACHED, uniquely-keyed wizard tab so
            # several can be open at once and the install pipeline is free to
            # continue with the next queued archive. The pipeline treats this
            # item as handed off (not counted here — the detached wizard reports
            # its own outcome). Staging on finish is serialised separately.
            self._open_wizard_install(prepared, from_queue=True)
            # Release the pipeline slot: this item is no longer pending. Pass the
            # handoff sentinel so _on_one_install_done just advances the queue
            # without a rename prompt or an ok-count (the wizard owns those).
            self._install_handoffs = getattr(self, "_install_handoffs", 0) + 1
            self._one_install_done.emit(_WIZARD_HANDOFF)
        else:
            self._run_finish_install(prepared, None)

    def _open_wizard_install(self, prepared, from_queue: bool):
        """Open a DETACHED FOMOD/BAIN wizard for *prepared* in its own tab.

        Unlike the old single-wizard flow, this does NOT hold the install
        pipeline: the tab is uniquely keyed (fomod_wizard_N / bain_picker_N) so
        multiple wizards coexist, and on finish the staging step runs through
        _run_staged_finish (serialised against installs/deploys — the shared
        game._active_profile_dir wrong-profile guard). Each wizard reports its
        own toast + modlist reload; there is no shared _fomod_done flag.

        *from_queue* is True when the archive came off the install queue (so its
        source archive should honour 'clear archive after install' just like the
        pipeline would); the flag is captured for the deferred staging call."""
        self._wizard_seq += 1
        seq = self._wizard_seq
        clear_archives = getattr(self, "_install_clear_archives", True)
        # Capture the Change-Version 'previous mod name' for THIS handoff (a
        # single-archive Change Version can be a FOMOD): the pipeline's shared
        # _install_prev_name is one-shot and would be cleared by later items, so
        # bind it to the wizard now. Consumed once in _on_wizard_finish_done.
        prev_name = getattr(self, "_install_prev_name", None) if from_queue else None
        if prev_name is not None:
            self._install_prev_name = None
        done = {"v": False}   # per-wizard double-fire guard (finish/cancel/close)
        # NB: the progress popup is intentionally NOT cleared here (unlike the old
        # blocking flow) — the install pipeline keeps running behind this detached
        # tab, so the popup still reflects that real ongoing work and clears itself
        # when the pipeline finishes.

        if prepared.is_fomod():
            tab_key = f"fomod_wizard_{seq}"
            from gui_qt.views.fomod_wizard_view import FomodWizardView

            def _finish(selections):
                if done["v"]:
                    return
                done["v"] = True
                self._tabs.close_tab(tab_key)
                self._stage_wizard_finish(prepared, selections, None,
                                          clear_archives, prev_name)

            def _cancel():
                if done["v"]:
                    return
                done["v"] = True
                self._tabs.close_tab(tab_key)
                self._op_log.emit(f"FOMOD install cancelled: {prepared.mod_name}")
                prepared.cleanup()
                self._notify(self.tr("Install cancelled: {0}").format(prepared.mod_name), "info")

            _sel_path = None
            _game_name = getattr(prepared.game, "name", "")
            if _game_name:
                from Utils.config_paths import get_fomod_selections_path
                _sel_path = get_fomod_selections_path(_game_name,
                                                      prepared.mod_name)
            _prof_sel_path = self._profile_fomod_selection_path(
                getattr(prepared, "profile_dir", None), prepared.mod_name)
            _inst, _act, _loose = prepared.fomod_context
            # prepared.fomod_context was read from plugins.txt/loadorder.txt on the
            # worker thread. Those files can lag the LIVE plugin panel: a mod
            # enabled in the model isn't persisted to plugins.txt until the next
            # save (e.g. Sort Plugins), so a FOMOD `state="Active"` dep wouldn't
            # match and its conditional option wouldn't auto-select until the user
            # sorted. Merge the panel's live enabled/all sets so the wizard sees
            # the true current state without needing a sort first.
            try:
                pm = getattr(self, "_plugin_model", None)
                if pm is not None:
                    live_all = pm.all_lower()
                    live_active = pm.enabled_lower()
                    _inst = set(_inst) | live_all
                    # A plugin present in the panel but not enabled is genuinely
                    # inactive — only add the live-enabled ones to active, and drop
                    # any stale disk-active entry the panel now shows as disabled.
                    _act = (set(_act) | live_active) - (live_all - live_active)
            except Exception:
                pass
            self._append_log(
                f"[fomod] {prepared.mod_name}: {len(_inst)} installed / "
                f"{len(_act)} active plugin(s) for condition eval")
            view = FomodWizardView(prepared.fomod_config, prepared.fomod_base,
                                   prepared.mod_name, on_finish=_finish,
                                   on_cancel=_cancel,
                                   saved_selections=prepared.saved_fomod_selections,
                                   selections_path=_sel_path,
                                   profile_selections_path=_prof_sel_path,
                                   installed_files=_inst, active_files=_act,
                                   loose_files=_loose)
            # Closing the tab (× / detached-window close) cancels the install.
            view.destroyed.connect(lambda *_: _cancel())
            self._tabs.open_tab(view, self.tr("Install: {0}").format(prepared.mod_name),
                                key=tab_key)
        else:   # BAIN
            tab_key = f"bain_picker_{seq}"
            from gui_qt.views.bain_picker_view import BainPickerView

            def _bfinish(result):
                if done["v"]:
                    return
                done["v"] = True
                self._tabs.close_tab(tab_key)
                if result is None:
                    self._op_log.emit(
                        f"BAIN install cancelled: {prepared.mod_name}")
                    prepared.cleanup()
                    self._notify(self.tr("Install cancelled: {0}").format(prepared.mod_name), "info")
                    return
                self._stage_wizard_finish(prepared, None, result,
                                          clear_archives, prev_name)

            view = BainPickerView(
                prepared.bain_subpkgs, prepared.bain_root, prepared.mod_name,
                on_done=_bfinish,
                readme_text=getattr(prepared, "readme_text", None),
                saved_selections=getattr(prepared, "saved_bain_selections", None))
            # Closing the tab (× / detached-window close) cancels the install.
            view.destroyed.connect(lambda *_: _bfinish(None))
            self._tabs.open_tab(view, self.tr("Install: {0}").format(prepared.mod_name),
                                key=tab_key)

    def _run_finish_install(self, prepared, selections, bain_selections=None):

        # Quick Update (forced folder name) auto-confirms Replace-All — pass
        # on_exists=None so finish_install silently replaces the existing folder
        # instead of raising the Mod-Already-Exists overlay (Tk parity).
        _forced = str(getattr(prepared, "archive", "")) in \
            getattr(self, "_install_preferred", {})
        exists_cb = None if _forced else self._make_exists_cb()

        def worker():
            from Utils.mods.mod_install import finish_install
            try:
                name = finish_install(
                    prepared, selections,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    on_exists=exists_cb,
                    bain_selections=bain_selections)
            except Exception as exc:
                self._op_log.emit(f"Install error ({prepared.mod_name}): {exc}")
                name = None
            if name:
                self._maybe_clear_archive(prepared)
            self._one_install_done.emit(name)

        threading.Thread(target=worker, daemon=True).start()

    # -- detached-wizard staging --------------------------------------------
    def _stage_wizard_finish(self, prepared, selections, bain_selections,
                             clear_archives, prev_name=None):
        """A detached FOMOD/BAIN wizard finished — enqueue its staging step.

        Staging (finish_install) mutates the shared game (get_effective_mod_
        staging_path reads the live active profile), so it MUST run one-at-a-time
        against installs AND deploys — the same wrong-profile guard _install_paths
        enforces. Wizards may finish in any order and while a pipeline install or
        a deploy is running, so we queue here and drain via _run_staged_finish.

        *prev_name* — a Change-Version 'previous mod name' captured for this
        wizard; if the install lands a differently-named mod, the remove-previous
        prompt fires from _on_wizard_finish_done."""
        self._staged_finish_queue.append({
            "prepared": prepared, "selections": selections,
            "bain_selections": bain_selections,
            "clear_archives": clear_archives, "prev_name": prev_name})
        self._notify(self.tr("Installing {0}…").format(prepared.mod_name), "info")
        self._run_staged_finish()

    def _run_staged_finish(self):
        """Drain one queued detached-wizard staging job if nothing else is
        touching the shared game. Re-armed by _on_wizard_finish_done, and also by
        _on_install_done / _on_op_done when a blocking install/deploy finishes."""

        if not self._staged_finish_queue \
                or self._staged_finish_running \
                or getattr(self, "_install_running", False) \
                or getattr(self, "_deploy_running", False) \
                or getattr(self, "_col_install_running", False):
            return
        job = self._staged_finish_queue.pop(0)
        self._staged_finish_running = True
        prepared = job["prepared"]
        selections = job["selections"]
        bain_selections = job["bain_selections"]
        prev_name = job.get("prev_name")
        # Carry the clear-archive policy captured when this wizard opened (the
        # pipeline's _install_clear_archives may have changed for a later batch).
        self._install_clear_archives = job["clear_archives"]
        # A just-finished worker may have left _active_profile_dir out of sync;
        # re-assert it so get_effective_mod_staging_path resolves to the profile
        # the dropdown shows (parity with _install_paths).
        self._gs.reassert_active_profile()
        self._ensure_feedback()

        _forced = str(getattr(prepared, "archive", "")) in \
            getattr(self, "_install_preferred", {})
        exists_cb = None if _forced else self._make_exists_cb()

        def worker():
            from Utils.mods.mod_install import finish_install
            try:
                name = finish_install(
                    prepared, selections,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    on_exists=exists_cb,
                    bain_selections=bain_selections)
            except Exception as exc:
                self._op_log.emit(f"Install error ({prepared.mod_name}): {exc}")
                name = None
            if name:
                self._maybe_clear_archive(prepared)
            self._wizard_finish_done.emit(name, prepared, prev_name)

        threading.Thread(target=worker, daemon=True).start()

    def _drain_pending_after_staged(self):
        """Release Deploy/Restore requests deferred behind an install, a
        collection install, or a detached-wizard staging job (all three
        mutate the shared game's active-profile pointer — see
        _install_paths for the full race explanation). Only safe once ALL
        of them are confirmed idle; any one still running holds the shared
        game object, and releasing early would re-open the exact
        wrong-profile race this queue exists to prevent. Called from every
        handler that clears one of those flags.

        _pending_install_batches also gates this: _on_install_done clears
        _install_running and calls here SYNCHRONOUSLY, but only DEFERS the
        next queued batch to the next event-loop tick (QTimer.singleShot).
        Without this check, a Deploy queued behind several near-simultaneous
        mod updates (e.g. updating multiple outdated mods) would release
        right after the FIRST update finishes — capturing on-disk state
        before the rest have installed — instead of waiting for the whole
        queue to drain."""
        if (getattr(self, "_install_running", False)
                or getattr(self, "_deploy_running", False)
                or getattr(self, "_staged_finish_running", False)
                or self._staged_finish_queue
                or getattr(self, "_col_install_running", False)
                or self._pending_install_batches):
            return
        pending, self._pending_after_staged = self._pending_after_staged, []
        for cb in pending:
            try:
                cb()
            except Exception as exc:
                self._append_log(f"[install] deferred action error: {exc}")

    def _on_wizard_finish_done(self, name, prepared, prev_name):
        """UI thread: a detached-wizard staging job finished. Report it (toast /
        optional rename prompt / remove-previous), reload, then drain the next
        queued wizard job."""
        self._staged_finish_running = False
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()

        def _drain_next():
            # Another wizard may have finished while this one staged, and an
            # install/deploy may have queued behind the staging lock.
            self._run_staged_finish()
            # Only release the deferred install/deploy/restore queues once the
            # staged-finish queue is FULLY drained AND idle — a still-running or
            # still-queued staging job holds the shared game, so releasing now
            # would re-open the wrong-profile race we just guarded.
            if not self._staged_finish_running and not self._staged_finish_queue:
                if self._pending_install_batches:
                    QTimer.singleShot(0, self._drain_pending_installs)
                self._drain_pending_after_staged()

        def _after_named(final):
            self._reload_modlist()
            # Plugins reload comes from _on_conflicts_ready after the rebuild —
            # an immediate reload prunes the fresh plugins.txt entry against the
            # stale filemap (see _on_install_done).
            if not getattr(self, "_reload_had_entries", False):
                self._reload_plugins()
            self._notify(self.tr("Installed {0}").format(final), "success")
            # Change Version landed a different-named version → offer to remove
            # the previous version (Tk parity with _finish_one_install).
            if prev_name and final != prev_name:
                self._maybe_prompt_remove_previous(prev_name, final)
            _drain_next()

        if name:
            self._maybe_prompt_rename(name, _after_named)
        else:
            self._reload_modlist()
            # See _after_named — only load directly when no rebuild is coming.
            if not getattr(self, "_reload_had_entries", False):
                self._reload_plugins()
            # Failed/cancelled staging (finish_install returned None): keep the
            # queue moving.
            _drain_next()

    def _maybe_clear_archive(self, prepared):
        """Delete the source archive after a successful install, honouring the
        'Clear archive after install' / 'Keep FOMOD archives' settings (Tk
        parity). Runs on the install worker thread; failures are non-fatal.
        Skipped for user-supplied archives (see _install_paths clear_archives)."""
        try:
            if not getattr(self, "_install_clear_archives", True):
                return
            from Utils.ui_config import (
                load_clear_archive_after_install,
                load_keep_fomod_archives,
            )
            if not load_clear_archive_after_install():
                return
            if prepared.is_fomod() and load_keep_fomod_archives():
                return
            archive = getattr(prepared, "archive", None)
            if archive is None:
                return
            from pathlib import Path as _P

            from Nexus.nexus_download import delete_archive_and_sidecar
            delete_archive_and_sidecar(_P(archive))
            self._op_log.emit(f"Removed archive: {_P(archive).name}")
        except Exception as exc:
            self._op_log.emit(f"Archive cleanup skipped: {exc}")

    def _on_one_install_done(self, name):
        if name is _WIZARD_HANDOFF:
            # Interactive archive handed off to a detached wizard tab — it owns
            # its own outcome (toast/rename/reload on finish). Just advance the
            # queue so the next archive installs while the wizard sits open.
            self._install_next()
            return
        if name:
            # Optional post-install rename prompt (Tk parity). Modal, before the
            # next queued install — keeps one dialog at a time.
            self._maybe_prompt_rename(name, self._finish_one_install)
        else:
            self._install_next()   # continue the queue

    def _finish_one_install(self, name: str):
        """Tail of _on_one_install_done, run after the optional rename prompt
        resolves (*name* is the final mod name)."""
        self._install_ok.append(name)
        # Change Version landed a different-named version → offer to remove
        # the previous version (Tk parity). One-shot per queue.
        prev = getattr(self, "_install_prev_name", None)
        if prev and name != prev:
            self._install_prev_name = None   # don't re-prompt for later items
            self._maybe_prompt_remove_previous(prev, name)
        self._install_next()   # continue the queue

    @staticmethod
    def _resolve_staging_dir_name(staging: Path, name: str) -> str | None:
        """Resolve *name* to an actual child directory of *staging*.

        Tries an exact match first, then falls back to a case-insensitive
        match. A modlist.txt entry (e.g. one carried over from a Mod
        Organizer 2 profile transferred from a case-insensitive Windows
        filesystem onto a case-sensitive Linux one) can differ in case from
        the real on-disk folder — an exact ``.is_dir()`` check alone would
        then silently fail to find a folder that does, in fact, exist.
        Returns the real on-disk name (which may differ in case from
        *name*), or None if no matching directory exists at all.
        """
        if (staging / name).is_dir():
            return name
        try:
            lname = name.casefold()
            for child in staging.iterdir():
                if child.is_dir() and child.name.casefold() == lname:
                    return child.name
        except OSError:
            pass
        return None

    def _maybe_prompt_remove_previous(self, old_name: str, new_name: str):
        """Show the borderless 'Remove previous version?' overlay if both the old
        and new mods exist. Remove → the new mod inherits the old one's modlist
        position + enabled state, then the old mod is removed.

        *old_name*/*new_name* are the modlist.txt entry names — used as-is for
        all modlist.txt reads/writes below. The on-disk existence check (and
        the folder actually deleted on "Remove") instead resolves through
        _resolve_staging_dir_name, so a case-only mismatch between the
        recorded name and the real folder (see its docstring) doesn't
        silently skip this prompt with no explanation.
        """
        staging = self._gs.staging_dir()
        if staging is None:
            return
        old_disk_name = self._resolve_staging_dir_name(staging, old_name)
        new_disk_name = self._resolve_staging_dir_name(staging, new_name)
        if old_disk_name is None or new_disk_name is None:
            self._append_log(
                "[install] remove-previous prompt skipped — "
                f"{'old' if old_disk_name is None else 'new'} mod folder "
                f"not found on disk ('{old_name if old_disk_name is None else new_name}').")
            return
        from gui_qt.overlays.remove_previous_overlay import RemovePreviousOverlay

        def _done(result):
            if result == "remove":
                self._remove_previous_version(old_name, new_name, old_disk_name)

        RemovePreviousOverlay.show_over(self, old_name, new_name, _done)

    def _remove_previous_version(self, old_name: str, new_name: str,
                                 old_disk_name: str | None = None):
        """New mod inherits old's modlist slot + enabled state; old is removed.

        *old_disk_name* is the real on-disk folder name for *old_name* (may
        differ only in case — see _resolve_staging_dir_name); falls back to
        *old_name* itself when not supplied, matching prior behaviour."""
        try:
            from Utils.mods.mod_remove import remove_mods
            from Utils.mods.modlist import read_modlist, write_modlist
            pdir = self._gs.profile_dir()
            game = self._gs.game
            if pdir is None or game is None:
                return
            ml = pdir / "modlist.txt"
            entries = read_modlist(ml)
            old_e = next((e for e in entries if e.name == old_name), None)
            new_e = next((e for e in entries if e.name == new_name), None)
            if new_e is not None:
                # New inherits old's enabled state + position, and the OLD entry
                # is dropped here (remove_mods intentionally does NOT touch
                # modlist.txt — it leaves the row for the caller to remove).
                if old_e is not None:
                    new_e.enabled = old_e.enabled
                # Pull the new entry out first, THEN locate old's slot in the
                # remaining list, drop old, and insert new there (so new lands
                # exactly where old was).
                rest = [e for e in entries if e.name != new_name]
                idx = next((i for i, e in enumerate(rest)
                            if e.name == old_name), 0)
                rest = [e for e in rest if e.name != old_name]
                rest.insert(min(idx, len(rest)), new_e)
                write_modlist(ml, rest)
            # Delete the old mod's files/plugins/index (NOT its modlist row —
            # already dropped above). Use the real on-disk folder name so a
            # case-only mismatch against the modlist.txt string doesn't leave
            # the actual folder behind undeleted.
            remove_mods(game, pdir, [old_disk_name or old_name],
                        log_fn=lambda m: self._append_log(f"[remove] {m}"))
        except Exception as exc:
            self._append_log(f"[install] remove-previous failed: {exc}")
        self._reload_modlist()
        self._rebuild_conflicts_async()

    def _maybe_prompt_rename(self, name: str, on_done):
        """If 'Rename mod after install' is on, prompt for a new name and rename
        the mod (staging folder + index + modlist entry). Calls ``on_done`` with
        the final name (unchanged if the user cancels or the rename fails)."""
        try:
            from Utils.ui_config import load_rename_mod_after_install
            if not load_rename_mod_after_install():
                on_done(name)
                return
        except Exception:
            on_done(name)
            return

        def _named(new):
            if new is None or not new.strip():
                on_done(name)
                return
            renamed = self._rename_mod_on_disk(name, new.strip())
            on_done(renamed or name)

        from gui_qt.overlays.text_input_overlay import TextInputOverlay
        extra_kw = {}
        overlay_holder: list = []
        if self._mod_has_nexus_id(name):
            def _do_fetch(_name=name):
                if overlay_holder:
                    self._fetch_nexus_name(_name, overlay_holder[0])
            extra_kw["extra_label"] = self.tr("Fetch name from Nexus")
            extra_kw["on_extra"] = _do_fetch
        overlay = TextInputOverlay.show_over(
            self, "Rename mod", "New name for the installed mod:", _named,
            initial=name, ok_label=self.tr("Rename"), **extra_kw)
        overlay_holder.append(overlay)

    def _rename_mod_on_disk(self, old_name: str, new_name: str) -> str | None:
        """Rename a mod: staging folder → new, modindex entry, modlist entry,
        then reload. Returns the sanitised new name on success, else None.
        Mirrors the Tk modlist_panel.rename_mod_by_name operation.

        If the target name is already taken, this shows the Mod-Already-Exists
        overlay (Replace All / Rename… / Cancel) rather than failing outright,
        and the rename completes asynchronously through that flow (so the return
        value is None — the folder is still under ``old_name`` at that point)."""
        from Utils.mods.mod_name_utils import sanitize_mod_folder_name
        new_name = sanitize_mod_folder_name(new_name)
        if not old_name or not new_name or old_name == new_name:
            return None
        staging = self._gs.staging_dir()
        if staging is None:
            return None
        new_folder = staging / new_name
        if new_folder.exists():
            # Collision → let the user Replace the existing mod, pick another
            # name, or cancel (same overlay the installer uses).
            from gui_qt.overlays.mod_exists_overlay import ModExistsOverlay

            def _resolved(result):
                if not result or result == "cancel":
                    return
                if result == "replace":
                    self._replace_then_rename(old_name, new_name)
                elif result.startswith("rename:"):
                    self._rename_mod_on_disk(old_name, result[len("rename:"):])

            ModExistsOverlay.show_over(self, new_name, False, _resolved)
            return None
        return self._do_rename_mod_on_disk(old_name, new_name)

    def _replace_then_rename(self, old_name: str, new_name: str) -> None:
        """Fully remove the existing mod occupying *new_name*, drop its modlist
        row, then rename *old_name* → *new_name* (used by the rename-collision
        Replace-All path)."""
        try:
            from Utils.mods.mod_remove import remove_mods
            remove_mods(self._gs.game, self._gs.profile_dir(), [new_name],
                        log_fn=self._append_log)
        except Exception as exc:
            self._notify(self.tr("Replace failed: {0}").format(exc), "warning")
            return
        # Drop the replaced mod's modlist row so it isn't left dangling. File-
        # level, not self._modlist_model — same stale-snapshot hazard as
        # _do_rename_mod_on_disk below (this whole flow runs before the
        # post-install reload for a fresh install), and remove_row's default
        # save=True would silently re-persist that stale state.
        try:
            from Utils.mods.modlist import _lock_for, read_modlist, write_modlist
            ml_path = self._gs.modlist_path()
            if ml_path is not None:
                with _lock_for(ml_path):
                    entries = read_modlist(ml_path)
                    entries = [e for e in entries
                              if e.is_separator or e.name != new_name]
                    write_modlist(ml_path, entries)
        except Exception as exc:
            print(f"[gui_qt] replace-then-rename modlist update failed: {exc}",
                  flush=True)
        self._do_rename_mod_on_disk(old_name, new_name)

    def _do_rename_mod_on_disk(self, old_name: str, new_name: str) -> str | None:
        """Perform the actual rename (staging folder + index + state + modlist),
        assuming *new_name* is free. Returns the new name on success."""
        staging = self._gs.staging_dir()
        if staging is None:
            return None
        old_folder = staging / old_name
        new_folder = staging / new_name
        try:
            if old_folder.is_dir():
                old_folder.rename(new_folder)
        except OSError as exc:
            self._notify(self.tr("Rename failed: {0}").format(exc), "warning")
            return None
        # Keep the persistent mod index in sync (avoids a full rescan).
        try:
            from Utils.filemap import rename_in_mod_index
            from Utils.ui_config import load_normalize_folder_case
            idx_path = staging.parent / "modindex.bin"
            rename_in_mod_index(idx_path, old_name, new_name,
                                normalize_folder_case=load_normalize_folder_case())
        except Exception:
            pass
        # Re-key the name-keyed per-mod state (strip prefixes, disabled plugins,
        # excluded files, notes) — Tk parity: _migrate_mod_name_state.
        try:
            from Utils.mods.mod_rename import migrate_mod_state
            migrate_mod_state(self._gs.profile_dir(), old_name, new_name,
                              log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] rename state migration failed: {exc}", flush=True)
        # Update the modlist entry by name, persist, and reload everything.
        # Read-modify-write the FILE directly rather than renaming the row in
        # self._modlist_model and calling m.save() — the rename prompt (this
        # whole flow) runs BEFORE _on_install_done's _reload_modlist() for a
        # single install (_finish_one_install, which the prompt gates, is what
        # continues the queue into _install_done), so the model is still
        # showing its PRE-install snapshot at this point. Renaming a row that
        # isn't there yet was a silent no-op, and m.save() then overwrote the
        # freshly-written modlist.txt with that stale snapshot — deleting the
        # just-installed mod's entry outright (root cause of mods vanishing
        # until a manual Refresh, but only when using this rename prompt).
        try:
            from Utils.mods.modlist import _lock_for, read_modlist, write_modlist
            ml_path = self._gs.modlist_path()
            if ml_path is not None:
                with _lock_for(ml_path):
                    entries = read_modlist(ml_path)
                    for e in entries:
                        if not e.is_separator and e.name == old_name:
                            e.name = new_name
                            break
                    write_modlist(ml_path, entries)
        except Exception as exc:
            print(f"[gui_qt] rename modlist update failed: {exc}", flush=True)
        self._reload_modlist()
        self._notify(self.tr("Renamed to '{0}'.").format(new_name), "info")
        return new_name

    def _on_install_done(self, ok: int, total: int, names):
        self._install_running = False
        # Drain any batch queued while this one ran (deferred so it starts
        # after this handler — and any on_all_done callback — completes).
        if self._pending_install_batches:
            QTimer.singleShot(0, self._drain_pending_installs)
        # A detached-wizard staging job may have finished (or been requested)
        # while this pipeline held the shared game — let it run now.
        if self._staged_finish_queue:
            QTimer.singleShot(0, self._run_staged_finish)
        # Release any Deploy/Restore deferred behind this install (no-op if
        # a collection install or staged-finish job is still holding the
        # shared game — see _drain_pending_after_staged).
        self._drain_pending_after_staged()
        if hasattr(self, "_install_btn"):
            self._install_btn.setEnabled(True)
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        self._reload_modlist()
        # NOTE: the plugin panel is reloaded from _on_conflicts_ready, after the
        # conflict/filemap rebuild queued by _reload_modlist — NOT here. An
        # immediate reload reads the STALE filemap (the just-installed mod's
        # plugin isn't in it yet), so load_plugins' phantom prune deleted the
        # plugins.txt entry _add_plugins had just appended; the plugin then
        # appeared in the panel only via filemap recovery, never persisted
        # (observed: SkyUI_SE.esp pruned within the same second as its install).
        # Only load directly when the modlist is empty and no rebuild is coming.
        if not getattr(self, "_reload_had_entries", False):
            self._reload_plugins()
        # Re-flag Reinstall in the Downloads tab now that meta.ini changed.
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        # A batch owner (Quick Update) reports its own summary; skip the generic
        # install toast and hand control back to it.
        cb = getattr(self, "_install_all_done_cb", None)
        self._install_all_done_cb = None
        if cb is not None:
            cb(ok, total, names)
            return
        # Archives handed off to a detached wizard aren't done yet — they report
        # their own toast on finish, so drop them from this batch's tally.
        handoffs = getattr(self, "_install_handoffs", 0)
        total = max(0, total - handoffs)
        if total == 0:
            # Nothing installed synchronously; every archive opened a wizard (or
            # there was nothing to summarise). Let the wizards speak for
            # themselves — no batch toast.
            return
        if ok == total and ok > 0:
            if ok == 1:
                self._notify(self.tr("Installed {0}").format(names[0]), "success")
            else:
                self._notify(self.tr("Installed {0} mods").format(ok), "success")
        elif ok > 0:
            self._notify(self.tr("Installed {0} of {1} mods — see log for failures.").format(ok, total),
                         "warning")
        else:
            self._notify(self.tr("Install failed — see log."), "error")

    def _drain_pending_installs(self):
        """Start the next install batch queued while an earlier install OR
        deploy OR a detached-wizard staging job ran. If a callback/coalesced
        deploy already started work in the meantime, leave the queue for the next
        _on_install_done / _on_op_done / _on_wizard_finish_done to drain (all
        call here)."""
        if not self._pending_install_batches \
                or getattr(self, "_install_running", False) \
                or getattr(self, "_deploy_running", False) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            return
        b = self._pending_install_batches.pop(0)
        self._install_paths(b["paths"], metas=b["metas"],
                            previous_mod_name=b["previous_mod_name"],
                            preferred_names=b["preferred_names"],
                            on_all_done=b["on_all_done"],
                            clear_archives=b.get("clear_archives", True))

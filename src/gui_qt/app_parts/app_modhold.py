"""Modhold: the browse tab and the account screen.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import threading


class MainWindow:

    def _modhold_game_or_warn(self):
        """The current game and its Modhold slug, or (None, "") after telling the
        user why not."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return None, ""
        slug = getattr(game, "modhold_game_slug", "") or ""
        if not slug:
            self._notify(self.tr("Modhold does not list '{0}' yet.").format(game.name),
                         "warning")
            return None, ""
        return game, slug

    def _open_modhold_browser_tab(self):
        """Open the Modhold mod browser for the current game as a detachable tab."""
        if self._tabs.has_key("modhold_browser"):
            self._tabs.focus_key("modhold_browser")
            return
        game, slug = self._modhold_game_or_warn()
        if game is None:
            return
        from gui_qt.modhold.modhold_browser_view import ModholdBrowserView
        view = ModholdBrowserView(game, slug,
                                  install_fn=self._install_paths,
                                  log_fn=self._append_log,
                                  progress_fn=self._nexus_download_progress,
                                  open_account_fn=self._open_modhold_account)
        self._modhold_view = view
        view.destroyed.connect(lambda *_: setattr(self, "_modhold_view", None))
        self._tabs.open_tab(view, self.tr("Modhold"), key="modhold_browser")

    def _open_modhold_account(self):
        """Open the Modhold account wizard (Connect / Disconnect)."""
        game, _slug = self._modhold_game_or_warn()
        if game is None:
            return
        from Utils.plugins.plugin_loader import get_all_wizard_tools
        tool = next((t for t in get_all_wizard_tools(game)
                     if t.id == "modhold_account"), None)
        if tool is None:
            self._notify(self.tr("The Modhold account screen is not available."), "warning")
            return
        self._open_wizard_tool(tool)

    # ---- updates -----------------------------------------------------------
    def _update_modhold_mods(self, mod_names):
        """Download and install the newest file for each Modhold-update-flagged mod.
        Off the UI thread: resolve, download and verify each one, then the whole
        batch goes through _install_paths with the folder name forced (a silent
        replace that keeps the modlist slot, like Nexus's Quick Update)."""
        if getattr(self, "_modhold_updating", False):
            self._notify(self.tr("A Modhold update is already running."), "info")
            return
        if getattr(self, "_install_running", False):
            self._notify(self.tr("An install is already in progress."), "warning")
            return
        game, slug = self._modhold_game_or_warn()
        if game is None:
            return
        from Modhold import modhold_key
        key = modhold_key.load_modhold_key()
        if not key:
            self._notify(self.tr("Connect your Modhold account first (Modhold ▸ Account…)."),
                         "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        targets = list(mod_names or [])
        if not targets:
            return

        from Modhold.modhold_api import ModholdAPI
        from Modhold.modhold_download import ModholdDownloader
        from Modhold.modhold_update import prepare_update
        from Utils.config_paths import get_download_cache_dir_for_game

        self._modhold_updating = True
        self._notify(self.tr("Updating {0} Modhold mod(s)…").format(len(targets)), "info")
        self._append_log(f"[modhold] updating {len(targets)} mod(s)…")
        dest = get_download_cache_dir_for_game(game.name)

        def _worker():
            queue, skipped = [], []
            try:
                api = ModholdAPI(key)
                dl = ModholdDownloader(api, dest)
                for nm in targets:
                    job, reason = prepare_update(api, dl, staging, nm, slug)
                    if job is None:
                        skipped.append((nm, reason))
                        self._op_log.emit(f"[modhold] {nm} — {reason}, skipped.")
                    else:
                        queue.append(job)
                        self._op_log.emit(f"[modhold] {nm} — downloaded {job.info.version}.")
            except Exception as e:
                self._op_log.emit(f"[modhold] update failed — {e}")
                skipped.extend((nm, str(e)) for nm in targets
                               if nm not in {j.mod_name for j in queue}
                               and nm not in {s[0] for s in skipped})
            self._modhold_update_ready.emit(queue, skipped)

        threading.Thread(target=_worker, daemon=True, name="modhold-update").start()

    def _on_modhold_update_ready(self, queue, skipped):
        """UI thread: the downloads finished. Install them over the old folders."""
        self._modhold_updating = False
        problems = len(skipped)
        if not queue:
            if problems:
                first_name, first_reason = skipped[0]
                self._notify(self.tr("Modhold: {0} — {1}").format(first_name, first_reason),
                             "warning")
            return
        from Modhold.browse_util import prebuilt_nexus_meta
        paths = [j.archive for j in queue]
        metas = {j.archive: prebuilt_nexus_meta(j.info) for j in queue}
        preferred = {j.archive: j.mod_name for j in queue}
        self._append_log(f"[modhold] installing {len(queue)} update(s)…")
        if problems:
            self._append_log(f"[modhold] {problems} mod(s) could not be updated: "
                             + "; ".join(f"{n} ({r})" for n, r in skipped))
        self._install_paths(paths, metas=metas, preferred_names=preferred)

    # ---- change version ----------------------------------------------------
    def _change_version_modhold(self, mod_name):
        """Pick another file of a Modhold mod and install it over the current one.
        Modhold lists a mod's current files (its main file, optional add-ons and any
        older releases the author kept), so this switches between those."""
        if getattr(self, "_modhold_updating", False):
            self._notify(self.tr("A Modhold update is already running."), "info")
            return
        game, _slug = self._modhold_game_or_warn()
        if game is None:
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        from Modhold.modhold_api import ModholdAPI
        from Modhold.modhold_meta import read_modhold_meta
        meta = read_modhold_meta(staging / mod_name / "meta.ini")
        if meta.mod_id <= 0:
            self._notify(self.tr("'{0}' isn't a Modhold mod.").format(mod_name), "warning")
            return
        self._notify(self.tr("Fetching the files of {0}…").format(mod_name), "info")

        def _worker():
            try:
                detail = ModholdAPI().get_mod(meta.mod_id)
                self._modhold_versions_ready.emit(mod_name, detail, "")
            except Exception as e:
                self._modhold_versions_ready.emit(mod_name, None, str(e))

        threading.Thread(target=_worker, daemon=True, name="modhold-versions").start()

    def _on_modhold_versions_ready(self, mod_name, detail, error):
        """UI thread: show the file picker, then download the chosen file."""
        if detail is None:
            self._notify(self.tr("Could not load the mod: {0}").format(error), "error")
            return
        files = [f for f in (detail.get("files") or []) if f.get("id")]
        if not files:
            self._notify(self.tr("{0} has no downloadable files.").format(mod_name), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            return
        from Modhold.modhold_meta import read_modhold_meta
        installed = read_modhold_meta(staging / mod_name / "meta.ini")
        from PySide6.QtWidgets import QDialog

        from gui_qt.modhold.modhold_file_chooser import ModholdFileChooser
        dlg = ModholdFileChooser(mod_name, files, self, single=True,
                                 installed_id=installed.file_id)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dlg.selected_files()
        if not chosen:
            return
        file = chosen[0]
        same = (int(file["id"]) == installed.file_id
                and (not installed.sha256
                     or str(file.get("sha256") or "").lower() == installed.sha256.lower()))
        if same:
            self._notify(self.tr("That file is already installed. Use Reinstall Mod to "
                                 "put it back."), "info")
            return
        if detail.get("can_download") is False:
            self._notify(self.tr("This account cannot download {0} (Pass, purchase or "
                                 "beta access).").format(mod_name), "warning")
            return
        self._install_modhold_file(mod_name, detail, file, installed.game, installed.loadout)

    def _install_modhold_file(self, mod_name, detail, file, slug, loadout):
        """Download one chosen file off the UI thread, then hand it to the same
        install step the updates use (silent replace of *mod_name*'s folder)."""
        from Modhold import modhold_key
        key = modhold_key.load_modhold_key()
        if not key:
            self._notify(self.tr("Connect your Modhold account first (Modhold ▸ Account…)."),
                         "warning")
            return
        if getattr(self, "_install_running", False):
            self._notify(self.tr("An install is already in progress."), "warning")
            return
        game = self._gs.game
        from Modhold.modhold_api import ModholdAPI
        from Modhold.modhold_download import ModholdDownloader
        from Modhold.modhold_update import prepare_file
        from Utils.config_paths import get_download_cache_dir_for_game

        self._modhold_updating = True
        self._notify(self.tr("Downloading {0}…").format(file.get("name") or mod_name), "info")
        dest = get_download_cache_dir_for_game(game.name)

        def _worker():
            queue, skipped = [], []
            try:
                dl = ModholdDownloader(ModholdAPI(key), dest)
                job, reason = prepare_file(dl, detail, file, slug or self._modhold_slug(),
                                           mod_name=mod_name, loadout=loadout)
                if job is None:
                    skipped.append((mod_name, reason))
                else:
                    queue.append(job)
            except Exception as e:
                skipped.append((mod_name, str(e)))
            self._modhold_update_ready.emit(queue, skipped)

        threading.Thread(target=_worker, daemon=True, name="modhold-change-version").start()

    def _modhold_slug(self):
        game = self._gs.game
        return getattr(game, "modhold_game_slug", "") or ""

    # ---- loadouts ----------------------------------------------------------
    def _open_modhold_loadouts_tab(self):
        """Open the Modhold loadouts list for the current game as a detachable tab."""
        if self._tabs.has_key("modhold_loadouts"):
            self._tabs.focus_key("modhold_loadouts")
            return
        game, slug = self._modhold_game_or_warn()
        if game is None:
            return
        from gui_qt.modhold.modhold_loadouts_view import ModholdLoadoutsView
        view = ModholdLoadoutsView(game, slug, install_fn=self._install_modhold_loadout,
                                   log_fn=self._append_log)
        self._tabs.open_tab(view, self.tr("Modhold loadouts"), key="modhold_loadouts")

    def _install_modhold_loadout(self, summary):
        """The user pressed Install on a loadout: fetch it in full, then ask what to include."""
        if getattr(self, "_col_install_running", False) or getattr(self, "_install_running", False):
            self._notify(self.tr("Another install is already running."), "warning")
            return
        game, _slug = self._modhold_game_or_warn()
        if game is None:
            return
        from Modhold import modhold_key
        key = modhold_key.load_modhold_key()
        if not key:
            self._notify(self.tr("Connect your Modhold account first (Modhold ▸ Account…)."),
                         "warning")
            return
        lid = int(summary.get("id") or 0)
        self._notify(self.tr("Fetching the loadout…"), "info")

        def _worker():
            try:
                from Modhold.loadout_manifest import parse_loadout
                from Modhold.modhold_api import ModholdAPI
                self._modhold_loadout_ready.emit(parse_loadout(ModholdAPI(key).get_loadout(lid)), "")
            except Exception as e:
                self._modhold_loadout_ready.emit(None, str(e))

        threading.Thread(target=_worker, daemon=True, name="modhold-loadout-fetch").start()

    def _modhold_installed_version(self, game) -> str:
        """The installed game version ("" when it can't be read), for loadout checks."""
        from Modhold.game_versions import installed_game_version
        return installed_game_version(game)

    def _on_modhold_loadout_ready(self, loadout, error):
        """UI thread: the full loadout arrived. Ask what to include and where to put it."""
        if loadout is None:
            self._notify(self.tr("Could not load the loadout: {0}").format(error), "error")
            return
        game, slug = self._modhold_game_or_warn()
        if game is None:
            return
        if loadout.game_slug and loadout.game_slug != slug:
            self._notify(self.tr("This loadout is for another game ({0}).").format(
                loadout.game_name or loadout.game_slug), "warning")
            return
        nexus_items = [i for i in loadout.items if i.nexus_ref is not None]
        if not nexus_items:
            self._show_modhold_preflight(loadout, None, "")
            return
        # Items hosted on Nexus can be downloaded for a Premium account: check that and
        # choose each mod's file off the UI thread, then show the preflight with the choices.
        api = self._ensure_nexus_api()
        if api is None:
            self._show_modhold_preflight(loadout, None, self.tr(
                "Log in to Nexus (Nexus ▸ Login) with a Premium account to download "
                "the Nexus items automatically."))
            return
        self._notify(self.tr("Checking the Nexus files for {0} item(s)…").format(len(nexus_items)),
                     "info")
        # Pick files made for the version the game runs; the loadout's own target when that
        # can't be read. Read on the UI thread (a small exe read), used by the worker below.
        wanted_version = self._modhold_installed_version(game) or loadout.game_version

        def _worker():
            from Modhold.nexus_links import resolve_nexus_picks
            from Utils.ui_config import (
                load_force_manual_install,
                load_nexus_last_premium,
                save_nexus_last_premium,
            )
            try:
                premium = bool(getattr(api.validate(), "is_premium", False))
                try:
                    save_nexus_last_premium(premium)
                except Exception:
                    pass
            except Exception as exc:
                # A hiccup must not demote a Premium account: use the last known status.
                premium = bool(load_nexus_last_premium())
                self._op_log.emit(f"[modhold] Nexus premium check failed: {exc} — "
                                  f"using the last known status")
            try:
                forced_manual = bool(load_force_manual_install())
            except Exception:
                forced_manual = False
            if not premium or forced_manual:
                self._modhold_nexus_ready.emit(loadout, None, self.tr(
                    "Nexus Premium is needed to download the Nexus items automatically, "
                    "so they are listed for you instead."))
                return
            try:
                picks = resolve_nexus_picks(
                    api, nexus_items, log=lambda m: self._op_log.emit(f"[modhold] {m}"),
                    game_version=wanted_version)
            except Exception as exc:
                self._op_log.emit(f"[modhold] could not choose the Nexus files: {exc}")
                self._modhold_nexus_ready.emit(loadout, None, self.tr(
                    "Could not check the Nexus files ({0}); they are listed for you instead."
                ).format(exc))
                return
            self._modhold_nexus_ready.emit(loadout, picks, "")

        threading.Thread(target=_worker, daemon=True, name="modhold-nexus-picks").start()

    def _on_modhold_nexus_ready(self, loadout, picks, note):
        """UI thread: the Nexus file choices are ready (or Nexus can't be used)."""
        self._show_modhold_preflight(loadout, picks, note)

    def _show_modhold_preflight(self, loadout, picks, note):
        """Ask what to include and where to put it, then start the install."""
        from PySide6.QtWidgets import QDialog

        from gui_qt.modhold.modhold_loadout_preflight import ModholdLoadoutPreflight
        from Modhold.game_versions import check as check_game_version
        from Utils.exe_launch.game_helpers import _profiles_for_game
        game = self._gs.game
        vcheck = check_game_version(self._modhold_installed_version(game),
                                    loadout.game_version, loadout.works_on)
        if vcheck.mismatch:
            self._op_log.emit(f"[modhold] loadout targets game version {vcheck.target}, "
                              f"installed is {vcheck.installed}")
        dlg = ModholdLoadoutPreflight(loadout, set(_profiles_for_game(game.name)), self,
                                      nexus_picks=picks, nexus_note=note,
                                      version_check=vcheck)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        plan = dlg.plan()
        if not plan.ordered():
            self._notify(self.tr("Nothing in this loadout can be downloaded automatically."),
                         "warning")
            self._show_modhold_external_items(loadout.name, plan.external)
            return
        self._start_modhold_loadout(loadout, plan, dlg.profile_name(), dlg.continue_existing(),
                                    nexus_picks=dlg.nexus_picks() if picks is not None else None)

    def _start_modhold_loadout(self, loadout, plan, profile_name, continue_existing,
                               nexus_picks=None):
        """UI thread: create (or reuse) the profile, show the shared progress overlay and
        run the installer on a worker. Everything after this (done / paused / cancelled /
        failed, the failure report, the profile switch) is the collection pipeline's own
        handling in _on_col_finished."""
        from pathlib import Path

        from Modhold import modhold_key
        from Modhold.loadout_install import run_loadout_install
        from Modhold.modhold_api import ModholdAPI
        from Modhold.modhold_download import ModholdDownloader
        from Utils.collections.collection_install import CollectionInstallControl
        from Utils.config_paths import get_download_cache_dir_for_game
        from Utils.exe_launch.game_helpers import _create_profile

        game = self._gs.game
        if continue_existing:
            profile_dir = game.get_profile_root() / "profiles" / profile_name
            if not profile_dir.is_dir():
                self._notify(self.tr("Profile '{0}' not found.").format(profile_name), "error")
                return
        else:
            try:
                profile_dir = Path(_create_profile(game.name, profile_name,
                                                   profile_specific_mods=True))
            except Exception as exc:
                self._notify(self.tr("Could not create profile: {0}").format(exc), "error")
                return

        self._col_install_running = True
        self._col_install_slug = f"modhold-loadout-{loadout.id}"
        self._col_created_profile = not continue_existing      # only a profile we made may be deleted
        self._col_profile_name = profile_dir.name
        self._col_offsite = []                                  # the Nexus reminder does not apply
        self._col_bundle_zip = ""
        # What the shared finish handler hands back to _modhold_after_install.
        self._col_loadout = {"loadout": loadout}
        old_profile_dir = getattr(game, "_active_profile_dir", None)

        control = CollectionInstallControl()
        self._col_install_control = control
        from gui_qt.collections.collection_install_overlay import (
            CollectionInstallOverlay,
        )
        from Utils.ui_config import load_download_speed_limit
        self._col_install_overlay = CollectionInstallOverlay.show_over(
            self, self.tr("Installing loadout: {0}").format(loadout.name),
            on_pause=self._on_col_pause_clicked, on_cancel=self._on_col_cancel_clicked,
            limit_mbps=load_download_speed_limit(), on_limit_change=self._on_col_limit_changed)
        callbacks = self._build_collection_callbacks()
        key = modhold_key.load_modhold_key()
        nexus_api = self._ensure_nexus_api() if (nexus_picks and plan.nexus) else None

        def _worker():
            try:
                cache_dir = get_download_cache_dir_for_game(game.name or "")
                downloader = ModholdDownloader(ModholdAPI(key), cache_dir)
                nexus_downloader = None
                if nexus_api is not None:
                    from Nexus.nexus_download import NexusDownloader
                    nexus_downloader = NexusDownloader(nexus_api, download_dir=cache_dir)
                run_loadout_install(
                    game=game, loadout=loadout, plan=plan, downloader=downloader,
                    profile_dir=profile_dir, callbacks=callbacks, control=control,
                    continue_existing=continue_existing, nexus_picks=nexus_picks,
                    nexus_api=nexus_api, nexus_downloader=nexus_downloader)
            except Exception as exc:
                import traceback
                self._op_log.emit(f"[modhold] loadout install error: {exc}\n"
                                  f"{traceback.format_exc()}")
                # An unexpected error is not a cancel: the profile and the download
                # cache are kept so Install -> Continue picks up where this stopped.
                self._col_finished.emit("failed", {"profile_dir": str(profile_dir),
                                                   "error": str(exc)})
            finally:
                _ = old_profile_dir

        threading.Thread(target=_worker, daemon=True, name="modhold-loadout-install").start()

    def _modhold_after_install(self, state, unresolved):
        """A loadout install just finished. *unresolved* is what could not be fetched,
        as ``(name, link)`` pairs: show it as a list of links the user can open."""
        from Modhold.loadout_install import items_for_unresolved
        loadout = state["loadout"]
        items = items_for_unresolved(loadout, unresolved)
        if items:
            self._show_modhold_external_items(loadout.name, items)

    def _show_modhold_external_items(self, loadout_name, items):
        """The items a loadout lists that Modhold doesn't host, with links (non-modal)."""
        if not items:
            return
        from gui_qt.modhold.modhold_external_items import ModholdExternalItems
        rows = [(i.name, i.link, i.note) for i in items]
        dlg = ModholdExternalItems(loadout_name, rows, self)
        self._modhold_external_dialog = dlg          # keep it alive; it is not modal
        dlg.show()

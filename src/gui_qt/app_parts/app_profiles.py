"""Profile actions: create/duplicate/remove, import/export, bundles, backups, copy-to-profile and settings.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QTimer


class MainWindow:

    def _update_profile_collection_tooltip(self):
        """Show the tracked collection + its installed revision on the profile
        selector, since a profile's own folder name is only ever stamped with
        a _RevN suffix at the moment it's first created -- Collection Update
        bumps profile_state.json's collection_revision_number correctly but
        never renames the (potentially long-since-stale) folder name to
        match, so the name alone can't be trusted to show the real revision."""
        game = self._gs.game
        pdir = getattr(game, "_active_profile_dir", None) if game is not None else None
        if pdir is None:
            self._profile_selector.setToolTip("")
            return
        from Utils.exe_launch.game_helpers import get_collection_url_from_profile
        from Utils.profile.profile_state import read_collection_revision
        url = get_collection_url_from_profile(pdir)
        if not url:
            self._profile_selector.setToolTip("")
            return
        revision = read_collection_revision(pdir)
        name = ""
        try:
            import json
            manifest_path = Path(pdir) / "collection.json"
            if manifest_path.is_file():
                name = (json.loads(manifest_path.read_text(encoding="utf-8"))
                        .get("info", {}).get("name", "")).strip()
        except Exception:
            pass
        label = name or self.tr("Nexus Collection")
        tip = (self.tr("Tracking: {0} — Rev {1}").format(label, revision)
               if revision is not None else self.tr("Tracking: {0}").format(label))
        self._profile_selector.setToolTip(tip)

    # ---- Import profile: local-bundle extraction -------------------------
    def _finish_import_bundle(self, bundle_zip, profile_name, ov,
                              installed, total, skipped_n):
        """Extract a local .mosaic's bundled mods + profile files into the
        just-installed profile on a worker, then reload on the UI thread."""
        game = self._gs.game
        if game is None:
            self._select_installed_collection_profile(profile_name)
            return
        profile_dir = game.get_profile_root() / "profiles" / profile_name
        if ov is not None:
            ov.set_status(self.tr("Restoring bundled mods + profile files…"))

        def _worker():
            staged = []
            try:
                # Resolve the imported profile's own mods/overwrite dirs (it's a
                # profile_specific_mods profile → <profile_dir>/mods + /overwrite).
                # Set it active first so get_effective_*_path resolves to it.
                prev = getattr(game, "_active_profile_dir", None)
                try:
                    game.set_active_profile_dir(profile_dir)
                    game.load_paths()
                    mods_dir = game.get_effective_mod_staging_path()
                    overwrite_dir = game.get_effective_overwrite_path()
                finally:
                    game.set_active_profile_dir(prev)
                    game.load_paths()
                from Utils.profile import profile_export
                staged = profile_export.install_local_bundle(
                    bundle_zip, profile_dir, mods_dir, overwrite_dir,
                    log_fn=lambda m: self._op_log.emit(str(m)))
            except Exception as exc:
                self._op_log.emit(f"[import] bundle extraction failed: {exc}")
            # Bundled mods carry no Nexus file ID, so the install pipeline never
            # counts them — each extracted bundle folder is an installed mod.
            self._col_import_done.emit(
                (profile_name, int(installed) + len(staged or ()),
                 int(total), int(skipped_n)))

        threading.Thread(target=_worker, daemon=True, name="import-bundle").start()

    def _on_import_bundle_done(self, payload):
        profile_name, installed, total, skipped_n = payload
        ov = self._col_install_overlay
        if ov is not None:
            ov.finish(self.tr("Imported — {0}/{1} installed.").format(installed, total))
            QTimer.singleShot(1500, self._dismiss_col_overlay)
        # Rebuild the mod index for the imported bundle mods, then reload.
        self._select_installed_collection_profile(profile_name, rescan_index=True)
        msg = self.tr("Profile imported — {0}/{1} mod(s)").format(installed, total)
        self._notify(msg + (self.tr(" ({0} skipped)").format(skipped_n) if skipped_n else ""),
                     "success")
        self._show_offsite_reminder()

    def _backup_profile_now(self):
        """Create a manual, timestamped profile backup with an optional note
        in one step — the lightweight alternative to Export profile (which
        bundles/reinstalls every mod) and Export code (clipboard-only, still
        re-resolves Nexus mods on import). See Utils.profile.profile_backup
        for exactly what a backup captures (modlist/plugins/profile state,
        not mod files)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        pname = self._gs.profile
        if not pname:
            self._notify(self.tr("No profile selected."), "warning")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname

        def _done(text):
            if text is None:
                return
            from Utils.profile.profile_backup import create_backup, set_backup_label
            try:
                bdir = create_backup(profile_dir, log_fn=self._append_log, manual=True)
            except Exception as exc:  # noqa: BLE001 — surface, don't crash
                self._notify(self.tr("Backup failed: {0}").format(exc), "warning")
                return
            note = (text or "").strip()
            if note:
                set_backup_label(bdir, note)
            self._notify(self.tr("Backup created."), "success")

        from gui_qt.overlays.text_input_overlay import TextInputOverlay
        TextInputOverlay.show_over(
            self,
            self.tr("Backup now"),
            self.tr("Optional note for this backup (leave blank to use the date)."),
            _done,
            ok_label=self.tr("Create backup"),
        )

    def _open_restore_backup_tab(self):
        """Open the Restore backup list for the current profile as a tab that
        takes over the whole plugins panel. Triggered by the footer button."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        pname = self._gs.profile
        if not pname:
            self._notify(self.tr("No profile selected."), "warning")
            return
        if self._tabs.has_key("restore_backup"):
            self._tabs.focus_key("restore_backup")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname
        from gui_qt.views.backup_restore_view import BackupRestoreView
        view = BackupRestoreView(
            profile_dir, pname,
            on_restored=self._on_backup_restored,
            on_close=self._close_restore_backup_tab,
            log_fn=self._append_log)
        self._tabs.open_scoped_tab(
            view, self.tr("Backups"), self._plugins_panel_stack,
            key="restore_backup")

    def _on_backup_restored(self):
        """A backup was restored — sync the modlist with the mods folder so any
        mods in staging but absent from the restored modlist.txt show up, then
        refresh both panels (backups cover modlist AND plugins)."""
        from Utils.mods.modlist import sync_modlist_with_mods_folder
        self._reassert_profile_paths()
        ml = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        if ml is not None and staging is not None:
            try:
                sync_modlist_with_mods_folder(ml, staging)
            except Exception as exc:  # noqa: BLE001
                print(f"[gui_qt] modlist sync failed: {exc}", flush=True)
        self._reload_modlist(rescan_index=True, preserve_overlays=True)
        self._reload_plugins()

    def _close_restore_backup_tab(self):
        if self._tabs.has_key("restore_backup"):
            self._tabs.close_tab("restore_backup")

    def _open_bundle_tab(self, mod_name: str):
        """Open the Bundle Options selector for the RE/Fluffy bundle *mod_name*
        as a full (detachable) tab, like the Nexus browser. Triggered by the
        bundle-flag click + the right-click 'Bundle options…' item. On Save the
        new selection is materialised, the mod re-indexed and the filemap
        rebuilt."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        from Utils.profile.re_bundle import BUNDLE_LIB_DIR, read_bundle_spec
        meta_path = staging / mod_name / "meta.ini"
        spec = read_bundle_spec(meta_path)
        if spec is None:
            self._notify(self.tr("'{0}' has no bundle configuration.").format(mod_name), "warning")
            return
        lib_dir = staging / mod_name / BUNDLE_LIB_DIR

        # Reuse one tab: rebuild it for the new mod if already open.
        if self._tabs.has_key("bundle_options"):
            self._tabs.close_tab("bundle_options")
        from gui_qt.views.bundle_options_view import BundleOptionsView
        view = BundleOptionsView(
            mod_name, spec, lib_dir,
            on_save=lambda s, m=mod_name, mp=meta_path:
                self._apply_bundle_selection(m, mp, s),
            on_close=self._close_bundle_tab,
            log_fn=self._append_log)
        self._bundle_options_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_bundle_options_view", None))
        self._tabs.open_tab(view, self.tr("Bundle: {0}").format(mod_name), key="bundle_options")

    def _apply_bundle_selection(self, mod_name, meta_path, new_spec):
        """Persist *new_spec*, re-materialise the bundle's selection, then close
        the tab and re-index + rebuild the filemap (Tk parity, gui/modlist_panel.
        _apply_bundle_selection). The rescan skips <mod>/.mm_bundle/ so only the
        materialised selection at the mod root is indexed/deployed."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        try:
            from Utils.profile.re_bundle import materialize_selection, write_bundle_spec
            write_bundle_spec(meta_path, new_spec)
            materialize_selection(staging / mod_name, new_spec)
        except Exception as exc:
            self._append_log(f"Bundle options: apply failed — {exc}")
            self._notify(self.tr("Could not update bundle: {0}").format(exc), "error")
            return
        if self._tabs.has_key("bundle_options"):
            self._tabs.close_tab("bundle_options")
        # Full re-index (skips .mm_bundle/) then filemap rebuild — the Qt
        # equivalent of Tk's rescan_mods_in_index([mod]) + _rebuild_filemap.
        self._rebuild_conflicts_async(rescan_index=True)
        self._notify(self.tr("Updated bundle: {0}").format(mod_name), "info")

    def _close_bundle_tab(self):
        """Close the Bundle Options overlay + refresh the modlist."""
        if self._tabs.has_key("bundle_options"):
            self._tabs.close_tab("bundle_options")
        self._reload_modlist()

    def _copy_mods_to_profile(self, names, enabled_map, target_profile, move):
        """Copy (or move) the given mods' staging folders into *target_profile*.
        Resolves collisions (single → Replace/Rename/Cancel overlay; multi → one
        Replace-or-skip prompt), copies on a worker thread, and — for a move —
        removes the sources here afterwards. Port of Tk _copy_mod(s)_to_profile."""
        from Utils.mods import mod_copy
        game = self._gs.game
        src_staging = self._gs.staging_dir()
        src_profile_dir = self._gs.profile_dir()
        if game is None or src_staging is None or src_profile_dir is None:
            return
        try:
            target_profile_dir = game.get_profile_root() / "profiles" / target_profile
            target_staging = mod_copy.resolve_target_staging(game, target_profile_dir)
        except Exception as exc:
            self._notify(self.tr("Could not resolve target profile: {0}").format(exc), "error")
            return
        names = [n for n in names if n]
        if not names:
            return
        existing = [n for n in names
                    if mod_copy.mod_exists_in_profile(target_staging, n)]

        # plan[name] = dest_name (rename) or None (copy as-is / replace after wipe)
        def _launch(plan, replace_set):
            self._run_copy_to_profile(
                names, enabled_map, plan, replace_set, move,
                src_staging, src_profile_dir, target_staging, target_profile_dir,
                target_profile, game)

        if not existing:
            _launch({n: None for n in names}, set())
            return

        if len(names) == 1 and existing:
            # Single mod already there → Replace / Rename / Cancel.
            from gui_qt.overlays.mod_exists_overlay import ModExistsOverlay
            nm = names[0]

            def _resolved(action, _nm=nm):
                if not action or action == "cancel":
                    self._notify(self.tr("Copy cancelled."), "info")
                    return
                if action == "replace":
                    _launch({_nm: None}, {_nm})
                elif action.startswith("rename:"):
                    new = action.split(":", 1)[1].strip()
                    if new:
                        _launch({_nm: new}, set())

            ModExistsOverlay.show_over(self, nm, False, _resolved)
        else:
            # Several already there → one Replace-or-skip prompt (Tk parity).
            from gui_qt.overlays.confirm_overlay import ConfirmOverlay

            def _resolved(replace):
                if replace:
                    _launch({n: None for n in names}, set(existing))
                else:
                    # Skip existing: copy only the non-colliding ones.
                    keep = [n for n in names if n not in existing]
                    if not keep:
                        self._notify(self.tr("All selected mods already exist there."), "info")
                        return
                    _launch({n: None for n in keep}, set())

            ConfirmOverlay.show_over(
                self, self.tr("Copy to profile"),
                self.tr("{0} of {1} mod(s) already exist in "
                        "'{2}'. Replace them? (Cancel skips those.)").format(
                            len(existing), len(names), target_profile),
                _resolved, confirm_label=self.tr("Replace"),
                cancel_label=self.tr("Skip"), danger=False)

    def _run_copy_to_profile(self, names, enabled_map, plan, replace_set, move,
                             src_staging, src_profile_dir, target_staging,
                             target_profile_dir, target_profile, game):
        """Worker: copy each planned mod, then (move) remove the sources."""
        from Utils.mods import mod_copy
        # Serialize: a second copy/move while one runs would write the same
        # target folders concurrently (install has the same guard).
        if getattr(self, "_copy_running", False):
            self._notify(self.tr("A copy/move is already in progress."), "info")
            return
        self._copy_running = True
        self._op_title = self.tr("Moving") if move else self.tr("Copying")
        self._ensure_feedback()
        self._notify(
            (self.tr("Moving {0} mod(s) to '{1}'…") if move
             else self.tr("Copying {0} mod(s) to '{1}'…"))
            .format(len(plan), target_profile), "info")
        total = len(plan)
        self._op_progress.emit(0, total, f"to '{target_profile}'")

        def _worker():
            copied = []
            # `plan` is ordered highest-priority-first (row 0 = top of the source
            # modlist). Register the whole copied block in ONE prepend after the
            # loop — prepending each mod individually would reverse the group.
            registered = []  # (dest_name, enabled), top-first
            for done, (nm, dest_name) in enumerate(plan.items()):
                self._op_progress.emit(done, total, nm)
                try:
                    if nm in replace_set:
                        import shutil
                        shutil.rmtree(target_staging / (dest_name or nm),
                                      ignore_errors=True)
                    out = mod_copy.copy_mod_to_profile(
                        Path(src_staging), Path(src_profile_dir),
                        Path(target_staging), Path(target_profile_dir),
                        nm, enabled_map.get(nm, True), dest_name=dest_name,
                        game=game, register=False)
                    if out:
                        copied.append(nm)
                        registered.append((out, enabled_map.get(nm, True)))
                except Exception as exc:
                    self._op_log.emit(f"Copy to profile failed for '{nm}': {exc}")
            if registered:
                try:
                    mod_copy.register_mods_in_modlist(
                        Path(target_profile_dir) / "modlist.txt", registered)
                except Exception as exc:
                    self._op_log.emit(f"Copy to profile: modlist update failed: {exc}")
            self._op_progress.emit(total, total, "finishing")
            removed = False
            if move and copied:
                try:
                    from Utils.mods.mod_remove import remove_mods
                    remove_mods(game, Path(src_profile_dir), copied,
                                log_fn=lambda m: self._op_log.emit(str(m)))
                    removed = True
                except Exception as exc:
                    self._op_log.emit(f"Move: could not remove sources: {exc}")
            self._copy_done.emit({"copied": len(copied), "total": len(plan),
                                  "move": move, "removed": removed,
                                  "target": target_profile,
                                  # names to drop from THIS profile's modlist
                                  # (remove_mods deliberately leaves modlist.txt
                                  # to the caller — mirror Tk _finish_copy_popup).
                                  "removed_names": list(copied) if removed else []})

        threading.Thread(target=_worker, daemon=True, name="copy-to-profile").start()

    def _copy_separators_to_profile(self, sep_names, sep_rows, target_profile, move):
        """Copy (or move) the given separators — marker only (name, color,
        lock) — into *target_profile*. Synchronous: separators are small
        JSON/text writes, no worker thread or collision overlay needed;
        colliding names are skipped (dedup by name)."""
        from Utils.mods.modlist import _SEPARATOR_SUFFIX
        from Utils.profile import separator_copy
        game = self._gs.game
        src_profile_dir = self._gs.profile_dir()
        if game is None or src_profile_dir is None:
            return
        model = self._modlist_model
        sep_names = [n for n in sep_names if n]
        if not sep_names:
            return
        payload = []
        for name in sep_names:
            display = (name[:-len(_SEPARATOR_SUFFIX)]
                      if name.endswith(_SEPARATOR_SUFFIX) else name)
            payload.append({"name": name,
                            "color": model.sep_color(name),
                            "locked": model.is_sep_locked(display)})
        try:
            target_profile_dir = game.get_profile_root() / "profiles" / target_profile
            target_modlist = target_profile_dir / "modlist.txt"
        except Exception as exc:
            self._notify(self.tr("Could not resolve target profile: {0}").format(exc), "error")
            return
        added = separator_copy.register_separators_in_modlist(
            target_modlist, target_profile_dir, payload)
        skipped = len(payload) - len(added)
        if move and added:
            from gui_qt.modlist.modlist_menu import _remove_separator_rows
            rows = [r for r in sep_rows
                    if (e := model.entry(r)) is not None
                    and e.is_separator and e.name in added]
            removed = _remove_separator_rows(self._modlist_view, model, rows)
            if removed:
                self._on_separators_removed(removed)
        if added:
            msg = ((self.tr("Moved {0} separator(s) to '{1}'.") if move
                   else self.tr("Copied {0} separator(s) to '{1}'."))
                  .format(len(added), target_profile))
            if skipped:
                msg += " " + self.tr("({0} already existed there and were skipped.)").format(skipped)
            self._notify(msg, "success")
        else:
            self._notify(self.tr("All selected separator(s) already exist in '{0}'.")
                         .format(target_profile), "info")

    @staticmethod
    def _profile_fomod_selection_path(profile_dir, mod_name):
        """``<profile>/fomod/<mod>.json`` — the profile-scoped mirror of a saved
        FOMOD selection. Returned whether or not it exists; the wizard's Reset
        deletes it alongside the global copy, because
        _read_saved_fomod_selections falls back to the mirror and a
        global-only delete lets a cleared selection silently come back."""
        if not profile_dir or not mod_name:
            return None
        try:
            return Path(profile_dir) / "fomod" / f"{mod_name}.json"
        except (TypeError, ValueError):
            return None

    def _on_profile_action(self, which):
        if which == "add":
            if self._gs.game is None:
                self._append_log("[profile] no game selected.")
                return
            self._new_profile_bar.open_for()
        elif which == "duplicate":
            self._duplicate_current_profile()
        elif which == "remove":
            self._remove_current_profile()
        elif which == "settings":
            self._open_profile_settings_tab()
        elif which == "backup_now":
            self._backup_profile_now()
        elif which == "backups":
            self._open_restore_backup_tab()
        elif which == "export":
            self._open_export_profile_tab()
        elif which == "create_collection":
            self._open_create_collection_tab()
        elif which == "import":
            self._import_profile()
        elif which == "export_code":
            self._export_profile_code()
        elif which == "import_code":
            self._import_profile_code()
        else:
            self._append_log(f"[profile] {which} (not wired yet)")

    def _open_export_profile_tab(self):
        """Open the Export Profile panel scoped over the MODLIST panel (like Profile
        Settings): per-mod source/version/optional config + Export to a .mosaic."""
        if self._gs.game_name is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._tabs.has_key("export_profile"):
            self._tabs.focus_key("export_profile")
            return
        api = self._ensure_nexus_api()   # optional — version/size fetch needs it
        from gui_qt.views.export_profile_view import ExportProfileView
        view = ExportProfileView(self, game, api, log_fn=self._append_log)
        self._export_profile_view = view
        self._tabs.open_scoped_tab(
            view, self.tr("Export Profile"), self._modlist_panel_stack,
            key="export_profile")

    def _import_profile(self):
        """Import a .mosaic / manifest: parse it, then reuse the collection detail
        + install pipeline (via CollectionDetailView with a local manifest) to build
        a new profile from it. Requires a configured game matching the manifest."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return
        # Native picker via the XDG portal (portal_filechooser). The callback fires
        # on a WORKER thread — QTimer.singleShot(0, …) from there never fires (no
        # event loop on that thread), so marshal to the GUI thread with a Signal
        # (auto-queued to the receiver's thread).
        from Utils.wine_proton.portal_filechooser import pick_file
        pick_file(
            "Import profile",
            lambda p: self._import_file_picked.emit(p),
            filters=[("Mosaic Manifest (*.mosaic *.amethyst *.zip *.7z *.json)",
                      ["*.mosaic", "*.amethyst", "*.zip", "*.7z", "*.json"]),
                     ("All files", ["*"])])

    def _on_import_file_picked(self, picked):
        """GUI thread: continue the import once a file was chosen in the portal."""
        if not picked:
            return
        path = str(picked)
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return
        from Utils.profile import profile_export
        try:
            manifest = profile_export.read_manifest(path)
        except Exception as exc:
            self._notify(self.tr("Could not read manifest: {0}").format(exc), "error")
            return
        if not isinstance(manifest, dict) or not manifest.get("mods"):
            self._notify(self.tr("That file doesn't look like a Mosaic manifest."),
                         "warning")
            return
        bundle_zip = path if Path(path).suffix.lower() in (".mosaic", ".amethyst", ".zip") else ""
        self._open_manifest_import(manifest, Path(path).stem, bundle_zip=bundle_zip)

    # ---- Share code: export / import a modlist as a text string -----------
    def _export_profile_code(self):
        """Build a compact share code for the active profile (mods with a
        modId + fileId, FOMOD/BAIN choices, load order, enabled state,
        separators) and show it with a Copy-to-clipboard overlay. The build
        runs on a worker thread (modlist + sidecar reads)."""
        if self._gs.game_name is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # No API call is needed to build the code (file sizes come from each
        # mod's meta.ini, stamped at install time) — but the modlist read +
        # FOMOD/BAIN sidecar reads still run off the UI thread.
        import threading
        threading.Thread(
            target=self._export_code_worker, args=(game,),
            daemon=True, name="export-code").start()

    def _export_code_worker(self, game):
        try:
            from Utils.mods.modlist import read_modlist
            from Utils.profile import profile_export
            pd = getattr(game, "_active_profile_dir", None)
            modlist_path = (Path(pd) / "modlist.txt") if pd else None
            if not modlist_path or not modlist_path.is_file():
                self._export_code_ready.emit(None, 0)
                return
            # read_modlist returns index 0 = highest priority (top of modlist);
            # build_code_manifest expects exactly that order. Separators are
            # passed through — the manifest carries them as modlistSeparators.
            entries = read_modlist(modlist_path)
            try:
                from version import __version__ as app_version
            except Exception:
                app_version = ""
            profile_name = Path(pd).name if pd else None
            manifest = profile_export.build_code_manifest(
                entries, game, app_version, profile_name=profile_name)
            mods = manifest.get("mods") or []
            if not mods:
                self._export_code_ready.emit("", 0)
                return
            code = profile_export.encode_manifest(manifest)
            self._export_code_ready.emit(code, len(mods))
        except Exception as exc:
            self._append_log(f"[export-code] failed: {exc}")
            self._export_code_ready.emit(None, -1)

    def _on_export_code_ready(self, code, mod_count):
        if code is None:
            if mod_count == -1:
                self._notify(self.tr("Could not build share code."), "error")
            else:
                self._notify(self.tr("No active profile to export."), "warning")
            return
        if not code:
            self._notify(
                self.tr("No mods with a Nexus mod + file ID to share."), "warning")
            return
        from gui_qt.overlays.share_code_overlay import ShareCodeExportOverlay
        ShareCodeExportOverlay(self.window(), code, mod_count)
        self._append_log(f"[export-code] built code for {mod_count} mod(s).")

    def _import_profile_code(self):
        """Prompt for a pasted share code, decode it, then reuse the manifest
        import pipeline to build a new profile from it."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._ensure_nexus_api() is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return

        def _got(text):
            if not text:
                return
            from Utils.profile import profile_export
            try:
                manifest = profile_export.decode_manifest(text)
            except Exception as exc:
                self._notify(self.tr("Could not read code: {0}").format(exc), "error")
                return
            # Name the imported profile "<source profile> - Code". The source name
            # is carried in the manifest's info.name; overwrite it with the "- Code"
            # form so both the collection/profile name and the import tab title use
            # it (build_code_manifest stores the bare name, so re-exporting an
            # imported profile won't stack "- Code - Code").
            info = manifest.setdefault("info", {})
            src_name = (info.get("name") or "Imported").strip()
            info["name"] = f"{src_name} - Code"
            # A code has no bundle, so appending into an existing profile is safe.
            self._open_manifest_import(
                manifest, f"{src_name}_code", bundle_zip="", allow_append=True)

        from gui_qt.overlays.share_code_overlay import ShareCodeImportOverlay
        ShareCodeImportOverlay(self.window(), _got)

    def _open_profile_settings_tab(self):
        """Open the Profile Settings panel scoped over the MODLIST panel (like the
        Settings gear / image preview): profile rows with lock / rename / open /
        remove, while the plugins panel + the rest of the UI stay live."""
        if self._gs.game_name is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        if self._tabs.has_key("profile_settings"):
            self._tabs.focus_key("profile_settings")
            return
        view = self._make_profile_settings_view()
        self._profile_settings_view = view
        self._tabs.open_scoped_tab(
            view, self.tr("Profile Settings"), self._modlist_panel_stack,
            key="profile_settings")

    def _make_profile_settings_view(self):
        """Build a ProfileSettingsView wired to the app's selector/reload
        callbacks. Used both by the scoped tab and the dropdown's Remove action."""
        from gui_qt.views.profile_settings_view import ProfileSettingsView
        return ProfileSettingsView(
            self,
            game_name=self._gs.game_name,
            current_profile=self._gs.profile,
            on_profile_renamed=self._on_profile_renamed,
            on_profile_removed=self._on_profile_removed,
            on_profiles_changed=self._on_profiles_lock_changed,
            log_fn=self._append_log,
        )

    def _current_profile_locked(self) -> bool:
        """True when the active profile is locked (or the original default) and so
        must not be removable from the dropdown. Mirrors the Profile Settings row
        gate without opening the tab."""
        if self._gs.game_name is None or self._gs.profile is None:
            return True
        try:
            view = self._make_profile_settings_view()
            return view._is_profile_locked(self._gs.profile)
        except Exception:
            return True

    def _duplicate_current_profile(self):
        """Copy the active profile under a new name, sharing its mods.

        The copy gets the load order, the enabled set and the settings, so a
        fixed setup (a Wabbajack modlist, a collection) can be branched and
        experimented with while the original stays as it was. The mods
        themselves are shared, not copied -- duplicating "Fashionista - Steam"
        would otherwise mean another 71 GB.
        """
        if self._gs.game_name is None or self._gs.profile is None:
            self._notify(self.tr("No profile selected."), "warning")
            return
        from Utils.exe_launch.game_helpers import _profiles_for_game
        source = self._gs.profile
        existing = set(_profiles_for_game(self._gs.game_name))
        suggestion = self.tr("Copy of {0}").format(source)
        if suggestion in existing:
            n = 2
            while f"{suggestion} {n}" in existing:
                n += 1
            suggestion = f"{suggestion} {n}"

        from gui_qt.overlays.text_input_overlay import TextInputOverlay
        TextInputOverlay(
            self, self.tr("Duplicate profile"),
            self.tr("Name for the copy of '{0}':").format(source),
            lambda name: self._do_duplicate_profile(source, name),
            initial=suggestion, ok_label=self.tr("Duplicate"))

    def _do_duplicate_profile(self, source: str, new_name) -> None:
        if not new_name:
            return                      # cancelled
        new_name = str(new_name).strip()
        from Utils.exe_launch.game_helpers import _profiles_for_game, duplicate_profile
        if not new_name or new_name != Path(new_name).name or new_name in (".", ".."):
            self._notify(self.tr("That name can't be used for a profile."), "warning")
            return
        if new_name in set(_profiles_for_game(self._gs.game_name)):
            self._notify(
                self.tr("A profile named '{0}' already exists.").format(new_name),
                "warning")
            return
        try:
            duplicate_profile(self._gs.game_name, source, new_name)
        except Exception as exc:
            self._notify(self.tr("Could not duplicate the profile: {0}").format(exc),
                         "error")
            return
        self._append_log(f"[profile] duplicated '{source}' as '{new_name}'")
        self._notify(self.tr("Duplicated '{0}' as '{1}'.").format(source, new_name), "ok")
        self._select_installed_collection_profile(new_name)

    def _remove_current_profile(self):
        """Remove the active profile via the same flow as Profile Settings'
        Remove button (confirm overlays, restore-if-deployed, delete, selector
        reload). Kept alive on self until the async remove worker finishes."""
        if self._gs.game_name is None or self._gs.profile is None:
            self._notify(self.tr("No profile selected."), "warning")
            return
        profile = self._gs.profile
        view = self._make_profile_settings_view()
        if view._is_profile_locked(profile):
            return
        # Retain a reference — the remove runs on a worker thread and emits back
        # into the (parentless) view; without this it could be collected mid-op.
        self._profile_remove_helper = view
        view._on_remove(profile)

    # -- Profile Settings callbacks (view → app: refresh selector + reload) --
    def _on_profiles_lock_changed(self):
        """A profile's lock toggled — the active profile is unchanged, so just
        refresh the selector list (Remove-eligibility, etc.)."""
        self._profile_selector.set_items(self._gs.profiles(),
                                         current=self._gs.profile)
        # Lock state feeds the "Remove current profile…" gate — rebuild the
        # pinned actions so unlocking the active profile reveals Remove (and
        # locking it hides Remove) without reopening the dropdown.
        self._refresh_profile_actions()

    def _on_profile_renamed(self, old: str, new: str):
        profs = self._gs.profiles()
        if self._gs.profile == old:
            # The active profile was renamed → switch GameState + reload.
            self._gs.set_profile(new)
            self._profile_selector.set_items(profs, current=new)
            self._reload_modlist()
            self._reload_plugins()
        else:
            self._profile_selector.set_items(profs, current=self._gs.profile)
        # The active profile (or its name) may have changed — rebuild the pinned
        # actions so Remove-eligibility tracks the current profile.
        self._refresh_profile_actions()
        self._update_deployed_profile_highlight()

    def _on_profile_removed(self, name: str):
        profs = self._gs.profiles()
        if self._gs.profile == name:
            # The active profile was removed → fall back like the view did.
            target = "default" if "default" in profs else (
                profs[0] if profs else "default")
            self._gs.set_profile(target)
            self._profile_selector.set_items(profs, current=target)
            self._reload_modlist()
            self._reload_plugins()
        else:
            self._profile_selector.set_items(profs, current=self._gs.profile)
        # Fall-back to default (or any other switch) changes Remove-eligibility —
        # rebuild the pinned actions so Remove hides on the locked default.
        self._refresh_profile_actions()
        self._update_deployed_profile_highlight()

    def _on_new_profile_create(self, name: str, profile_specific_mods: bool):
        """Create a new profile for the active game and switch to it. Mirrors the
        Tk top_bar._on_add_profile flow: reject an existing name, create via the
        neutral _create_profile, repopulate the selector, select + reload."""
        from Utils.exe_launch.game_helpers import _create_profile, _profiles_for_game
        game_name = self._gs.game_name
        if not game_name:
            return
        existing = _profiles_for_game(game_name)
        if name in existing:
            self._notify(self.tr("Profile '{0}' already exists.").format(name), "error")
            # Re-open so the user can pick another name (fields reset).
            self._new_profile_bar.open_for()
            return
        try:
            _create_profile(game_name, name,
                            profile_specific_mods=profile_specific_mods)
        except Exception as exc:
            self._append_log(f"[profile] create failed: {exc}")
            self._notify(self.tr("Could not create profile: {0}").format(exc), "error")
            return
        # Success — make sure the bar is closed (the widget hides itself before
        # calling us, but close explicitly so the flow is robust to any caller).
        self._new_profile_bar.close()
        self._append_log(f"[profile] created '{name}'"
                         + (" (profile-specific mods)" if profile_specific_mods
                            else ""))
        # Refresh the profile selector, select the new profile, and load it.
        profs = self._gs.profiles()
        self._profile_selector.set_items(profs, current=name)
        self._gs.set_profile(name)
        self._profile_selector.set_current(name)
        # Rebuild the pinned actions so "Remove current profile…" appears now
        # that the active profile is a fresh (unlocked, removable) one.
        self._refresh_profile_actions()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()
        self._notify(self.tr("Profile '{0}' created").format(name), "info")

    def _profile_actions(self):
        """Build the pinned action entries for the profile selector. An "Open"
        submenu is prepended only when the current game exposes profile-specific
        INI files and/or Saves *and* the matching option is toggled on."""
        open_items = []
        game = getattr(getattr(self, "_gs", None), "game", None)
        if game is not None and getattr(game, "is_configured", lambda: False)():
            if getattr(game, "profile_ini_files", False) \
                    and hasattr(game, "_profile_ini_dir"):
                open_items.append(
                    (self.tr("Profile INI files folder"),
                     lambda: self._open_profile_dir("ini")))
            if getattr(game, "profile_saves", False) \
                    and hasattr(game, "_profile_saves_dir"):
                open_items.append(
                    (self.tr("Profile saves folder"),
                     lambda: self._open_profile_dir("saves")))
        actions = []
        # Add new profile always comes first.
        add_opts = {"separator_after": True}
        # Remove current profile — hidden for locked (and default) profiles, so
        # the "Add" entry keeps the trailing separator when Remove is absent.
        can_remove = (self._gs.profile is not None
                      and not self._current_profile_locked())
        if can_remove:
            add_opts = {}
        actions.append(
            (self.tr("Add new profile…"), lambda: self._on_profile_action("add"),
             {} if self._gs.profile is not None else add_opts))
        if self._gs.profile is not None:
            actions.append(
                (self.tr("Duplicate current profile…"),
                 lambda: self._on_profile_action("duplicate"),
                 {} if can_remove else {"separator_after": True}))
        if can_remove:
            actions.append(
                (self.tr("Remove current profile…"),
                 lambda: self._on_profile_action("remove"),
                 {"separator_after": True}))
        if open_items:
            actions.append((self.tr("Open"), open_items))
        quick = self._quick_configure_submenu(game)
        if quick:
            actions.append((self.tr("Quick configure"), quick))
        # Profile settings, then the export/import group (profile + code together).
        actions.extend([
            (self.tr("Profile settings…"), lambda: self._on_profile_action("settings"),
             {"separator_after": True}),
            (self.tr("Backup now…"), lambda: self._on_profile_action("backup_now")),
            (self.tr("Backups…"), lambda: self._on_profile_action("backups"),
             {"separator_after": True}),
            (self.tr("Export profile…"), lambda: self._on_profile_action("export")),
            (self.tr("Import profile…"), lambda: self._on_profile_action("import")),
            (self.tr("Export code…"), lambda: self._on_profile_action("export_code")),
            (self.tr("Import code…"), lambda: self._on_profile_action("import_code")),
            (self.tr("Create/Publish Collection…"),
             lambda: self._on_profile_action("create_collection")),
        ])
        return actions

    def _refresh_profile_actions(self):
        """Rebuild the profile selector's pinned actions so the Open submenu
        appears/disappears with the current game's profile-specific settings."""
        sel = getattr(self, "_profile_selector", None)
        if sel is not None:
            sel.set_actions(self._profile_actions())

    def _open_profile_dir(self, which: str):
        """Open the active profile's INI files or Saves folder."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # Keep the game's active profile dir in sync with the dropdown so the
        # folder resolves against the profile actually shown (a background worker
        # may have moved it).
        self._gs.reassert_active_profile()
        profile = self._gs.profile
        if which == "ini":
            getter = getattr(game, "_profile_ini_dir", None)
            descr = "profile INI files folder"
        elif which == "saves":
            getter = getattr(game, "_profile_saves_dir", None)
            descr = "profile saves folder"
        else:
            return
        path = getter(profile) if callable(getter) else None
        # The folder is created on demand when the setting is toggled on, but a
        # profile added afterwards may not have it yet — create it so the open
        # never fails on a valid, enabled setting.
        if path is not None:
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        self._open_folder_path(path, descr)

"""Game-folder actions, Proton prefix tools and the Wizard menu / wizard tabs.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QTimer


class MainWindow:

    # ---- Proton tools ------------------------------------------------------
    def _proton_game(self):
        """Return the active configured game, or None (after notifying)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return None
        return game

    def _open_folder_path(self, path, descr):
        """Open *path* in the file manager, or log why it can't be opened.
        Ported from gui/dialogs ProtonTools._open_folder."""
        if path is None:
            self._notify(self.tr("{0} is not configured for this game.").format(descr), "warning")
            return
        path = Path(path)
        if not path.is_dir():
            self._notify(self.tr("{0} not found ({1}).").format(descr, path), "warning")
            return
        from Utils.xdg import xdg_open
        try:
            xdg_open(str(path))
        except Exception as e:
            self._append_log(f"Open {descr} error: {e}")

    def _quick_configure_submenu(self, game):
        """Build the profile dropdown's 'Quick configure' submenu: the active
        profile's Configure-view options as inline checkable/radio entries that
        flip live when clicked (like saving that one field would). Returns a list
        of SelectorButton action entries, or [] when the game has no options."""
        from Utils.exe_launch.quick_configure import build_quick_configure_options
        try:
            options = build_quick_configure_options(game)
        except Exception as e:
            self._append_log(f"[quick-configure] build failed: {e}")
            return []
        entries = []
        for opt in options:
            if opt["kind"] == "toggle":
                entries.append((
                    self.tr(opt["label"]),
                    lambda checked, act, o=opt: self._apply_quick_configure(
                        o, checked, act),
                    {"checkable": True, "checked": opt["value"], "keep_open": True}))
            else:  # choice → a nested submenu of radio entries
                gid = f"qc_{opt['key']}"
                sub = []
                for val, clabel in opt["choices"]:
                    sub.append((
                        self.tr(clabel),
                        lambda checked, act, o=opt, v=val: (
                            self._apply_quick_configure(o, v, act)
                            if checked else None),
                        {"checkable": True, "checked": val == opt["value"],
                         "group": gid, "keep_open": True, "value": val}))
                entries.append((self.tr(opt["label"]), sub))
        return entries

    def _apply_quick_configure(self, opt, value, action=None):
        """Apply one quick-configure option live to the active profile, then run
        the same post-save refresh the Configure view triggers. The menu stays
        open, so on failure we revert *action*'s check state in place rather than
        rebuilding the (still-shown) menu."""
        game = getattr(self._gs, "game", None)
        if game is None:
            return
        # Deploy method can't change while mods are deployed (would strand them).
        if opt["key"] == "deploy_mode":
            from Utils.exe_launch.quick_configure import deploy_mode_change_blocked
            if deploy_mode_change_blocked(game, value):
                self._notify(
                    self.tr("Cannot change the deploy method while mods are "
                            "deployed. Restore first."), "warning")
                self._revert_quick_configure(opt, action)
                return
        try:
            opt["apply"](value)
        except Exception as e:
            self._append_log(f"[quick-configure] apply {opt['key']} failed: {e}")
            self._notify(self.tr("Could not change {0}.").format(
                self.tr(opt["label"])), "warning")
            self._revert_quick_configure(opt, action)
            return
        self._append_log(f"[quick-configure] {opt['key']} = {value}")
        # Track the new value so a later revert (in this still-open menu) knows
        # what the checkbox/radio should snap back to.
        opt["value"] = value
        if opt.get("needs_reload"):
            # A path-changing option (e.g. game patch version) — reload the
            # game's paths, then refresh the lists that depend on them (mirrors
            # a same-game Configure save, which _on_game_changed no-ops).
            try:
                game.load_paths()
            except Exception:
                pass
            self._reload_modlist()
            self._reload_plugins()
        # Keep the Configure tab (if open) in sync. The profile menu itself is
        # NOT rebuilt here — it's still open; its action state already reflects
        # the click, and the next open rebuilds fresh from _profile_actions()
        # (via the selector's aboutToShow hook), so a toggle that adds/removes
        # the Open submenu takes effect the next time the dropdown is opened.
        v = getattr(self, "_configure_game_view", None)
        if v is not None and self._tabs.has_key("configure_game"):
            try:
                v.refresh_for_profile()
            except Exception:
                pass
        self._notify(self.tr("Setting saved."), "info")

    def _revert_quick_configure(self, opt, action):
        """Snap a quick-configure action back to the option's real (unchanged)
        value after a blocked/failed change, without closing or rebuilding the
        open menu."""
        if action is None:
            return
        try:
            if opt["kind"] == "toggle":
                action.setChecked(bool(opt["value"]))
                return
            # Radio: re-check the sibling whose stashed value matches the real
            # (pre-change) value; the shared QActionGroup unchecks the rest.
            group = action.actionGroup()
            peers = group.actions() if group is not None else [action]
            for peer in peers:
                if peer.data() == opt["value"]:
                    peer.setChecked(True)
                    return
            action.setChecked(False)
        except Exception:
            pass

    def _open_game_dir(self, which: str):
        """Open one of the game's on-disk folders (game selector ▸ Open ▸ …).
        Mirrors gui/dialogs ProtonTools folder openers."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # A background worker may have left the game's _active_profile_dir out of
        # sync with the selected profile — re-assert it so staging/profile folder
        # opens resolve against the profile the dropdown actually shows.
        self._gs.reassert_active_profile()
        if which == "game":
            self._open_folder_path(game.get_game_path(), "game folder")
        elif which == "prefix":
            self._open_folder_path(game.get_prefix_path(), "prefix folder")
        elif which == "mygames":
            getter = getattr(game, "_mygames_path", None)
            path = getter() if callable(getter) else None
            if path is None:
                prefix = game.get_prefix_path()
                if prefix is not None:
                    path = prefix / "drive_c/users/steamuser/Documents/My Games"
            self._open_folder_path(path, "My Games folder")
        elif which == "appdata":
            prefix = game.get_prefix_path()
            if prefix is None:
                self._open_folder_path(None, "AppData folder")
                return
            sub = getattr(game, "_APPDATA_SUBPATH", None)
            if sub is not None:
                self._open_folder_path(prefix / sub, "AppData folder")
            else:
                self._open_folder_path(
                    prefix / "drive_c/users/steamuser/AppData/Local",
                    "AppData folder")
        elif which == "staging":
            getter = getattr(game, "get_effective_mod_staging_path", None) \
                or getattr(game, "get_mod_staging_path", None)
            path = getter() if callable(getter) else None
            self._open_folder_path(path, "staging folder")
        elif which == "profile":
            path = getattr(game, "_active_profile_dir", None)
            if path is None:
                try:
                    path = game.get_profile_root() / "profiles"
                except Exception:
                    path = None
            self._open_folder_path(path, "profile folder")
        elif which == "config":
            try:
                from Utils.config_paths import get_config_dir
                path = get_config_dir()
            except Exception:
                path = None
            self._open_folder_path(path, ".config folder")

    def _proton_winecfg(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.wine_proton.proton_tools import launch_wine_tool
        launch_wine_tool(game, "winecfg", log_fn=self._append_log)

    def _proton_regedit(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.wine_proton.proton_tools import launch_wine_tool
        launch_wine_tool(game, "regedit", log_fn=self._append_log)

    def _proton_dll_overrides(self):
        """Open the Wine DLL overrides manager scoped over the MODLIST panel
        (like the Settings gear): a per-DLL load-order picker for this game's
        prefix. The plugins panel + rest of the UI stay live."""
        game = self._proton_game()
        if game is None:
            return
        if self._tabs.has_key("dll_overrides"):
            self._tabs.focus_key("dll_overrides")
            return
        from gui_qt.views.dll_overrides_view import DllOverridesView
        view = DllOverridesView(self, game, log_fn=self._append_log)
        self._dll_overrides_view = view
        self._tabs.open_scoped_tab(
            view, self.tr("Wine DLL overrides"), self._modlist_panel_stack,
            key="dll_overrides")

    def _proton_winetricks(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.wine_proton.proton_tools import launch_winetricks
        self._notify(self.tr("Launching winetricks…"), "info")
        threading.Thread(
            target=lambda: launch_winetricks(
                game, log_fn=lambda m: self._op_log.emit(str(m))),
            daemon=True).start()

    def _proton_run_exe(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.wine_proton.portal_filechooser import pick_exe_file
        from Utils.wine_proton.proton_tools import launch_exe_in_prefix

        def _picked(exe_path):
            if exe_path is None:
                return
            launch_exe_in_prefix(game, exe_path, log_fn=self._append_log)

        pick_exe_file(self.tr("Select EXE to run in this prefix"), _picked)

    def _proton_install_vcredist(self):
        from Utils.wine_proton.proton_tools import install_vcredist
        self._run_proton_installer(
            self.tr("Installing VC++ Redistributable"),
            lambda plog: install_vcredist(self._gs.game, log_fn=plog))

    def _proton_install_d3dcompiler(self):
        from Utils.wine_proton.proton_tools import install_d3dcompiler_47
        self._run_proton_installer(
            self.tr("Installing d3dcompiler_47"),
            lambda plog: install_d3dcompiler_47(self._gs.game, log_fn=plog))

    def _proton_install_xact(self):
        from Utils.wine_proton.proton_tools import install_xact
        self._run_proton_installer(
            self.tr("Installing XACT audio (XAudio2)"),
            lambda plog: install_xact(self._gs.game, log_fn=plog))

    def _proton_install_dotnet(self, version: str):
        from Utils.wine_proton.proton_tools import install_dotnet
        self._run_proton_installer(
            self.tr("Installing .NET {0}").format(version),
            lambda plog: install_dotnet(self._gs.game, version, log_fn=plog))

    def _run_proton_installer(self, title: str, worker_fn):
        """Run a blocking Proton installer (*worker_fn(log_fn) -> bool*) on a
        worker thread, showing the indeterminate progress popup + a toast on
        completion. Serialized: refuses a second installer while one runs."""
        game = self._proton_game()
        if game is None:
            return
        if self._proton_busy:
            self._notify(self.tr("A Proton installer is already running."), "warning")
            return
        self._proton_busy = True
        self._op_title = title
        self._ensure_feedback()
        self._notify(self.tr("{0}…").format(title), "info")
        self._op_progress.emit(0, 0, title)   # indeterminate (busy) bar


        def _run():
            ok = False
            try:
                ok = bool(worker_fn(lambda m: self._op_log.emit(f"Proton Tools: {m}")))
            except Exception as exc:
                self._op_log.emit(f"Proton Tools error: {exc}")
            self._proton_done.emit(title, ok)

        threading.Thread(target=_run, daemon=True).start()

    def _on_proton_done(self, title: str, success: bool):
        self._proton_busy = False
        if self._progress_popup is not None:
            self._progress_popup.clear()
        if success:
            self._notify(self.tr("{0} — done.").format(title), "success")
        else:
            self._notify(self.tr("{0} — failed (see log).").format(title), "error")

    # ---- Wizard tools ------------------------------------------------------
    def _rebuild_wizard_menu(self):
        """(Re)populate the Wizard header menu for the current game. Runs on
        every aboutToShow: several games' wizard_tools do live filesystem
        checks (e.g. BodySlide only appears once its exe exists in staging),
        and this also handles game switches for free. Tools without a Qt view
        registered yet appear greyed out; EXCLUDED ones are dropped."""
        menu = getattr(self._wizard_btn, "_menu", None)
        if menu is None:
            return
        menu.clear()
        menu.setToolTipsVisible(True)
        game = self._gs.game
        if game is None:
            menu.addAction(self.tr("No game selected")).setEnabled(False)
            self._add_prefix_manager_action(menu)
            return
        from Utils.plugins.plugin_loader import get_all_wizard_tools
        from Utils.wizard_support.wizard_catalog import group_by_category
        from wizards_qt import EXCLUDED, get_spec
        try:
            tools = [t for t in get_all_wizard_tools(game)
                     if t.dialog_class_path not in EXCLUDED]
        except Exception as exc:
            self._append_log(f"Wizards: failed to list tools: {exc}")
            tools = []
        if not tools:
            menu.addAction(self.tr("No wizard tools for this game")).setEnabled(False)
            self._add_prefix_manager_action(menu, tools)
            return
        # Favourites submenu at the top — quick launch for pinned tools.
        from Utils.ui_config import load_favourite_wizards
        fav_ids = load_favourite_wizards()
        fav_tools = [t for t in tools if t.id in fav_ids]
        if fav_tools:
            fav_menu = menu.addMenu(self.tr("★ Favourites"))
            fav_menu.setToolTipsVisible(True)
            for tool in fav_tools:
                act = fav_menu.addAction(tool.label)
                if tool.description:
                    act.setToolTip(tool.description)
                if get_spec(tool.dialog_class_path) is None:
                    act.setEnabled(False)   # not ported to Qt yet
                else:
                    act.triggered.connect(
                        lambda _=False, t=tool: self._open_wizard_tool(t))
            menu.addSeparator()
        groups = group_by_category(tools)
        for cat, cat_tools in groups:
            target = menu if len(groups) == 1 else menu.addMenu(cat)
            target.setToolTipsVisible(True)
            for tool in cat_tools:
                act = target.addAction(tool.label)
                if tool.description:
                    act.setToolTip(tool.description)
                if get_spec(tool.dialog_class_path) is None:
                    act.setEnabled(False)   # not ported to Qt yet
                else:
                    act.triggered.connect(
                        lambda _=False, t=tool: self._open_wizard_tool(t))
        self._add_prefix_manager_action(menu, tools)

    def _add_prefix_manager_action(self, menu, tools=None):
        """Trailing 'Add Favourites…' + 'Manage Prefixes…' entries. The prefix
        manager is always available (it lists every game's tool prefixes, like
        the Tk wizard picker's button); 'Add Favourites…' is only shown when the
        current game has tools to favourite."""
        from Utils.nif.catalog_loader import NIF_VIEWER_GAME_IDS
        game = self._gs.game
        if game is not None and getattr(game, "game_id", None) in NIF_VIEWER_GAME_IDS:
            menu.addSeparator()
            viewer = menu.addAction(self.tr("NIF Viewer…"))
            viewer.setToolTip(self.tr(
                "Browse every mesh and texture the game would load — base game and "
                "each mod — and view them in 3D."))
            viewer.triggered.connect(lambda _=False: self._open_nif_viewer_tab())
            char = menu.addAction(self.tr("Character Viewer…"))
            char.setToolTip(self.tr(
                "Dress a whole character on the game's skeleton: armour, clothing and "
                "hair on the base body, with the skeleton overlay."))
            char.triggered.connect(lambda _=False: self._open_character_tab())
        menu.addSeparator()
        if tools:
            fav = menu.addAction(self.tr("Add Favourites…"))
            fav.setToolTip(self.tr("Choose which wizard tools appear at the top of "
                           "this menu for quick access."))
            fav.triggered.connect(
                lambda _=False, t=list(tools): self._open_favourite_wizards(t))
        act = menu.addAction(self.tr("Manage Prefixes…"))
        act.setToolTip(self.tr("Browse every wizard-tool Wine prefix and delete them "
                       "to reclaim disk space."))
        act.triggered.connect(lambda _=False: self._open_prefix_manager())

    def _open_nif_viewer_tab(self):
        """Open the NIF Viewer as a tab in the shared top tab bar (one instance;
        re-opening focuses it). It indexes the game and the active profile as
        they are now — close and re-open it after switching profile."""
        if self._tabs.has_key("nif_viewer"):
            self._tabs.focus_key("nif_viewer")
            return
        game = self._gs.game
        if game is None:
            return
        from gui_qt.nif_viewer.nif_viewer_view import NifViewerView
        view = NifViewerView(game, self._gs.profile_dir(), self._gs.staging_dir())
        view.equip_requested.connect(self._equip_on_character)
        self._tabs.open_tab(view, self.tr("NIF Viewer"), key="nif_viewer")

    def _open_character_tab(self):
        """Open the Character tab (one instance; re-opening focuses it) and
        return its view, or None if there is no game."""
        if self._tabs.has_key("character_viewer") and getattr(self, "_character_view", None) is not None:
            self._tabs.focus_key("character_viewer")
            return self._character_view
        game = self._gs.game
        if game is None:
            return None
        from gui_qt.nif_viewer.character_view import CharacterView
        view = CharacterView(game, self._gs.profile_dir(), self._gs.staging_dir())
        # Equipping from the NIF Viewer happens with the Character tab in the
        # background — its result (slot, or why it was refused) shows as a toast.
        view.equip_result.connect(
            lambda msg, ok: self._notify(msg, "success" if ok else "warning"))
        self._character_view = view
        self._tabs.open_tab(view, self.tr("Character"), key="character_viewer")
        return view

    def _equip_on_character(self, entry):
        """"Add to character" from the NIF Viewer: put the mesh on the Character
        tab, opening it first if needed (an already-open tab is left in the
        background so several pieces can be added in a row)."""
        if self._tabs.has_key("character_viewer") and getattr(self, "_character_view", None) is not None:
            self._character_view.equip(entry)
            return
        view = self._open_character_tab()
        if view is not None:
            view.equip(entry)

    def _open_favourite_wizards(self, tools):
        """Open the borderless favourites picker for the given wizard *tools*.
        Saving replaces the global favourites set; the Wizard menu rebuilds its
        Favourites submenu on next open."""
        from gui_qt.overlays.favourite_wizards_overlay import FavouriteWizardsOverlay
        from Utils.ui_config import load_favourite_wizards, save_favourite_wizards

        # Only offer tools that are actually openable (ported to Qt).
        from wizards_qt import get_spec
        items = [(t.label, t.id) for t in tools
                 if get_spec(t.dialog_class_path) is not None]
        if not items:
            return

        def _done(chosen):
            if chosen is None:
                return
            try:
                save_favourite_wizards(chosen)
            except Exception as exc:
                self._append_log(f"Wizards: failed to save favourites: {exc}")

        FavouriteWizardsOverlay.show_over(
            self, items, load_favourite_wizards(), _done)

    def _open_prefix_manager(self):
        """Open the prefix manager as a plugins-panel-scoped tab."""
        if self._tabs.has_key("prefix_manager"):
            self._tabs.focus_key("prefix_manager")
            return
        from gui_qt.views.prefix_manager_view import PrefixManagerView
        game = self._gs.game
        view = PrefixManagerView(
            active_game_name=(game.name if game is not None else ""),
            on_close=lambda: self._close_wizard_tab("prefix_manager"),
            log_fn=self._append_log)
        self._tabs.open_scoped_tab(
            view, self.tr("Manage Prefixes"), self._plugins_panel_stack,
            key="prefix_manager")

    def _open_wizard_tool(self, tool):
        """Open a ported wizard tool as a panel-scoped tab (plugins panel for
        most tools; modlist panel for the wide ones that were full-width
        overlays in Tk). Re-opening an already-open tool refocuses its tab."""
        game = self._gs.game
        if game is None:
            return
        from wizards_qt import get_spec
        spec = get_spec(tool.dialog_class_path)
        if spec is None:
            return
        if spec.steam_install_only and getattr(game, "uses_game_copy", lambda: False)():
            from gui_qt.overlays.confirm_overlay import ConfirmOverlay
            self._append_log(f"[wizard] {tool.label}: not available, this profile runs on "
                             f"its own game copy ({game.get_game_path()})")
            ConfirmOverlay.show_message(
                self, self.tr("Not available for a game copy"),
                self.tr("This profile runs on its own game copy:\n{0}\n\n{1} changes the "
                        "Steam install's game files, so it can't be used here. The game "
                        "copy keeps the version it was made with.").format(
                    game.get_game_path(), tool.label))
            return
        key = f"wizard:{tool.id}"
        if self._tabs.has_key(key):
            self._tabs.focus_key(key)
            return
        # _full_width_overlay is a Tk-only hint (the registry's `panel` field
        # replaces it); the rest of extra is forwarded to the view.
        extra = {k: v for k, v in (tool.extra or {}).items()
                 if k != "_full_width_overlay"}
        stack = (self._modlist_panel_stack if spec.panel == "modlist"
                 else self._plugins_panel_stack)
        from wizards_qt import QtWizardContext
        ctx = QtWizardContext(
            profile_name=self._gs.profile or "default",
            run_deploy=self._wizard_run_deploy,
            run_restore=self._wizard_run_restore,
            refresh_modlist=self._on_refresh_modlist,
            refresh_plugins=self._wizard_refresh_plugins,
            import_manifest=lambda manifest, stem, bundle_zip:
                self._open_manifest_import(manifest, stem, bundle_zip=bundle_zip),
            current_profile=lambda: self._gs.profile or "default",
            nexus_api=self._ensure_nexus_api,
        )
        try:
            view = spec.view_factory(
                game, log_fn=self._append_log,
                on_close=lambda k=key: self._close_wizard_tab(k),
                ctx=ctx, **extra)
        except Exception as exc:
            self._append_log(f"Wizards: failed to open {tool.label}: {exc}")
            return
        self._tabs.open_scoped_tab(view, tool.label, stack, key=key)

    def _wizard_run_deploy(self, on_done) -> bool:
        """Start a deploy for a wizard step through the normal deploy path
        (mutex/coalesce + progress popup). *on_done(ok)* fires on the UI
        thread after the final deploy completes. Returns False when a deploy
        can't be started (unconfigured game)."""
        game = self._gs.game
        if game is None or not game.is_configured() or not hasattr(game, "deploy"):
            return False
        self._deploy_done_hooks.append(on_done)
        self._on_deploy()
        return True

    def _wizard_run_restore(self, on_done, *, ignore_col_install: bool = False) -> bool:
        """Start a Restore (undeploy) for a wizard step through the normal
        restore path — root-flagged mods, active-profile dir and the deploy
        flag are handled there, which game.restore() alone doesn't do.
        *on_done(ok)* fires on the UI thread when it finishes (a restore queued
        behind an install fires once it has run). Returns False when one can't
        be started now (unconfigured game, or a deploy/restore already running).
        *ignore_col_install* — see _on_restore; set by the collection preflight,
        which runs its own Restore step before any profile/install exists.
        """
        game = self._gs.game
        if (game is None or not game.is_configured()
                or not hasattr(game, "restore") or self._deploy_running):
            return False
        self._restore_done_hooks.append(on_done)
        self._on_restore(ignore_col_install=ignore_col_install)
        return True

    def _wizard_refresh_plugins(self):
        """Wizard hook: re-run LOOT to refresh plugin metadata without touching
        the load order (the xEdit wizards call this after a clean/edit session
        so dirty/message flags update). Deferred a beat so it runs after the
        refresh_modlist() reload the wizard also fires — the LOOT refresh reads
        the plugin model's rows, which that reload rebuilds."""
        game = self._gs.game
        if game is None or not game.is_configured():
            return
        if not getattr(game, "loot_sort_enabled", False):
            return
        # QTimer so it lands after the modlist refresh + plugin reload settle.
        QTimer.singleShot(0, self._on_refresh_plugins)

    def _close_wizard_tab(self, key: str):
        if self._tabs.has_key(key):
            self._tabs.close_tab(key)

    def _close_wizard_tabs(self):
        """Close every open wizard tab (game switch — they're game-scoped).
        The prefix manager closes too: its active-game highlight goes stale."""
        for key in [k for k in list(self._tabs._keys)
                    if k.startswith("wizard:") or k == "prefix_manager"]:
            self._tabs.close_tab(key)

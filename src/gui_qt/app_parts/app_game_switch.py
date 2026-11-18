"""Game and profile switching: selector handlers, browser retargeting, game actions.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import time


class MainWindow:

    # ---- selector handlers -------------------------------------------------
    def _on_game_changed(self, name):
        if name == self._gs.game_name:
            return
        # The NIF Viewer / Character tabs index the previous game — close them
        # (see _close_nif_character_tabs) like every other game-scoped tab
        # below. The confirmation this warrants for a user-initiated switch is
        # the selector's own job (_confirm_game_change) — this method itself
        # stays synchronous since several callers (Nexus auto-switch-on-
        # download, Add Game, collection-cancel cleanup) depend on that.
        self._close_nif_character_tabs()
        self._gs.set_game(name)
        # The Profile Settings tab is scoped to the previous game — close it.
        if self._tabs.has_key("profile_settings"):
            self._tabs.close_tab("profile_settings")
            self._profile_settings_view = None
        # The Wine DLL overrides tab is game-scoped too — close it.
        if self._tabs.has_key("dll_overrides"):
            self._tabs.close_tab("dll_overrides")
            self._dll_overrides_view = None
        # The View Requirements tab reads the previous game's staging — close
        # it (its destroyed hook restores the conflict highlights).
        if self._tabs.has_key("view_requirements"):
            self._tabs.close_tab("view_requirements")
        # Wizard tool tabs are game-scoped — close them all.
        self._close_wizard_tabs()
        # The exe-settings tab is bound to the previous game's exe.
        self._close_exe_settings_tab()
        # Userlist tabs/bars hold the previous profile's userlist path.
        self._close_userlist_ui()
        # Retarget the Nexus / Collections browsers (if open) at the new game so
        # they show the new game's mods instead of holding a stale domain. A
        # missing/empty Nexus domain closes them (nothing to show).
        self._retarget_browsers_for_game()
        # Reflect the new game's profiles + keep both game selectors in sync.
        profs = self._gs.profiles()
        if profs:
            self._profile_selector.set_items(profs, current=self._gs.profile)
        # The Open submenu depends on the new game's profile-specific settings.
        self._refresh_profile_actions()
        self._game_selector.set_current(name)
        # The 'Edit custom game…' entry depends on the active game.
        self._refresh_game_actions()
        self._refresh_play_selector()
        self._clear_search_boxes()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()

    def _retarget_browsers_for_game(self):
        """On game switch, point any open Nexus / Collections browser at the new
        game (or close them if the new game has no Nexus domain). Per-collection
        detail tabs are game-specific, so they're closed unconditionally."""
        game = self._gs.game
        domain = (getattr(game, "nexus_game_domain", "") or "") if game else ""

        # The Wabbajack browser isn't tied to Nexus: it just re-filters.
        wj = getattr(self, "_wabbajack", None)
        if wj is not None:
            try:
                wj.on_game_changed(game)
            except Exception as exc:
                self._append_log(f"[wabbajack] retarget failed: {exc}")

        # Close per-collection detail tabs (they belong to the previous game).
        # Swallow (a game switch must not abort) but leave a trace — a failure
        # here leaves stale tabs from the previous game.
        try:
            for key in [k for k in list(self._tabs._keys)
                        if k.startswith("collection_detail_")]:
                self._tabs.close_tab(key)
        except Exception as exc:
            from Utils.app_log import app_log
            app_log(f"game switch: closing collection tabs failed: {exc}")

        for attr, tab_key in (("_nexus_view", "nexus_browser"),
                              ("_collections_view", "collections")):
            view = getattr(self, attr, None)
            if view is None:
                continue
            if not domain:
                # New game can't show Nexus content — close the tab.
                if self._tabs.has_key(tab_key):
                    self._tabs.close_tab(tab_key)
                setattr(self, attr, None)
                continue
            try:
                view.set_game(game, domain)
            except Exception as exc:
                self._append_log(f"[nexus] retarget failed: {exc}")

    def _clear_search_boxes(self):
        """Reset both search boxes on a game/profile switch (Tk parity — a
        stale query silently filters the freshly-loaded lists)."""
        for attr, text_attr in (("_modlist_search", "_modlist_search_text"),
                                ("_plugins_search", "_plugin_search_text")):
            box = getattr(self, attr, None)
            if box is not None and box.text():
                box.clear()   # textChanged → debounced apply sees empty
            setattr(self, text_attr, "")

    def _on_profile_changed(self, name):
        if name == self._gs.profile:
            return
        # The NIF Viewer / Character tabs index the previous profile — close
        # them (see _close_nif_character_tabs) like the other profile-scoped
        # UI below. Confirming this for a user-initiated switch is the
        # selector's own job (_confirm_profile_change) — this method stays
        # synchronous since callers (collection-cancel cleanup) depend on that.
        self._close_nif_character_tabs()
        from Utils import perftrace
        # End-to-end switch latency: the switch "feels done" only when the async
        # milestones land (meta → plugins → conflicts → final plugin pass), so
        # stamp t0 here and mark elapsed at each (see _mark_since_switch).
        if perftrace.is_enabled():
            self._switch_t0 = time.perf_counter()
            self._switch_conflicts_done = False
        with perftrace.span("switch.sync_total"):
            with perftrace.span("switch.set_profile"):
                self._gs.set_profile(name)
            self._profile_selector.set_current(name)
            self._update_profile_collection_tooltip()
            # profile_ini_files / profile_saves are per-profile overrides — set_profile
            # reloaded them, so refresh the Open submenu for the new profile.
            self._refresh_profile_actions()
            # Userlist tabs/bars are profile-scoped (userlist.yaml lives there).
            self._close_userlist_ui()
            # Exe selection + exe args are per-profile.
            self._close_exe_settings_tab()
            with perftrace.span("switch.refresh_play_selector"):
                self._refresh_play_selector()
            self._clear_search_boxes()
            # Non-specific profiles share the mods folder: a mod added while
            # another profile was active is on disk but absent from this
            # profile's modlist.txt. Sync the file against the folder so the
            # new mod shows up on switch without a manual Refresh. This is
            # cheap (one iterdir + modlist.txt diff) and does NOT rescan the
            # shared index/filemap, which is unchanged across the switch.
            with perftrace.span("switch.sync_modlist_folder"):
                try:
                    from Utils.mods.modlist import sync_modlist_with_mods_folder
                    ml = self._gs.modlist_path()
                    staging = self._gs.staging_dir()
                    if ml is not None and staging is not None:
                        sync_modlist_with_mods_folder(ml, staging)
                except Exception as exc:
                    print(f"[gui_qt] profile-switch modlist sync failed: {exc}",
                          flush=True)
            with perftrace.span("switch.reload_modlist(sync)"):
                self._reload_modlist()
            with perftrace.span("switch.reload_plugins(kickoff)"):
                if getattr(self, "_reload_had_entries", False):
                    # The conflict rebuild just queued by _reload_modlist ends
                    # in a plugin reload against the FRESH filemap. Loading now
                    # too would run the whole per-plugin pipeline twice — and
                    # the early pass resolves against the OLD profile's filemap
                    # (observed: 10 of 254 plugins listed until the rebuild
                    # corrected it). Clear the stale panel; the rebuild's
                    # reload populates it once, correctly.
                    self._clear_plugin_panel()
                else:
                    # Empty modlist → no conflict rebuild is coming; load the
                    # (vanilla-only) plugin list directly.
                    self._reload_plugins()
            self._update_deployed_profile_highlight()
        # Appended-collections section tracks the ACTIVE profile.
        view = getattr(self, "_collections_view", None)
        if view is not None:
            try:
                view.refresh_appended()
            except Exception:
                pass
        # Keep the Profile Settings ★ marker in sync if that tab is open.
        if self._tabs.has_key("profile_settings"):
            v = getattr(self, "_profile_settings_view", None)
            if v is not None:
                v.set_current_profile(name)
        # Configure-Game tab tracks the active profile too — refresh its form +
        # tab title in place (no focus change) so the user doesn't have to
        # reopen it to configure a different profile.
        if self._tabs.has_key("configure_game"):
            v = getattr(self, "_configure_game_view", None)
            if v is not None:
                try:
                    v.refresh_for_profile()
                    self._tabs.set_tab_title(
                        "configure_game", self._configure_tab_title(self._gs.game))
                except Exception:
                    pass

    def _mark_since_switch(self, label: str) -> None:
        """Perf instrumentation (MM_PERFTRACE=1): record wall-clock elapsed
        since the last profile switch under *label*. The switch's perceived
        latency spans thread boundaries (sync UI work → meta worker → plugin
        worker → conflict/filemap worker → final plugin pass), which a single
        span can't measure — so each async milestone marks its own arrival
        time. No-op when perftrace is disabled or no switch is in flight."""
        t0 = getattr(self, "_switch_t0", None)
        if t0 is None:
            return
        from Utils import perftrace
        perftrace.mark(label, time.perf_counter() - t0)

    def _game_actions(self):
        """Build the pinned entries for the game selector. The 'Edit custom
        game…' entry only appears when the active game is a custom game whose
        definition is editable (i.e. not a repo handler with editable: false)."""
        actions = [
            (self.tr("Add game…"), lambda: self._on_game_action("add")),
            (self.tr("Configure game…"), lambda: self._on_game_action("configure")),
            (self.tr("Define custom game…"), lambda: self._on_game_action("custom")),
        ]
        game = getattr(self._gs, "game", None)
        if (game is not None
                and getattr(game, "is_custom", False)
                and getattr(game, "editable", False)):
            actions.append(
                (self.tr("Edit custom game…"), lambda: self._on_game_action("edit_custom")))
        # Repo handlers (downloaded from the Resources branch) carry version +
        # editable metadata; user-authored definitions don't. Only those can be
        # force-refreshed from the repo.
        defn = getattr(game, "_defn", None) if game is not None else None
        if (game is not None
                and getattr(game, "is_custom", False)
                and isinstance(defn, dict)
                and "version" in defn and "editable" in defn):
            actions.append(
                (self.tr("Force update handler"),
                 lambda: self._on_game_action("update_handler")))
        actions.append(
            (self.tr("Open"), [
                (self.tr("Game folder"),     lambda: self._open_game_dir("game")),
                (self.tr("Prefix folder"),   lambda: self._open_game_dir("prefix")),
                (self.tr("My Games folder"), lambda: self._open_game_dir("mygames")),
                (self.tr("AppData folder"),  lambda: self._open_game_dir("appdata")),
                (self.tr("Staging folder"),  lambda: self._open_game_dir("staging")),
                (self.tr("Profile folder"),  lambda: self._open_game_dir("profile")),
                (self.tr(".config folder"),  lambda: self._open_game_dir("config")),
            ]))
        return actions

    def _refresh_game_actions(self):
        """Rebuild the game-selector menu so the 'Edit custom game…' entry
        appears/disappears to match the active game."""
        if getattr(self, "_game_selector", None) is not None:
            self._game_selector.set_actions(self._game_actions())

    def _on_game_action(self, which):
        if which == "add":
            self._open_add_game_tab()
        elif which == "configure":
            game = self._gs.game
            if game is not None:
                self._open_configure_game_tab(game)
            else:
                self._append_log("[game] no active game to configure")
        elif which == "custom":
            self._open_custom_game_tab()
        elif which == "edit_custom":
            self._open_edit_custom_game_tab()
        elif which == "update_handler":
            self._force_update_handler()
        else:
            self._append_log(f"[game] {which} (not wired yet)")

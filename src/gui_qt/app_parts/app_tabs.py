"""Tab openers and their close/focus handlers: settings, change-version, separator, requirements, conflicts, game setup, collections.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

from pathlib import Path as _P

from gui_qt.app_parts.app_updates import _load_bg3_modio
from Utils.wine_proton.framework_detect import game_provides_requirement


class MainWindow:

    def _open_settings_tab(self):
        """Open the Settings tab scoped over the MODLIST panel (like the image
        preview / text editor): it shows in the modlist region (in the shared top
        tab bar) while the plugins panel and the rest of the UI stay live.
        Re-clicking the gear focuses the existing tab."""
        from gui_qt.views.settings_view import SettingsView
        if self._tabs.has_key("settings"):
            self._tabs.focus_key("settings")
            return
        view = SettingsView(self)
        self._tabs.open_scoped_tab(
            view, self.tr("Settings"), self._modlist_panel_stack, key="settings")

    def _open_install_name_patterns_tab(self):
        """Open the custom install-name rules editor scoped over the MODLIST
        panel (like Settings). Re-opening focuses the existing tab."""
        from gui_qt.views.install_name_patterns_view import InstallNamePatternsView
        if self._tabs.has_key("install_name_patterns"):
            self._tabs.focus_key("install_name_patterns")
            return
        view = InstallNamePatternsView(self)
        self._tabs.open_scoped_tab(
            view, self.tr("Install-name Rules"),
            self._modlist_panel_stack, key="install_name_patterns")

    def _open_theme_editor_tab(self):
        """Open the Theme Editor as a full-screen tab (its own key). Editing a
        theme touches the whole window, so unlike Settings it takes over the
        entire UI rather than a single panel. Re-opening focuses the existing
        tab."""
        from gui_qt.theme.theme_editor_view import ThemeEditorView
        if self._tabs.has_key("theme_editor"):
            self._tabs.focus_key("theme_editor")
            return
        view = ThemeEditorView(self)
        self._tabs.open_tab(view, self.tr("Theme Editor"), key="theme_editor")

    def _open_image_preview_tab(self, path, rel_str, rel_key=None, mod_name=None):
        """Open an image/.dds preview as a MODLIST-PANEL-SCOPED tab: it shows in
        the modlist region (in the shared top tab bar) while the Mod Files tree
        in the plugins panel stays live. Reuses one preview tab — browsing to a
        new image swaps it in place (Tk parity). When other mods ship the same
        file the preview offers a picker and a side-by-side compare."""
        from gui_qt.image_preview import ImagePreview
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        providers = self._asset_providers(rel_key, mod_name)
        existing = getattr(self, "_image_preview_widget", None)
        if existing is not None and self._tabs.has_key("mf_image_preview"):
            existing.set_image(_P(path), name, providers, mod_name)
            self._tabs.focus_key("mf_image_preview")
            self._tabs.set_tab_title("mf_image_preview", name)
            return
        widget = ImagePreview(_P(path), name, providers=providers,
                              current_mod=mod_name)
        self._image_preview_widget = widget
        self._tabs.open_scoped_tab(
            widget, name, self._modlist_panel_stack, key="mf_image_preview")

    def _open_custom_game_tab(self):
        """Open the Define-Custom-Game form as a (detachable) tab. On save, chain
        to the Configure-Game tab so the user can set the install path/prefix
        (a fresh custom game has no path yet → not yet selectable)."""
        if self._tabs.has_key("custom_game"):
            self._tabs.focus_key("custom_game")
            return
        from gui_qt.views.custom_game_view import CustomGameView

        def _done(saved_defn, deleted):
            self._tabs.close_tab("custom_game")
            if saved_defn is None:
                return
            from Utils.exe_launch.game_helpers import _GAMES, _load_games
            names = _load_games()
            self._gs.game_names = names
            real_names = [n for n in names if n != "No games configured"]
            if real_names:
                self._game_selector.set_items(
                    real_names, current=self._gs.game_name)
            else:
                self._game_selector.set_items([], current=self.tr("Add game"))
            self._append_log(f"[game] custom game defined: {saved_defn['name']}")
            game = _GAMES.get(saved_defn["name"])
            if game is not None:
                # A freshly-defined custom game has no path yet — this is the
                # add flow, so Save should close the tab when done.
                self._open_configure_game_tab(game, from_add_game=True)

        view = CustomGameView(on_done=_done)
        self._tabs.open_tab(view, self.tr("Define custom game"), key="custom_game")

    def _open_edit_custom_game_tab(self):
        """Open the custom-game form pre-filled with the active game's definition
        so it can be edited in place. Only reachable for custom, editable games
        (see _game_actions). On save, refresh the game list + reload the game so
        any changed name/paths/routing take effect."""
        game = self._gs.game
        defn = getattr(game, "_defn", None) if game is not None else None
        if not defn:
            self._append_log("[game] no editable custom-game definition to edit")
            return
        if self._tabs.has_key("custom_game"):
            self._tabs.focus_key("custom_game")
            return
        from gui_qt.views.custom_game_view import CustomGameView

        prev_name = self._gs.game_name

        def _done(saved_defn, deleted):
            self._tabs.close_tab("custom_game")
            from Utils.exe_launch.game_helpers import _load_games
            names = _load_games()
            self._gs.game_names = names
            real_names = [n for n in names if n != "No games configured"]
            # A rename or delete may have changed which game we should show.
            target = None
            if saved_defn is not None and saved_defn["name"] in real_names:
                target = saved_defn["name"]
            elif prev_name in real_names:
                target = prev_name
            elif real_names:
                target = real_names[0]
            if target is not None:
                self._game_selector.set_items(real_names, current=target)
                # Re-select through the normal path so all views reload for the
                # (possibly renamed) game.
                self._gs.game_names = names
                self._gs.set_game(target)
                self._game_selector.set_current(target)
                self._refresh_game_actions()
                self._refresh_profile_actions()
                self._refresh_play_selector()
                self._reload_modlist()
                self._reload_plugins()
            else:
                self._game_selector.set_items([], current=self.tr("Add game"))
                self._refresh_game_actions()
            if deleted:
                self._append_log(f"[game] custom game deleted: {prev_name}")
            elif saved_defn is not None:
                self._append_log(f"[game] custom game edited: {saved_defn['name']}")

        view = CustomGameView(on_done=_done, existing=defn)
        self._tabs.open_tab(view, self.tr("Edit custom game"), key="custom_game")

    def _open_add_game_tab(self):
        """Open the Add Game card-grid picker as a (detachable) tab."""
        from gui_qt.views.add_game_view import AddGameView
        from Utils.exe_launch.game_helpers import _GAMES, _load_games
        _load_games()   # refresh registry (populates _GAMES with ALL games)
        page = AddGameView(dict(_GAMES),
                           on_select=self._on_add_game_select,
                           on_add=self._on_add_game_add)
        self._tabs.open_tab(page, self.tr("Add game"), key="add_game")
        # Pull down any custom-game banner images still missing on disk (e.g.
        # handlers synced on a previous run but their images never fetched).
        self._download_custom_game_images(page)

    def _open_onboarding_tab(self):
        """Open first-run onboarding as a fullscreen detachable tab (like the
        Nexus browser). Any dismissal — Skip, Add-a-Game, tab X, detach-close —
        persists onboarding_complete=1 via the view's destroyed handler."""
        if self._tabs.has_key("onboarding"):
            self._tabs.focus_key("onboarding")
            return
        from gui_qt.views.onboarding_view import OnboardingView
        view = OnboardingView(
            on_login=self._nexus_login_sso,
            on_add_game=self._open_add_game_tab,
            on_done=self._finish_onboarding,
            already_logged_in=self._ensure_nexus_api() is not None,
            on_language_change=self._apply_language,
            on_sync_languages=self._sync_languages_now,
        )
        self._onboarding_view = view
        # ANY dismissal (Skip, Add-Game, tab X, detach-close) tears the view
        # down → mark onboarding done + drop the ref.
        view.destroyed.connect(self._on_onboarding_destroyed)
        self._tabs.open_tab(view, self.tr("Welcome"), key="onboarding")

    def _finish_onboarding(self):
        """Called by the view's on_done (Skip / Add-a-Game). Just close the tab;
        the destroyed handler persists the flag."""
        self._tabs.close_tab("onboarding")

    def _on_onboarding_destroyed(self, *_):
        from Utils.ui_config import save_onboarding_complete
        try:
            save_onboarding_complete(True)
        except Exception:
            pass
        self._onboarding_view = None

    def _open_change_version_tab(self, mod_name: str):
        """Open the Change Version picker for *mod_name* as a tab that takes over
        the whole plugins panel. Triggered by the update-flag click + the
        right-click 'Change Version' item."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        self._gs.reassert_active_profile()
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        from Nexus.nexus_meta import read_meta
        meta = read_meta(staging / mod_name / "meta.ini")
        if int(getattr(meta, "mod_id", 0) or 0) <= 0:
            self._notify(self.tr("'{0}' isn't a Nexus mod.").format(mod_name), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return

        # Reuse one overlay: rebuild it for the new mod if already open.
        if self._tabs.has_key("change_version"):
            self._tabs.close_tab("change_version")
        from gui_qt.views.change_version_view import ChangeVersionView
        view = ChangeVersionView(
            api, game, mod_name, meta,
            install_fn=self._install_paths,
            on_close=self._close_change_version_tab,
            log_fn=self._append_log,
            progress_fn=self._nexus_download_progress)
        self._change_version_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_change_version_view", None))
        self._tabs.open_scoped_tab(
            view, self.tr("Change Version"), self._plugins_panel_stack,
            key="change_version")

    def _close_change_version_tab(self):
        """Close the Change Version overlay + refresh modlist flags (an Ignore-
        Update toggle or an install may have changed meta.ini).

        When an install kicked off from this tab is still in flight, do nothing:
        the install's own completion path (_on_install_done) reloads the modlist
        + rebuilds the filemap once at the end. Reloading here as well caused a
        redundant refresh (+ filemap rebuild) BEFORE the "replace old mod?"
        dialog, on top of the necessary one after. For a plain close (Ignore-
        Update toggle only touched meta.ini flags) a light flag refresh is
        enough — no filemap rebuild needed."""
        if self._tabs.has_key("change_version"):
            self._tabs.close_tab("change_version")
        if getattr(self, "_install_running", False):
            return
        self._refresh_modlist_flags()

    def _open_change_version_modio_tab(self, mod_name: str):
        """Open the Change Version (mod.io) picker for *mod_name* as a tab that
        takes over the whole plugins panel. Triggered by the mod.io update-flag
        click + the right-click 'Change Version (mod.io)' item. Mirrors
        _open_change_version_tab (Nexus) but reads/writes mod.io meta.ini keys
        and needs no premium check (mod.io files are always public)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        self._gs.reassert_active_profile()
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        modio_meta = _load_bg3_modio("modio_meta")
        meta_path = staging / mod_name / "meta.ini"
        meta = modio_meta.read_modio_meta(meta_path)
        if meta.mod_id <= 0:
            self._notify(self.tr("'{0}' isn't a mod.io mod.").format(mod_name), "warning")
            return
        api_key = _load_bg3_modio("modio_key").load_modio_key()
        if not api_key:
            self._notify(
                self.tr("No mod.io API key configured — Wizard ▸ mod.io API Key."),
                "warning")
            return
        modio_api = _load_bg3_modio("modio_api")
        try:
            api = modio_api.ModioAPI(api_key)
        except Exception as exc:
            self._notify(self.tr("Could not initialise mod.io API: {0}").format(exc), "error")
            return

        # Reuse one overlay: rebuild it for the new mod if already open.
        if self._tabs.has_key("change_version_modio"):
            self._tabs.close_tab("change_version_modio")
        from gui_qt.views.change_version_modio_view import ModioChangeVersionView
        view = ModioChangeVersionView(
            api, mod_name, meta, meta_path, modio_meta,
            getattr(game, "name", "") or "",
            install_fn=self._install_modio_change_version,
            on_close=self._close_change_version_modio_tab,
            log_fn=self._append_log,
            progress_fn=self._nexus_download_progress)
        self._change_version_modio_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_change_version_modio_view", None))
        self._tabs.open_scoped_tab(
            view, self.tr("Change Version (mod.io)"), self._plugins_panel_stack,
            key="change_version_modio")

    def _close_change_version_modio_tab(self):
        """Close the Change Version (mod.io) overlay + refresh modlist flags
        (mirrors _close_change_version_tab)."""
        if self._tabs.has_key("change_version_modio"):
            self._tabs.close_tab("change_version_modio")
        if getattr(self, "_install_running", False):
            return
        self._refresh_modlist_flags()

    def _install_modio_change_version(self, mod_name, meta, file, archive_path):
        """install_fn for ModioChangeVersionView: install the chosen file into
        the mod's existing folder (silent replace, same as Quick Update
        (mod.io) — mod.io has no name-matching concept, so there's no
        "different folder name" case to defer to the user). _install_paths'
        `metas` dict expects Nexus-shaped meta objects, so — same as Quick
        Update (mod.io) — the install pipeline's own heuristic identification
        runs first, then meta.ini is overwritten with the file we KNOW we
        fetched via the existing _stamp_modio_meta helper."""
        staging = self._gs.staging_dir()

        def _done(ok, total, names):
            if staging is not None and mod_name in (names or []):
                self._stamp_modio_meta(staging / mod_name / "meta.ini", meta, file)
                self._reload_modlist()

        self._install_paths(
            [archive_path], preferred_names={archive_path: mod_name},
            on_all_done=_done)

    def _open_sep_settings_tab(self, sep_name, current_color, current_deploy):
        """Open the Separator Settings picker (colour + deploy override) as a tab
        over the plugins panel. Triggered by the right-click 'Separator settings…'
        item. *sep_name* is the internal `..._separator` name (the storage key)."""
        from gui_qt.views.separator_settings_view import SeparatorSettingsView
        if self._tabs.has_key("sep_settings"):
            self._tabs.close_tab("sep_settings")
        view = SeparatorSettingsView(
            sep_name, current_color, current_deploy,
            on_save=lambda color, deploy: self._save_sep_settings(
                sep_name, color, deploy),
            on_close=self._close_sep_settings_tab)
        self._tabs.open_scoped_tab(
            view, self.tr("Separator Settings"), self._plugins_panel_stack,
            key="sep_settings")

    def _write_separator_color_to_staging(self, sep_name, color):
        """Mirror a separator's colour into MO2's own ``meta.ini``.

        MO2 keeps separator colours in ``mods/<Name>_separator/meta.ini``, and
        that is where Mosaic reads them from on import. Without writing back,
        a colour changed here is invisible to MO2 and is overwritten by the old
        one the next time the staging folder is imported.

        Only separators that already have a folder are touched -- those came
        from MO2 or Wabbajack. Mosaic's own separators exist only as
        modlist.txt entries, and creating MO2 folders for them is a different
        decision from saving a colour.
        """
        game = self._gs.game
        if game is None:
            return
        try:
            from pathlib import Path as _Path

            from Utils.mods.mo2_separator_meta import write_separator_color
            getter = getattr(game, "get_effective_mod_staging_path", None) \
                or getattr(game, "get_mod_staging_path", None)
            staging = getter() if callable(getter) else None
            if staging:
                write_separator_color(_Path(staging), sep_name, color)
        except Exception as exc:
            # Cosmetic mirroring: profile_state already holds the real value.
            print(f"[gui_qt] separator colour write-back failed: {exc}", flush=True)

    def _save_sep_settings(self, sep_name, color, deploy):
        """Persist a separator's colour + deploy override, repaint the modlist,
        and rebuild the filemap (a deploy-path change reroutes its mods)."""
        profile_dir = self._gs.profile_dir()
        # Colour + deploy override — model drives the repaint (the deploy badge
        # painted on the separator row); profile_state persists them.
        self._modlist_model.set_sep_color(sep_name, color)
        self._modlist_model.set_sep_deploy_info(sep_name, deploy)
        if profile_dir is not None:
            try:
                from Utils.profile.profile_state import (
                    read_separator_colors,
                    read_separator_deploy_paths,
                    write_separator_colors,
                    write_separator_deploy_paths,
                )
                colors = read_separator_colors(profile_dir)
                if color:
                    colors[sep_name] = color
                else:
                    colors.pop(sep_name, None)
                write_separator_colors(profile_dir, colors)

                paths = read_separator_deploy_paths(profile_dir)
                if deploy:
                    paths[sep_name] = deploy
                else:
                    paths.pop(sep_name, None)
                write_separator_deploy_paths(profile_dir, paths)
            except Exception as exc:
                print(f"[gui_qt] separator settings save failed: {exc}",
                      flush=True)
        self._write_separator_color_to_staging(sep_name, color)
        self._modlist_view.viewport().update()
        # Deploy paths feed the filemap → rebuild so the routing takes effect.
        self._rebuild_conflicts_async()

    def _close_sep_settings_tab(self):
        if self._tabs.has_key("sep_settings"):
            self._tabs.close_tab("sep_settings")

    def _open_missing_reqs_tab(self, target):
        """Open the Missing Requirements panel over the plugins panel. *target* is
        a single mod name (str) or a set of names (multi-select). Triggered by the
        ⚠ flag click + the right-click 'Missing Requirements' item."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        names = [target] if isinstance(target, str) else list(target or ())
        from gui_qt.modlist.modlist_data import _parse_missing_req_pairs
        from Nexus.nexus_meta import nexus_mod_from_url, parse_external_reqs, read_meta
        # meta.ini keeps the FULL seeded requirement list on purpose (so a
        # requirement reappears if its mod is later removed) — filter out the
        # ones already installed here, exactly like the ⚠ flag pass does.
        # Without this, reopening the panel re-showed installed requirements.
        # Per-requirement IGNORED ids are NOT filtered — they stay visible in
        # the panel (with their Ignore box checked) so they can be un-ignored.
        installed_ids = self._installed_mod_ids()
        specs = []
        for name in names:
            meta = read_meta(staging / name / "meta.ini")
            raw = getattr(meta, "missing_requirements", "") or ""
            ids = {mid for mid, _ in _parse_missing_req_pairs(raw)}
            ids -= installed_ids
            # Off-Nexus requirements (e.g. a store's soft requirements): listed
            # with their link, never counted as missing.
            external = parse_external_reqs(
                getattr(meta, "external_requirements", "") or "")
            # An "off-site" entry that links a Nexus mod of this game and is
            # already installed has nothing left to show.
            external = [d for d in external
                        if not ((nm := nexus_mod_from_url(d.get("url", "")))
                                and nm[0] == domain.lower()
                                and nm[1] in installed_ids)]
            if not ids and not external:
                continue
            ignored_ids = {mid for mid, _ in _parse_missing_req_pairs(
                getattr(meta, "ignored_requirements", "") or "")}
            specs.append({"mod_name": name,
                          "mod_id": int(getattr(meta, "mod_id", 0) or 0),
                          "domain": getattr(meta, "game_domain", "") or domain,
                          "missing_ids": ids,
                          "external": external,
                          "ignored_ids": ignored_ids & ids})
        if not specs:
            self._notify(self.tr("No missing requirements."), "info")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return

        from Utils.profile.profile_state import (
            read_ignored_missing_requirements,
            write_ignored_missing_requirements,
        )
        pdir = self._gs.profile_dir()

        def _save_ignored(s):
            if pdir is not None:
                write_ignored_missing_requirements(pdir, set(s))

        ignored = (read_ignored_missing_requirements(pdir)
                   if pdir is not None else set())

        if self._tabs.has_key("missing_reqs"):
            self._tabs.close_tab("missing_reqs")
        from gui_qt.views.missing_reqs_view import MissingReqsView
        view = MissingReqsView(
            api, game, specs, ignored, _save_ignored,
            on_close=self._close_missing_reqs_tab, log_fn=self._append_log,
            install_fn=self._install_nexus_mod_by_id,
            ignore_req_fn=self._set_req_ignored,
            install_many_fn=self._install_nexus_mods_batch,
            stop_queue_fn=self._stop_req_batch)
        self._missing_reqs_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_missing_reqs_view", None))
        self._tabs.open_scoped_tab(
            view, self.tr("Missing Requirements"), self._plugins_panel_stack,
            key="missing_reqs")

    def _set_req_ignored(self, req_id: int, req_name: str, ignored: bool,
                         owner_names):
        """Per-requirement ignore from the Missing Requirements panel: add or
        remove *req_id* in the ignoredRequirements meta.ini field of each owning
        mod in *owner_names*, then re-run the light flag pass so the ⚠ flag
        updates immediately. Ignored ids stay in missingRequirements — the panel
        keeps listing them (checked) so they can be un-ignored later."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        from gui_qt.modlist.modlist_data import _parse_missing_req_pairs
        from Nexus.nexus_meta import read_meta, write_meta
        rid = int(req_id or 0)
        if rid <= 0:
            return
        changed = False
        for nm in owner_names or ():
            meta_path = staging / nm / "meta.ini"
            try:
                meta = read_meta(meta_path)
            except Exception:
                continue
            cur = dict(_parse_missing_req_pairs(
                getattr(meta, "ignored_requirements", "") or ""))
            if ignored:
                if rid in cur:
                    continue
                # ';' would corrupt the pair format (same rule the checkers use).
                cur[rid] = (req_name or "").replace(";", ",")
            else:
                if rid not in cur:
                    continue
                del cur[rid]
            meta.ignored_requirements = ";".join(
                f"{m}:{n}" for m, n in cur.items())
            try:
                write_meta(meta_path, meta)
                changed = True
            except Exception as exc:
                self._append_log(
                    f"[nexus] could not save ignored requirement for {nm}: {exc}")
        if changed:
            self._append_log(
                f"[nexus] {'ignored' if ignored else 'un-ignored'} requirement "
                f"'{req_name or rid}' for: {', '.join(owner_names)}")
            # Full pass (not a names= subset): the ⚠ two-pass needs installed
            # ids from ALL rows. Warm meta cache makes this cheap.
            self._refresh_modlist_flags()

    def _close_missing_reqs_tab(self):
        """Close the Missing Requirements panel. Only refresh flags when an Ignore
        toggle actually changed the ignored set (a plain open/close changes
        nothing) — and via the light flag path, not a full modlist reload."""
        view = getattr(self, "_missing_reqs_view", None)
        changed = bool(getattr(view, "ignored_changed", False))
        if self._tabs.has_key("missing_reqs"):
            self._tabs.close_tab("missing_reqs")
        if changed:
            # Re-read the on-disk ignored set the panel just wrote, then let the
            # light path recompute the ⚠ flag from it (no 500-file reload).
            pdir = self._gs.profile_dir()
            if pdir is not None:
                try:
                    from Utils.profile.profile_state import (
                        read_ignored_missing_requirements,
                    )
                    self._ignored_missing_reqs = frozenset(
                        read_ignored_missing_requirements(pdir))
                    self._modlist_view.ignored_missing_reqs = (
                        self._ignored_missing_reqs)
                except Exception:
                    pass
            self._refresh_modlist_flags()

    # ---- View Requirements (plugins-panel-scoped tab) -----------------------
    def _req_tab_active(self) -> bool:
        """True only while the View Requirements tab is OPEN AND on screen.

        A scoped tab that's been switched away from (another tab in the same
        panel is showing) is hidden by the tab widget — so requirement
        highlights should give way to the normal conflict highlights until the
        user switches back. Visibility is the exact signal for that."""
        view = getattr(self, "_view_requirements_view", None)
        return (view is not None
                and self._tabs.has_key("view_requirements")
                and view.isVisible())

    def _open_view_requirements_tab(self, name):
        """Open the View Requirements panel over the plugins panel (right-click
        item). Offline — it reads the full requirements list the update checker
        stores in meta.ini. While open it follows the modlist selection and the
        modlist tints the related mods purple (requires) / blue (required by)
        instead of the green/red conflict highlights.

        Seeds from the current selection so several right-clicked mods pool
        their requirements from the start; falls back to the clicked mod when
        it isn't part of the selection."""
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."),
                         "warning")
            return
        seed = self._modlist_view.selected_mod_names()
        seed = sorted(seed) if name in seed else [name]
        if self._tabs.has_key("view_requirements"):
            self._tabs.focus_key("view_requirements")
            view = getattr(self, "_view_requirements_view", None)
            if view is not None:
                view.show_mods(seed)
            return
        from gui_qt.views.requirements_view import RequirementsView
        view = RequirementsView(
            staging_fn=self._gs.staging_dir,
            on_close=self._close_view_requirements_tab,
            on_data_changed=self._apply_requirement_highlights,
            on_focus_changed=self._on_view_requirements_focus_changed,
            on_view_missing=self._view_requirements_open_missing,
            provided_fn=lambda name: game_provides_requirement(self._gs.game)(name))
        self._view_requirements_view = view
        # destroyed fires on EVERY teardown path (✕ button, tab-bar close,
        # game change) — restore normal highlights from the one place.
        view.destroyed.connect(lambda *_: self._on_view_requirements_gone())
        self._tabs.open_scoped_tab(
            view, self.tr("View Requirements"), self._plugins_panel_stack,
            key="view_requirements")
        # The showEvent from open_scoped_tab flips on the requirement highlights
        # via _on_view_requirements_focus_changed(True).
        view.show_mods(seed)

    def _view_requirements_open_missing(self, names):
        """View Missing Requirements button → open the Missing Requirements
        panel for the current selection. _open_missing_reqs_tab filters to mods
        that actually have missing requirements and toasts if none do (it also
        replaces the View Requirements tab, since both scope the plugins
        panel)."""
        if names:
            self._open_missing_reqs_tab(set(names))

    def _close_view_requirements_tab(self):
        if self._tabs.has_key("view_requirements"):
            self._tabs.close_tab("view_requirements")

    def _on_view_requirements_focus_changed(self, visible: bool):
        """The tab was shown or hidden (tab switch within the plugins panel).
        Shown → suppress the per-rebuild conflict reapply and paint requirement
        tints. Hidden → un-suppress and restore conflict highlights for the
        current selection."""
        self._modlist_view.suppress_conflict_highlights = bool(visible)
        if visible:
            self._apply_requirement_highlights()
        else:
            try:
                self._on_mod_selection_changed()
            except Exception:
                pass

    def _on_view_requirements_gone(self):
        """Post-teardown: drop the ref and restore the conflict highlights for
        the current selection (a plain _on_mod_selection_changed pass replaces
        the requirement sets — set_highlights swaps all five sets at once).

        The view's hideEvent normally restores highlights first, but teardown
        paths that delete the widget without a hide (or where the ref is already
        gone) still need this backstop."""
        self._view_requirements_view = None
        self._modlist_view.suppress_conflict_highlights = False
        try:
            self._on_mod_selection_changed()
        except Exception:
            pass

    def _apply_requirement_highlights(self):
        """Push the View Requirements tab's related-mod sets into the modlist
        as the purple/blue row tints (replaces any conflict tint). No-op when
        the tab isn't on screen — a background repopulate must not steal the
        modlist tints from the conflict highlights."""
        if not self._req_tab_active():
            return
        view = self._view_requirements_view
        self._modlist_model.set_highlights(
            requires=view.installed_requires,
            required_by=view.installed_required_by)

    # ---- Show Conflicts (full detachable tab) ------------------------------
    def _open_show_conflicts_tab(self, mod_name: str):
        """Open the conflict-detail view for *mod_name* as a full tab. The
        file-level data is computed on a worker thread inside the view (from
        filemap.txt + modindex.bin + bsa_index.bin — the app's ConflictData is
        only mod-level)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        cd = getattr(self, "_conflict_data", None)
        beaten = set()
        if cd is not None:
            beaten = (set(cd.overrides.get(mod_name, set()))
                      | set(cd.bsa_overrides.get(mod_name, set())))
        strip_prefixes = (getattr(game, "mod_folder_strip_prefixes", set())
                          | getattr(game, "mod_folder_strip_prefixes_post", set()))
        # UE pak mounting is not plugin-driven — archive winners follow the
        # engine's (_P boost, basename) mount order instead (empty plugin
        # order + archive_name_ordering below select that path).
        if getattr(game, "archive_plugin_ordering", True):
            plugin_order = [r.name for r in getattr(self._plugin_model, "_rows", [])
                            if getattr(r, "enabled", False)]
        else:
            plugin_order = []
        plugin_exts = frozenset(x.lower() for x in
                                (getattr(game, "plugin_extensions", []) or ()))
        from Utils.archives.ue_pak_reader import UE_ARCHIVE_EXTENSIONS
        archive_exts = frozenset(
            getattr(game, "archive_extensions", frozenset()) or frozenset())
        ctx = {
            "staging_root": staging,
            "profile_dir": self._gs.profile_dir(),
            "filemap_path": staging.parent / "filemap.txt",
            "modindex_path": staging.parent / "modindex.bin",
            "bsa_index_path": staging.parent / "bsa_index.bin",
            "strip_prefixes": strip_prefixes,
            "beaten_mods": beaten,
            "archive_exts": archive_exts,
            "plugin_order": plugin_order,
            "plugin_exts": plugin_exts,
            # UE paks resolve by (_P boost, basename) mount order.
            "archive_name_ordering": bool(archive_exts & UE_ARCHIVE_EXTENSIONS),
        }
        # Reuse one tab: rebuild it for the new mod if already open.
        if self._tabs.has_key("show_conflicts"):
            self._tabs.close_tab("show_conflicts")
        from gui_qt.views.show_conflicts_view import ShowConflictsView
        view = ShowConflictsView(
            mod_name, ctx,
            on_close=lambda: self._tabs.close_tab("show_conflicts"),
            log_fn=self._append_log)
        self._tabs.open_tab(view, self.tr("Conflicts: {0}").format(mod_name), key="show_conflicts")

    def _open_configure_game_tab(self, game, from_add_game: bool = False):
        """Open the (live) Configure-Game view as a detachable tab.

        *from_add_game* is True when reached via the Add-Game picker (a brand-new
        instance). In that case Save closes the tab (the add flow is done); when
        reconfiguring an already-configured game, Save keeps the tab open so the
        user can keep tweaking (only Remove/Cancel close it)."""
        from gui_qt.views.configure_game_view import ConfigureGameView

        def _done(saved: bool, removed: bool):
            # Reconfigure saves keep the tab open; adding a game (or removing an
            # instance, or cancelling) closes it.
            if removed or not saved or from_add_game:
                self._tabs.close_tab("configure_game")
            if saved or removed:
                # Refresh the game registry + selector; switch to the game if it
                # is now configured, else fall back to the current/ first game.
                from Utils.exe_launch.game_helpers import _load_games
                names = _load_games()
                self._gs.game_names = names
                # _load_games returns the ["No games configured"] sentinel when
                # nothing is configured — don't surface that as a menu item;
                # show the "Add game" prompt instead.
                real_names = [n for n in names if n != "No games configured"]
                if real_names:
                    self._game_selector.set_items(
                        real_names, current=self._gs.game_name)
                else:
                    self._game_selector.set_items([], current=self.tr("Add game"))
                if saved and game.name in names:
                    self._on_game_changed(game.name)
                    self._game_selector.set_current(game.name)
                    # profile_ini_files / profile_saves may have just been
                    # toggled — refresh the Open submenu (a same-game save is a
                    # no-op for _on_game_changed).
                    self._refresh_profile_actions()
                elif removed:
                    self._append_log(f"[game] removed instance: {game.name}")
                    # Don't leave the manager pointed at the now-removed game —
                    # switch to the first remaining configured game, or fall
                    # back to the Add-Game picker if none are left.
                    remaining = [n for n in names
                                 if n != game.name and n != "No games configured"]
                    if remaining:
                        fallback = remaining[0]
                        self._on_game_changed(fallback)
                        self._game_selector.set_current(fallback)
                    else:
                        self._open_add_game_tab()

            # Reconfigure kept the tab open — confirm the save inline and re-sync
            # its header (a per-profile override may have just been pinned).
            if saved and not removed and not from_add_game \
                    and self._tabs.has_key("configure_game"):
                v = getattr(self, "_configure_game_view", None)
                if v is not None:
                    try:
                        v.notify_saved()
                    except Exception:
                        pass

        page = ConfigureGameView(game, on_done=_done)
        self._configure_game_view = page
        self._tabs.open_tab(page, self._configure_tab_title(game),
                            key="configure_game")

    def _configure_tab_title(self, game) -> str:
        """Tab label for the configure view — includes the active profile name
        so the user can see which profile they're configuring."""
        verb = "Reconfigure" if game.is_configured() else "Add"
        prof = self._gs.profile
        if prof:
            return self.tr("{0} game — {1}").format(verb, prof)
        return self.tr("{0} game").format(verb)

    def _open_create_collection_tab(self):
        """Open the Create/Publish Collection panel scoped over the MODLIST
        panel (like Export Profile): builds a real Nexus/Vortex collection
        archive from the current modlist, exportable locally or publishable
        as a draft revision on Nexus."""
        if self._gs.game_name is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._tabs.has_key("create_collection"):
            self._tabs.focus_key("create_collection")
            return
        api = self._ensure_nexus_api()   # optional — publish needs it, export doesn't
        from gui_qt.views.create_collection_view import CreateCollectionView
        view = CreateCollectionView(self, game, api, log_fn=self._append_log)
        self._create_collection_view = view
        self._tabs.open_scoped_tab(
            view, self.tr("Create Collection"), self._modlist_panel_stack,
            key="create_collection")

    def _open_manifest_import(self, manifest, source_stem, *, bundle_zip="",
                              allow_append=False):
        """Shared import continuation: validate the manifest's game domain against
        the selected game, then open the collection detail tab (which drives the
        install pipeline). Used by both file-import and code-import. *allow_append*
        permits appending into an existing profile (safe only when there is no
        bundle — i.e. a code import)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return
        # Game-domain guard: v1 requires the selected game to match the manifest.
        man_domain = ((manifest.get("info") or {}).get("domainName") or "").strip()
        game_domain = (getattr(game, "nexus_game_domain", "") or "").strip()
        if man_domain and game_domain and man_domain.lower() != game_domain.lower():
            self._notify(
                self.tr("This profile targets '{0}', but the selected game is '{1}'. Switch games first, then import.").format(man_domain, game_domain), "warning")
            return

        # Build a bare NexusCollection + open the detail tab populated from the
        # manifest; the bundle zip (if a .mosaic) restores mods/profile after.
        from Nexus.nexus_api import NexusCollection
        name = (manifest.get("info") or {}).get("name") or source_stem
        collection = NexusCollection(
            id=0, slug=f"import_{source_stem}", name=name,
            game_domain=man_domain or game_domain)
        key = f"import_profile_{source_stem}"
        if self._tabs.has_key(key):
            self._tabs.focus_key(key)
            return
        from gui_qt.collections.collection_detail_view import CollectionDetailView
        view = CollectionDetailView(
            api, collection, game, log_fn=self._append_log,
            local_manifest=manifest, bundle_zip=bundle_zip,
            allow_append=allow_append)
        view.set_install_handler(
            lambda chosen, skipped, intent="install": self._install_collection(
                collection, view, chosen, skipped, intent))
        self._tabs.open_tab(view, self.tr("Import: {0}").format(name), key=key)

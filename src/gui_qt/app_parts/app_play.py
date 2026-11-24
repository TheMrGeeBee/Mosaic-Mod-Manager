"""Play bar, exe selector and custom/shared exe handling, launch and exe settings.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QWidget

from gui_qt.icons import hamburger_icon
from gui_qt.selector_button import SelectorButton
from gui_qt.theme.theme_qt import _c


class MainWindow:

    def _update_deployed_profile_highlight(self):
        """Green-highlight the deployed profile in the profile dropdown. Reads the
        same backend state the Tk app writes (game deploy-state JSON via
        get_deploy_active / get_last_deployed_profile)."""
        game = self._gs.game
        deployed = None
        try:
            if game is not None and game.get_deploy_active():
                deployed = game.get_last_deployed_profile()
        except Exception:
            deployed = None
        if hasattr(self, "_profile_selector"):
            self._profile_selector.set_highlighted_item(deployed)

    # ------------------------------------------------------------- play bar
    def _refresh_play_selector(self):
        """Repopulate the play-bar dropdown: the game + auto-detected framework
        launchers (installed script extenders) + custom exes, restoring this
        profile's saved selection (Tk stores the label in profile_state)."""
        game = self._gs.game
        if hasattr(self, "_version_picker"):
            self._version_picker.refresh(game)
        if game is None:
            self._play_exe_paths = {}
            self._play_auto_exe_names = set()
            self._play_shared_exe_names = set()
            self._play_exe_selector.set_items(["—"], current="—")
            return
        from Utils.exe_launch.exe_launch import (
            detect_framework_exes,
            load_custom_exes,
            load_shared_exes,
        )
        self._play_exe_paths = {}
        self._play_auto_exe_names = set()
        self._play_shared_exe_names = set()
        items = [game.name]
        # Framework states from the banner detect (worker) — lets staged-but-
        # not-deployed extenders show up too. Only when the cached map belongs
        # to this game+profile; detection is disk-only otherwise until the
        # banner's detect lands and re-refreshes us.
        key = (game.name, self._gs.profile)
        cached = getattr(self, "_framework_states", None)
        states = cached[1] if cached is not None and cached[0] == key else None
        for p in detect_framework_exes(game, states):
            if p.name not in self._play_exe_paths and p.name != game.name:
                self._play_exe_paths[p.name] = p
                self._play_auto_exe_names.add(p.name)
                items.append(p.name)
        for p in load_custom_exes(game):
            if p.name not in self._play_exe_paths and p.name != game.name:
                self._play_exe_paths[p.name] = p
                items.append(p.name)
        for p in load_shared_exes():
            if p.name not in self._play_exe_paths and p.name != game.name:
                self._play_exe_paths[p.name] = p
                self._play_shared_exe_names.add(p.name)
                items.append(p.name)
        current = game.name
        pdir = self._gs.profile_dir()
        if pdir is not None:
            from Utils.profile.profile_state import read_selected_exe
            saved = read_selected_exe(pdir)
            if saved in self._play_exe_paths:
                current = saved
        self._play_exe_selector.set_items(
            items, current=current, item_icons=self._build_exe_icon_map(game))
        self._update_play_btn_label(current)

    def _build_exe_icon_map(self, game) -> dict:
        """{label: QIcon} for the play-bar dropdown: the game's logo for the
        game entry, each custom exe's own extracted icon for the exe entries
        (falling back to the game logo when an exe has no embeddable icon).

        Icons are loaded at 2x the display size so the ~28px play-bar face + menu
        entries stay crisp on HiDPI (QIcon.paint downscales cleanly)."""
        px = 56
        icons: dict = {}
        game_logo = None
        try:
            from gui_qt.views.add_game_view import _game_logo
            pm = _game_logo(getattr(game, "game_id", "") or game.name, px)
            if pm is not None and not pm.isNull():
                from PySide6.QtGui import QIcon
                game_logo = QIcon(pm)
        except Exception:
            game_logo = None
        if game_logo is not None:
            icons[game.name] = game_logo
        from gui_qt.icons import exe_icon
        for name, path in self._play_exe_paths.items():
            ic = exe_icon(path, px)
            if ic is None:
                ic = game_logo
            if ic is not None:
                icons[name] = ic
        return icons

    def _update_play_btn_label(self, label: str):
        game = self._gs.game
        is_game = game is not None and label == game.name
        self._play_btn.setText(self.tr("▶  Play") if is_game else self.tr("▶  Run"))

    def _on_play_exe_selected(self, label):
        game = self._gs.game
        pdir = self._gs.profile_dir()
        if pdir is not None:
            from Utils.profile.profile_state import write_selected_exe
            is_game = game is not None and label == game.name
            write_selected_exe(pdir, None if is_game else label)
        self._update_play_btn_label(label)

    def _on_add_custom_exe(self):
        if self._gs.game is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        # Picker callback fires on the portal WORKER thread → marshal via Signal.
        from Utils.exe_launch.exe_launch import EXE_PICKER_FILTERS
        from Utils.wine_proton.portal_filechooser import pick_file
        pick_file("Select executable",
                  lambda p: self._custom_exe_picked.emit(p),
                  filters=EXE_PICKER_FILTERS)

    def _on_custom_exe_picked(self, path):
        game = self._gs.game
        if path is None or game is None:
            return
        from Utils.exe_launch.exe_launch import add_custom_exe
        # The list is stored on the active profile, so make sure the game object
        # still points at the profile the dropdown shows (a background worker can
        # leave it out of sync) — otherwise the add is silently dropped.
        self._gs.reassert_active_profile()
        if not add_custom_exe(game, path):
            self._notify(self.tr("Couldn't add {0}: no active profile to save it to.")
                         .format(path.name), "warning")
            return
        self._refresh_play_selector()
        if path.name in self._play_exe_paths:
            self._play_exe_selector.set_current(path.name)
            self._on_play_exe_selected(path.name)

    def _on_add_shared_exe(self):
        if self._gs.game is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        # Picker callback fires on the portal WORKER thread → marshal via Signal.
        from Utils.exe_launch.exe_launch import EXE_PICKER_FILTERS
        from Utils.wine_proton.portal_filechooser import pick_file
        pick_file("Select executable (shared across all games)",
                  lambda p: self._shared_exe_picked.emit(p),
                  filters=EXE_PICKER_FILTERS)

    def _on_shared_exe_picked(self, path):
        if path is None:
            return
        from Utils.exe_launch.exe_launch import add_shared_exe
        add_shared_exe(path)
        self._refresh_play_selector()
        if path.name in self._play_exe_paths:
            self._play_exe_selector.set_current(path.name)
            self._on_play_exe_selected(path.name)

    def _on_add_exe_from_staging(self):
        game = self._gs.game
        if game is None or not hasattr(game, "get_mod_staging_path"):
            self._notify(self.tr("No game selected."), "warning")
            return
        from Utils.exe_launch.exe_launch import scan_staging_exes
        exes = scan_staging_exes(game)
        if not exes:
            self._notify(self.tr("No executables found in staging."), "info")
            return
        # Label each exe with a dim relative-path hint so duplicate basenames
        # from different mods are distinguishable in the picker.
        root = game.get_mod_staging_path().parent
        items = []
        for p in exes:
            try:
                hint = str(p.parent.relative_to(root))
            except ValueError:
                hint = str(p.parent)
            items.append((f"{p.name}  —  {hint}", p))
        from gui_qt.overlays.staging_exe_picker_overlay import StagingExePickerOverlay
        StagingExePickerOverlay.show_over(
            self.centralWidget() or self, items,
            on_done=self._on_staging_exes_picked)

    def _on_add_exe_from_game_folder(self):
        game = self._gs.game
        game_path = (game.get_game_path()
                     if game is not None and hasattr(game, "get_game_path")
                     else None)
        if game_path is None:
            self._notify(self.tr("No game folder configured."), "warning")
            return
        from Utils.exe_launch.exe_launch import scan_game_folder_exes
        exes = scan_game_folder_exes(game)
        if not exes:
            self._notify(self.tr("No executables found in the game folder."),
                         "info")
            return
        # Same dim relative-path hint as the staging picker; root-level exes
        # get no hint (relative parent is ".").
        items = []
        for p in exes:
            try:
                hint = str(p.parent.relative_to(game_path))
            except ValueError:
                hint = str(p.parent)
            items.append((p.name if hint == "." else f"{p.name}  —  {hint}", p))
        from gui_qt.overlays.staging_exe_picker_overlay import StagingExePickerOverlay
        StagingExePickerOverlay.show_over(
            self.centralWidget() or self, items,
            on_done=self._on_staging_exes_picked,
            title=self.tr("Add executable from game folder"),
            subtitle=self.tr(
                "Check the executables to add to the Run menu. These run "
                "from their location in the game folder — including files "
                "deployed there by mods."))

    def _on_staging_exes_picked(self, paths):
        game = self._gs.game
        if not paths or game is None:
            return
        from Utils.exe_launch.exe_launch import add_custom_exe
        self._gs.reassert_active_profile()      # see _on_custom_exe_picked
        failed = [p.name for p in paths if not add_custom_exe(game, p)]
        if failed:
            self._notify(self.tr("Couldn't add {0}: no active profile to save it to.")
                         .format(", ".join(failed)), "warning")
        self._refresh_play_selector()
        # Select the single added exe (mirror the custom-exe picker); leave the
        # current selection alone when several were added at once.
        if len(paths) == 1 and paths[0].name in self._play_exe_paths:
            self._play_exe_selector.set_current(paths[0].name)
            self._on_play_exe_selected(paths[0].name)

    def _on_play(self):
        # An exception escaping a Qt slot only prints to stderr — invisible in
        # a flatpak/AppImage GUI launch, so the button would "do nothing".
        # Surface it in the log + a toast instead.
        try:
            self._do_play()
        except Exception as exc:
            import traceback
            self._append_log(f"Play error: {exc!r}")
            for ln in traceback.format_exc().strip().splitlines():
                self._append_log(f"Play error:   {ln}")
            self._notify(self.tr("Play failed — see log."), "error")

    def _do_play(self):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        from Utils.exe_launch import exe_launch
        label = self._play_exe_selector.current()
        exe_path = self._play_exe_paths.get(label)
        # First breadcrumb of every launch: proves the click reached the
        # handler and records which dropdown entry it resolved to.
        self._append_log(
            f"Play: pressed — game '{game.name}', entry '{label}'"
            + ("" if exe_path is None else f" ({exe_path})"))
        if exe_path is not None:
            # If this exe belongs to a wizard tool (xEdit, BodySlide, Script
            # Merger, …), open the wizard instead of a bare Proton launch — the
            # wizard handles the install/deploy/prefix setup a raw launch skips.
            # Only divert when a Qt view exists for the wizard; otherwise fall
            # through and launch the exe normally.
            from Utils.plugins.plugin_loader import wizard_tool_for_exe
            tool = wizard_tool_for_exe(game, exe_path.name)
            if tool is not None:
                from wizards_qt import get_spec
                if get_spec(tool.dialog_class_path) is not None:
                    self._open_wizard_tool(tool)
                    return
            # Custom exe → Proton in the game prefix (or per-exe override).
            is_auto = label in self._play_auto_exe_names
            can_deploy = hasattr(game, "deploy")
            if not exe_path.is_file() and not (is_auto and can_deploy):
                self._notify(self.tr("Executable not found: {0}").format(exe_path), "warning")
                return
            target = (exe_launch.launch_jar if exe_launch.is_jar(exe_path)
                      else exe_launch.launch_custom_exe)

            def _launch_exe():
                run_path = exe_path
                if not run_path.is_file():
                    # Staged extender materialised by the deploy that just
                    # ran — re-resolve so on-disk casing wins.
                    from Utils.wine_proton.framework_detect import resolve_file_ci
                    gp = game.get_game_path() if hasattr(game, "get_game_path") else None
                    resolved = None
                    if gp is not None:
                        try:
                            resolved = resolve_file_ci(gp, run_path.relative_to(gp))
                        except ValueError:
                            resolved = None
                    if resolved is None:
                        self._notify(self.tr("Executable not found: {0}").format(run_path),
                                     "warning")
                        return
                    run_path = resolved

                def _run(run_path=run_path):
                    # Worker-thread exceptions otherwise vanish to stderr.
                    try:
                        if hasattr(game, "pre_launch_repair"):
                            game.pre_launch_repair(log_fn=self._append_log)
                        if hasattr(game, "verify_and_repair_plugins_txt"):
                            game.verify_and_repair_plugins_txt(
                                self._gs.profile, log_fn=self._append_log)
                        target(run_path, game, log_fn=self._append_log)
                    except Exception as exc:
                        self._append_log(f"Run EXE error: {exc!r}")

                def _start():
                    threading.Thread(target=_run, daemon=True).start()
                # Only the script extender loads SKSE plugins; tools don't.
                if run_path.name.lower().startswith("skse"):
                    self._guard_bsa_memory_map(game, _start)
                else:
                    _start()

            # Auto-detected script extenders must run against the CURRENT
            # profile's files: deploy first when the exe is only staged (not
            # deployed yet) or when a different profile (or nothing) is the
            # one deployed. Otherwise the per-exe "deploy on run" setting
            # decides, exactly like a manual custom exe.
            force_deploy = False
            if is_auto and can_deploy:
                if not exe_path.is_file():
                    force_deploy = True
                else:
                    try:
                        deployed = (game.get_last_deployed_profile()
                                    if game.get_deploy_active() else None)
                    except Exception:
                        deployed = None
                    force_deploy = deployed != self._gs.profile

            # Deploy exactly like the Deploy button, then launch once the
            # (final, non-coalesced) deploy succeeds.
            if can_deploy and (force_deploy
                               or exe_launch.load_deploy_on_run(
                                   game, exe_launch.exe_settings_key(game, exe_path))):
                self._post_deploy_action = _launch_exe
                self._on_deploy()
            else:
                _launch_exe()
            return

        # Game entry → optional deploy first, then Steam/Heroic/Proton routing.
        def _launch():
            def _run():
                # Worker-thread exceptions otherwise vanish to stderr.
                try:
                    if hasattr(game, "pre_launch_repair"):
                        game.pre_launch_repair(log_fn=self._append_log)
                    if hasattr(game, "verify_and_repair_plugins_txt"):
                        game.verify_and_repair_plugins_txt(
                            self._gs.profile, log_fn=self._append_log)
                    exe_launch.launch_game(game, log_fn=self._append_log)
                except Exception as exc:
                    self._append_log(f"Play error: {exc!r}")
            # Enabled plugins with no file in Data/ (a deploy that is out of date)
            # crash on the loading screen with nothing to explain why — ask first.
            # BSA Memory Map under Proton breaks every SKSE hook after it.
            self._guard_missing_plugins(
                game, lambda: self._guard_bsa_memory_map(
                    game, lambda: threading.Thread(target=_run, daemon=True).start()))

        if exe_launch.load_deploy_before_launch(game) and hasattr(game, "deploy"):
            self._post_deploy_action = _launch
            self._on_deploy()
        else:
            _launch()

    def _on_play_action(self, which):
        game = self._gs.game
        if game is None:
            self._append_log(f"[play] {which}: no game selected")
            return
        if which == "folder":
            if not hasattr(game, "get_mod_staging_path"):
                self._append_log("[play] could not determine the Applications folder")
                return
            from Utils.xdg import xdg_open
            apps_dir = game.get_mod_staging_path().parent / "Applications"
            try:
                apps_dir.mkdir(parents=True, exist_ok=True)
                xdg_open(apps_dir)
            except Exception as e:
                self._append_log(f"[play] could not open Applications folder: {e}")
        elif which == "settings":
            label = self._play_exe_selector.current()
            exe_path = self._play_exe_paths.get(label)
            if exe_path is None:
                self._open_launcher_settings(game)
            else:
                self._open_exe_settings_tab(exe_path)

    def _open_launcher_settings(self, game):
        """Borderless overlay with the game-launch settings (Tk: game-exe
        branch of the Configure dialog)."""
        from gui_qt.overlays.launcher_settings_overlay import LauncherSettingsOverlay
        from Utils.exe_launch import exe_launch
        exe_key = exe_launch.game_exe_key(game)

        def _done(mode, deploy):
            if mode is None:
                return
            exe_launch.save_launch_mode(game, exe_key, mode)
            exe_launch.save_deploy_before_launch(game, deploy)
            self._append_log(f"[play] launch settings saved (via={mode}, "
                             f"deploy-before-launch={'on' if deploy else 'off'})")

        LauncherSettingsOverlay.show_over(
            self.centralWidget() or self,
            game_name=game.name,
            mode=exe_launch.load_launch_mode(game, exe_key),
            deploy=exe_launch.load_deploy_before_launch(game),
            on_done=_done,
        )

    def _open_exe_settings_tab(self, exe_path):
        """Per-exe settings as a plugins-panel-scoped tab (Tk: ExeConfigPanel
        overlay)."""
        game = self._gs.game
        if game is None:
            return
        if self._tabs.has_key("exe_settings"):
            self._tabs.close_tab("exe_settings")
            self._exe_settings_view = None
        from gui_qt.views.exe_settings_view import ExeSettingsView

        def _close(removed: bool):
            self._close_exe_settings_tab()
            if removed:
                # Drop a stale per-profile selection pointing at the removed exe.
                pdir = self._gs.profile_dir()
                if pdir is not None:
                    from Utils.profile.profile_state import (
                        read_selected_exe,
                        write_selected_exe,
                    )
                    if read_selected_exe(pdir) == exe_path.name:
                        write_selected_exe(pdir, None)
                self._refresh_play_selector()

        view = ExeSettingsView(
            game=game,
            exe_path=exe_path,
            on_close=_close,
            log_fn=self._append_log,
            is_auto=exe_path.name in self._play_auto_exe_names,
            is_shared=exe_path.name in self._play_shared_exe_names,
        )
        self._exe_settings_view = view
        self._tabs.open_scoped_tab(view, self.tr("Configure: {0}").format(exe_path.name),
                                   self._plugins_panel_stack, key="exe_settings")

    def _close_exe_settings_tab(self):
        if self._tabs.has_key("exe_settings"):
            self._tabs.close_tab("exe_settings")
        self._exe_settings_view = None

    def _play_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        # Exe selector stretches with the plugins panel: its left edge hugs the
        # splitter separator, so dragging the split resizes the dropdown while
        # Play + gear stay fixed at the right edge. Items = the game +
        # auto-detected framework launchers (installed script extenders) +
        # manually added custom exes, plus pickers fed by a staging scan (mod
        # tools + wizard tools installed under the profile) and a game-folder
        # scan (tools that must run from the game root, incl. deployed exes).
        self._play_exe_selector = SelectorButton(
            items=["—"],
            current="—",
            min_width=0,
            icon_px=28,   # bigger game/exe icon on the play-bar face + menu
            on_select=self._on_play_exe_selected,
            actions=[
                (self.tr("+ Add custom EXE…"), self._on_add_custom_exe),
                (self.tr("+ Add shared tool (all games)…"), self._on_add_shared_exe),
                (self.tr("+ Add exe from staging…"), self._on_add_exe_from_staging),
                (self.tr("+ Add exe from game folder…"),
                 self._on_add_exe_from_game_folder),
            ],
        )
        self._play_exe_selector.setFixedHeight(self._BTN_H)
        self._play_exe_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        h.addWidget(self._play_exe_selector, 1)
        self._play_exe_paths: dict[str, Path] = {}
        self._play_auto_exe_names: set[str] = set()

        # Steam build dropdown — hidden unless the game has a version table or
        # builds in Mosaic's store (so it's absent for most games). Switching
        # replaces the game's own files, so it refuses while mods are deployed.
        from gui_qt.game_version_picker import GameVersionPicker
        self._version_picker = GameVersionPicker(
            host=self, log_fn=self._append_log, notify=self._notify,
            confirm=self._confirm_version_switch, message=self._show_version_message,
            open_wizard=self._open_game_version_wizard,
            run_restore=self._wizard_run_restore,
            on_busy=lambda busy: self._set_deploy_buttons_enabled(not busy),
            height=self._BTN_H, parent=self)
        h.addWidget(self._version_picker.button)

        # ▶ Play — plain fixed-size button (label flips to "Run" for exes).
        self._play_btn = QPushButton(self.tr("▶  Play"))
        self._play_btn.setObjectName("PlayButton")
        self._play_btn.setFixedHeight(self._BTN_H)
        self._play_btn.setCursor(Qt.PointingHandCursor)
        self._play_btn.clicked.connect(self._on_play)
        h.addWidget(self._play_btn)

        # Exe menu — a hamburger (not a gear, to avoid reading as a second
        # settings button next to the header's gear). Its Settings action
        # depends on the dropdown selection (game → launcher settings overlay,
        # exe → per-exe settings tab).
        self._exe_settings_btn = SelectorButton(
            items=[],
            icon=hamburger_icon(self._ICON_PX, color=_c(self._pal, "TEXT_MAIN")),
            icon_px=self._ICON_PX,
            actions=[
                (self.tr("Settings"), lambda: self._on_play_action("settings")),
                (self.tr("Open application folder"), lambda: self._on_play_action("folder")),
            ],
        )
        self._exe_settings_btn.setFixedSize(self._BTN_H, self._BTN_H)
        h.addWidget(self._exe_settings_btn)
        return bar

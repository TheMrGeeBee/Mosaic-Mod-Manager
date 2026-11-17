"""App self-update check, handler/language sync and changelog/restart prompts.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextBrowser


class MainWindow:

    def _force_update_handler(self):
        """Re-download the active game's repo handler .json from the Resources
        branch right now (bypassing the sync cache) so the user doesn't have to
        wait for the next startup sync to pick up a repo fix."""
        from gui_qt.safe_emit import safe_emit
        from Utils.gh_sync import force_update_handler
        game = self._gs.game
        defn = getattr(game, "_defn", None) if game is not None else None
        if not isinstance(defn, dict):
            self._append_log("[game] no handler definition to update")
            return
        # Repo handlers are named <game_id>.json, but a local edit re-saves
        # under the game_id — try the on-disk name first, then the canonical.
        candidates = []
        src = defn.get("_source_file", "")
        if src:
            candidates.append(Path(src).name)
        gid_name = f"{game.game_id}.json"
        if gid_name not in candidates:
            candidates.append(gid_name)
        name = game.name
        self._notify(self.tr("Updating handler…"), "info")
        self._append_log(f"[game] force handler update: {name} ({candidates[0]})")
        force_update_handler(
            candidates,
            on_done=lambda status: safe_emit(
                self._handler_force_updated, name, status))

    def _start_gh_sync(self):
        """Kick off background sync of custom handlers + Qt plugins from GitHub."""
        from gui_qt.safe_emit import safe_emit
        from Utils.gh_sync import (
            sync_custom_handlers,
            sync_languages,
            sync_plugins,
        )
        # Handler downloads land on a worker thread; marshal the "refresh games"
        # signal to the UI thread. Plugin sync only touches the plugin cache
        # (discover_plugins) and picks up on the next tools-menu open — no UI
        # refresh needed here.
        sync_custom_handlers(on_changed=lambda: safe_emit(self._handlers_synced))
        sync_plugins()
        # UI translations (.qm) → config languages/ folder. New/updated ones
        # apply on next launch (Qt can't hot-swap installed translators safely).
        sync_languages(on_changed=lambda: safe_emit(self._languages_synced))

    def _check_for_app_update(self, force_downgrade_prompt: bool = False,
                              force_fresh: bool = False, manual: bool = False):
        """Run in background: fetch latest version and prompt if newer.

        AppImage installs compare against GitHub releases and offer the
        auto-installer.  System installs (e.g. AUR) compare against the AUR
        package version and show instructions to update via the AUR helper.

        When *force_downgrade_prompt* is True (e.g. the user just toggled the
        pre-release channel off while running a beta), the AppImage/Flatpak
        branches will surface the latest stable even if it's older than the
        currently-running version, with downgrade-aware copy.

        When *force_fresh* is True (or implied by *force_downgrade_prompt* or
        *manual*), the ETag-cache throttle is bypassed so a manual user action
        triggers an immediate re-check instead of waiting out the 1-hour
        throttle window.

        When *manual* is True (the Settings "Check for Updates" button), a
        sticky "Checking…" toast is shown up front and always resolved with an
        outcome — up to date, an error, or (briefly, alongside the full
        banner) that an update was found — instead of the automatic startup
        check's silence when there is nothing to report.
        """
        from gui_qt.safe_emit import safe_emit
        from Utils.ui_config import (
            load_allow_prerelease,
            load_update_notifications,
        )
        from Utils.version_check import (
            _fetch_aur_version,
            _fetch_latest_version,
            _is_newer_version,
            is_appimage,
            is_flatpak,
        )
        from version import __version__

        force_fresh = bool(force_fresh or force_downgrade_prompt or manual)

        # Opted out of update notifications (overlay checkbox / Settings):
        # skip the AUTOMATIC startup check (no API call, no banner). User-
        # initiated re-checks (pre-release toggle → force_fresh, or the
        # manual button) still run, so channel switching and a manual check
        # keep working while muted. The flatpak origin tidy-up must still
        # happen though — a fresh bundle install recreates its no-enumerate
        # origin remote and relies on startup to heal it.
        if not force_fresh and not load_update_notifications():
            if is_flatpak():
                def _polish_only():
                    from Utils.version_check import polish_flatpak_origin
                    polish_flatpak_origin()
                threading.Thread(target=_polish_only, daemon=True).start()
            return

        if manual:
            self._app_update_toast = self._notify(
                self.tr("Checking for updates…"), "info", sticky=True)

        def _manual_done(text: str, state: str):
            if manual:
                safe_emit(self._app_update_checked, text, state)

        def _do_check():
            allow_pre = load_allow_prerelease()
            if is_appimage() or is_flatpak():
                mode = "appimage" if is_appimage() else "flatpak"
                if mode == "flatpak":
                    # Idempotent tidy-up of a bundle-created origin remote
                    # (enumerate + proper title) so Discover shows real target
                    # versions instead of the branch name. Cheap; daemon thread.
                    from Utils.version_check import polish_flatpak_origin
                    polish_flatpak_origin()
                result = _fetch_latest_version(
                    allow_prerelease=allow_pre, force=force_fresh)
                if result is None:
                    _manual_done(self.tr("Could not check for updates — "
                                          "GitHub may be unreachable."), "warning")
                    return
                latest, is_pre = result
                newer = _is_newer_version(__version__, latest)
                if newer or (force_downgrade_prompt and latest != __version__):
                    # GitHub Releases decides the *version strings*, but for a
                    # remote-tracked flatpak the *install* comes from our OSTree
                    # remote — which can lag Pages publishing or lack the beta
                    # branch entirely. Only show the banner when the remote can
                    # actually deliver; None (host unreachable) falls back to
                    # the GitHub-only decision.
                    if mode == "flatpak":
                        from Utils.version_check import (
                            flatpak_installed_from_remote,
                            flatpak_remote_update_ready,
                        )
                        if flatpak_installed_from_remote():
                            # Checkbox = channel = OSTree branch. Stables are
                            # published to BOTH branches (the beta branch's
                            # contract is "newest release, beta or stable"), so
                            # beta-channel users get new stables as ordinary
                            # beta-branch updates — no branch hopping needed.
                            branch = "beta" if allow_pre else "stable"
                            if flatpak_remote_update_ready(branch) is False:
                                _manual_done(self.tr(
                                    "{0} is out on GitHub, but not published "
                                    "to your update channel yet.").format(latest),
                                    "info")
                                return
                    _manual_done(self.tr("Update available: {0}").format(latest),
                                 "success")
                    safe_emit(self._app_update_found,
                              (__version__, latest, mode, is_pre, not newer))
                else:
                    _manual_done(self.tr("Mosaic is up to date ({0}).").format(
                        __version__), "info")
            else:
                aur_ver = _fetch_aur_version(force=force_fresh)
                if aur_ver is None:
                    _manual_done(self.tr("Could not check for updates — "
                                          "the AUR may be unreachable."), "warning")
                    return
                if _is_newer_version(__version__, aur_ver):
                    _manual_done(self.tr("Update available: {0}").format(aur_ver),
                                 "success")
                    safe_emit(self._app_update_found,
                              (__version__, aur_ver, "aur", False, False))
                else:
                    _manual_done(self.tr("Mosaic is up to date ({0}).").format(
                        __version__), "info")

        threading.Thread(target=_do_check, daemon=True).start()

    def _on_app_update_checked(self, text: str, state: str):
        """Resolve the manual "Check for Updates" sticky toast (UI thread)."""
        toast = self._app_update_toast
        self._app_update_toast = None
        if toast is not None:
            toast.dismiss(text, state=state)

    def _on_app_update_found(self, payload):
        """Show the update banner over the modlist panel (UI thread)."""
        current, latest, mode, is_prerelease, is_downgrade = payload
        from gui_qt.overlays.update_overlay import UpdateOverlay

        # Dismiss any prior banner so re-checking (e.g. after toggling the
        # pre-release setting) doesn't stack multiple overlays.
        prior = getattr(self, "_update_overlay", None)
        if prior is not None:
            try:
                prior.close_overlay()
            except Exception:
                pass
            self._update_overlay = None

        def _on_update():
            if mode == "flatpak":
                from Utils.ui_config import load_allow_prerelease
                from Utils.version_check import (
                    flatpak_installed_from_remote,
                    run_flatpak_installer,
                    update_flatpak_from_remote,
                )
                # Checkbox = channel = branch (stables are published to both
                # branches, so the beta branch always carries the newest
                # release — the banner's offer is deliverable either way).
                allow_pre = load_allow_prerelease()
                if flatpak_installed_from_remote():
                    status = update_flatpak_from_remote(
                        allow_prerelease=allow_pre)
                else:
                    status = ("launched" if run_flatpak_installer(latest)
                              else "unavailable")
                if status == "no-branch":
                    # The requested channel isn't on the remote yet (e.g. beta
                    # before the first beta release is published). The detached
                    # install would fail silently — tell the user instead.
                    channel = self.tr("beta") if allow_pre else self.tr("stable")
                    self._notify(self.tr(
                        "The {0} channel isn't published on the update remote "
                        "yet — try again after the next {0} release.").format(
                            channel), state="warning")
                    return
                if status != "launched":
                    # Host flatpak unreachable — fall back to the releases page
                    # rather than silently closing with nothing happening.
                    from Utils.version_check import _APP_UPDATE_RELEASES_URL
                    from Utils.xdg import open_url
                    open_url(_APP_UPDATE_RELEASES_URL)
                    return
            else:
                from Utils.version_check import run_installer
                run_installer(allow_prerelease=is_prerelease)
            # closeEvent handles the rest of the shutdown (NxmIPC etc.); the
            # installer waits 2s before replacing the running app.
            self.close()

        def _cleared(overlay):
            if getattr(self, "_update_overlay", None) is overlay:
                self._update_overlay = None

        self._update_overlay = UpdateOverlay(
            self._modlist_panel_stack, current, latest, mode=mode,
            is_prerelease=is_prerelease, is_downgrade=is_downgrade,
            on_update=_on_update, on_close=_cleared)

    def _prompt_ui_scale_restart(self):
        """Offer a self-restart after the UI scale changed (Settings). Qt reads
        QT_SCALE_FACTOR only at QApplication construction, so the new scale can
        only apply on a fresh launch. The value is already persisted by the
        caller; confirm, then restart on OK — on Cancel it applies next launch."""
        try:
            from gui_qt.overlays.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self,
                self.tr("Restart to change UI scale?"),
                self.tr("The UI scale change takes effect after a restart. "
                        "Restart now?"),
                lambda ok: self._request_restart() if ok else None,
                confirm_label=self.tr("Restart now"),
                cancel_label=self.tr("Later"),
                danger=False,
            )
        except Exception:
            self._request_restart()

    def _prompt_theme_restart(self):
        """Offer a self-restart after the theme changed (Settings). The palette/
        QSS is applied once at startup, so a theme change only takes full effect
        on a fresh launch. The value is already persisted by the caller; confirm,
        then restart on OK — on Cancel it applies next launch."""
        try:
            from gui_qt.overlays.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self,
                self.tr("Restart to change theme?"),
                self.tr("The theme change takes effect after a restart. "
                        "Restart now?"),
                lambda ok: self._request_restart() if ok else None,
                confirm_label=self.tr("Restart now"),
                cancel_label=self.tr("Later"),
                danger=False,
            )
        except Exception:
            self._request_restart()

    def _prompt_language_restart(self):
        """Offer a self-restart after the language changed (Settings picker).
        Alias for _apply_language's confirm flow — the choice is already
        persisted by the caller."""
        self._apply_language(None)

    def _apply_language(self, code: str):
        """Switch UI language (from the Settings/onboarding picker). A live
        in-place retranslate isn't feasible (the whole UI is built once), so we
        cleanly self-restart. The choice is already persisted by the caller;
        confirm first (a restart is disruptive) then restart on OK — on Cancel
        the new language simply applies at the next manual launch."""
        try:
            from gui_qt.overlays.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self,
                self.tr("Restart to change language?"),
                self.tr("The language change takes effect after a restart. "
                        "Restart now?"),
                lambda ok: self._request_restart() if ok else None,
                confirm_label=self.tr("Restart now"),
                cancel_label=self.tr("Later"),
                danger=False,
            )
        except Exception:
            # If the overlay can't show for any reason, fall back to restarting.
            self._request_restart()

    def _sync_languages_now(self):
        """Manual language sync (from onboarding / Settings): force a fetch from
        the Resources branch, then refresh pickers via _languages_synced."""
        from gui_qt.safe_emit import safe_emit
        from Utils.gh_sync import sync_languages
        try:
            self._notify(self.tr("Syncing language files…"), "info")
        except Exception:
            pass
        sync_languages(
            on_changed=lambda: safe_emit(self._languages_synced), force=True)

    def _on_handlers_synced(self):
        """A background handler sync wrote new .json files — reload the registry
        so an open Add-Game picker (and the game selector) sees them, and pull
        down any missing custom-game banner images."""
        try:
            from Utils.exe_launch.game_helpers import _GAMES, _load_games
            _load_games()
        except Exception:
            return
        # Refresh an open Add-Game tab IN PLACE (don't close+reopen — that
        # deletes the view while its scan/image threads may still emit).
        view = self._tabs._keys.get("add_game") if hasattr(self._tabs, "_keys") else None
        if view is not None and hasattr(view, "refresh_games"):
            try:
                view.refresh_games(dict(_GAMES))
            except Exception:
                pass
        # Now that the definitions are on disk, download their banner images.
        self._download_custom_game_images(view)

    def _on_languages_synced(self):
        """A background/manual language sync wrote new/updated .qm files into the
        config languages/ folder. Refresh any open language picker (Settings tab
        or the onboarding page) so newly-available languages show up; the active
        language only fully changes on restart."""
        for view in (
            (self._tabs._keys.get("settings")
             if hasattr(self._tabs, "_keys") else None),
            getattr(self, "_onboarding_view", None),
        ):
            try:
                if view is not None and hasattr(view, "refresh_language_options"):
                    view.refresh_language_options()
            except Exception:
                pass
        try:
            self._notify(
                self.tr("Language files updated — restart to apply."), "info")
        except Exception:
            pass

    def _find_changelog_file(self):
        """Locate the bundled Changelog.txt across the from-source and
        packaged (flatpak / AppImage) layouts.

        From source it lives at the repo root — a level above ``src/`` (which
        holds ``gui_qt``). In the flatpak/AppImage the tree is flattened, so it
        sits right beside ``gui_qt``. Check both, plus the current working dir
        (the launcher ``cd``s into the share dir before running)."""
        here = Path(__file__).resolve().parent  # .../gui_qt
        candidates = [
            here.parent / "Changelog.txt",         # flattened (flatpak/appimage)
            here.parent.parent / "Changelog.txt",  # from source (repo root)
            Path.cwd() / "Changelog.txt",
        ]
        for c in candidates:
            if c.is_file():
                return c
        return None

    def _open_changelog_tab(self):
        """Open the bundled Changelog.txt as a read-only detachable tab."""
        if self._tabs.has_key("changelog"):
            self._tabs.focus_key("changelog")
            return
        path = self._find_changelog_file()
        if path is not None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                text = self.tr("Could not read the changelog:\n{0}").format(exc)
        else:
            text = self.tr("Changelog file not found.")
        view = QTextBrowser()
        view.setReadOnly(True)
        view.setObjectName("LogView")
        view.setOpenExternalLinks(True)
        # Render the changelog as Markdown (## headings, - bullets, etc.)
        # with a slightly larger base font for readability.
        font = view.font()
        font.setPointSizeF(font.pointSizeF() + 2)
        view.setFont(font)
        view.setMarkdown(self._changelog_to_markdown(text))
        view.moveCursor(QTextCursor.Start)
        self._tabs.open_tab(view, self.tr("Changelog"), key="changelog")

    @staticmethod
    def _changelog_to_markdown(text):
        """Adapt the release-format Changelog.txt for Markdown rendering
        without editing the file itself.

        Version entries are written as ``- vX.Y.Z`` bullets (a format the
        release workflow depends on).  Turn those specific lines into level-1
        headings so they render as prominent titles, leaving every other line
        untouched.
        """
        version_re = re.compile(r"^\s*-\s*(v\d+(?:\.\d+)*)\s*$")
        out = []
        for line in text.splitlines():
            m = version_re.match(line)
            if m:
                # Blank line before the heading so Markdown doesn't fold it
                # into the preceding bullet list.
                if out and out[-1].strip():
                    out.append("")
                out.append("# {0}".format(m.group(1)))
            else:
                out.append(line)
        return "\n".join(out)

    # ------------------------------------------------------ social buttons
    def _open_github(self):
        from Utils.xdg import open_url
        open_url("https://github.com/TheMrGeeBee/Mosaic-Mod-Manager")

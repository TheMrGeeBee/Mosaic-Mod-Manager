"""Nexus account/login, NXM link handling (IPC, argv, downloads) and the Mosaic endorse button.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import sys
import threading

from PySide6.QtCore import Qt

from gui_qt.theme.theme_qt import _c, active_palette, contrast_text

# Mosaic Mod Manager's own Nexus Mods listing (a "site" mod, not tied to a
# specific game) — https://www.nexusmods.com/site/mods/2139. Also referenced
# as the NEXUS_MOD_ID repo variable in .github/workflows/release.yml.
_MOSAIC_NEXUS_DOMAIN = "site"
_MOSAIC_NEXUS_MOD_ID = 2139


class MainWindow:

    def _ensure_nexus_api(self):
        """Build the shared NexusAPI from saved OAuth tokens (idempotent) and
        kick off a background validate() to learn the username. Returns the API
        or None (not logged in / connection failed). Reused by the footer and
        the Nexus browser tab so there's a single instance whose passively
        captured rate limits the footer can read."""
        if getattr(self, "_nexus_api", None) is not None:
            return self._nexus_api
        from Nexus.nexus_api import NexusAPI
        from Nexus.nexus_oauth import (
            OAuthRefreshError,
            clear_oauth_tokens,
            load_oauth_tokens,
        )
        tokens = load_oauth_tokens()
        if tokens is None:
            return None
        try:
            self._nexus_api = NexusAPI.from_oauth(tokens)
        except OAuthRefreshError as exc:
            self._append_log(f"[nexus] api init failed: {exc}")
            # A dead (rotated-out / revoked) refresh token wedges every future
            # launch — clear it so the next attempt starts from a clean login.
            # A transient network failure leaves the token in place to retry.
            if exc.token_revoked:
                clear_oauth_tokens()
                self._append_log("[nexus] cleared expired login — please log in again")
                self._notify(
                    self.tr("Your Nexus session expired — please log in again "
                            "(Nexus ▸ Login to Nexus)."),
                    "warning",
                )
            return None
        except Exception as exc:
            self._append_log(f"[nexus] api init failed: {exc}")
            return None
        # Validate off-thread (one rate-limited call; result cached 5 min).
        import threading

        def _worker():
            name = None
            try:
                user = self._nexus_api.validate()
                name = user.name
                # Record premium status so the collection-install premium gate
                # has a last-known fallback if its own validate() errors (GH#278).
                try:
                    from Utils.ui_config import save_nexus_last_premium
                    save_nexus_last_premium(bool(getattr(user, "is_premium", False)))
                except Exception:
                    pass
            except Exception as exc:
                self._append_log(f"[nexus] validate failed: {exc}")
            self._nexus_validated.emit(name)
        threading.Thread(target=_worker, daemon=True).start()
        return self._nexus_api

    def _on_nexus_validated(self, name):
        """Worker reported the validated username (or None) — update the footer."""
        if name:
            self._append_log(f"[nexus] logged in as {name}")
            self._check_mosaic_endorsed()
        if hasattr(self, "_nexus_footer"):
            self._nexus_footer.set_username(name)

    def _check_mosaic_endorsed(self):
        """Background: does the logged-in user already endorse Mosaic's own
        Nexus listing? Fetched once per login (not polled) to set the
        Endorse button's initial label/state."""

        def _worker():
            endorsed = False
            error = None
            try:
                for e in self._nexus_api.get_endorsements() or []:
                    if (str(e.get("domain_name", "")) == _MOSAIC_NEXUS_DOMAIN
                            and int(e.get("mod_id", 0) or 0) == _MOSAIC_NEXUS_MOD_ID
                            and str(e.get("status", "")).lower() == "endorsed"):
                        endorsed = True
                        break
            except Exception as exc:
                error = str(exc)
            self._mosaic_endorse_done.emit(
                {"endorsed": endorsed, "notify": False, "error": error})
        threading.Thread(target=_worker, daemon=True, name="mosaic-endorse-check").start()

    def _on_endorse_mosaic(self):
        """Endorse (or abstain from) Mosaic Mod Manager's own Nexus listing —
        the log bar has no 'currently selected mod' context, unlike the
        modlist's per-mod Endorse (see _on_modlist_endorse), so this always
        targets Mosaic's own page."""
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus."), "warning")
            return
        new_state = not getattr(self, "_mosaic_endorsed", False)

        def _worker():
            try:
                if new_state:
                    api.endorse_mod(_MOSAIC_NEXUS_DOMAIN, _MOSAIC_NEXUS_MOD_ID)
                else:
                    api.abstain_mod(_MOSAIC_NEXUS_DOMAIN, _MOSAIC_NEXUS_MOD_ID)
                self._mosaic_endorse_done.emit(
                    {"endorsed": new_state, "notify": True, "error": None})
            except Exception as exc:
                self._mosaic_endorse_done.emit({
                    "endorsed": getattr(self, "_mosaic_endorsed", False),
                    "notify": True, "error": str(exc),
                })
        threading.Thread(target=_worker, daemon=True, name="endorse-mosaic").start()

    def _on_mosaic_endorse_done(self, payload):
        """UI thread: apply the Endorse-Mosaic result (silent state refresh,
        or a user-initiated click — see the 'notify' flag)."""
        error = payload.get("error")
        notify = payload.get("notify")
        if error:
            if notify:
                self._notify(
                    self.tr("Nexus: couldn't update endorsement — {0}").format(error),
                    "warning")
            else:
                self._append_log(f"[nexus] Mosaic endorsement check failed: {error}")
            return
        self._mosaic_endorsed = bool(payload.get("endorsed"))
        self._refresh_endorse_mosaic_style()
        if notify:
            msg = (self.tr("Endorsed Mosaic Mod Manager on Nexus — thank you!")
                   if self._mosaic_endorsed else self.tr("Endorsement removed."))
            self._notify(msg, "success")

    def _refresh_endorse_mosaic_style(self):
        """Style _endorse_mosaic_btn from self._mosaic_endorsed: a colored
        call-to-action ("♥ Endorse") when not yet endorsed, or the same flat
        chrome as the neighboring Log/Changelog/Github buttons — plus a
        green checkmark emoji, which renders in its own fixed color
        regardless of the button's (now-neutral) text color — once it is.
        Endorsing is a toggle (clicking again abstains), so this re-derives
        the look from state every time rather than setting it once."""
        btn = getattr(self, "_endorse_mosaic_btn", None)
        if btn is None:
            return
        if getattr(self, "_mosaic_endorsed", False):
            btn.setText(self.tr("✅ Endorsed"))
            btn.setStyleSheet("")
            btn.setObjectName("FooterButton")
        else:
            btn.setText(self.tr("♥ Endorse"))
            color = _c(active_palette(), "BTN_SUCCESS")
            fg = contrast_text(color)
            btn.setObjectName("")
            btn.setStyleSheet(
                f"QPushButton{{background:{color}; color:{fg}; border:none;"
                f" padding:4px 10px; border-radius:4px; font-size:12px;"
                f" font-weight:600;}}"
                f"QPushButton:hover{{background:{color};}}")
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _handle_nxm_argv(self):
        """Check sys.argv for --nxm <url> and kick off a download once the
        window has finished building."""
        if "--nxm" not in sys.argv:
            return
        try:
            idx = sys.argv.index("--nxm")
            nxm_url = sys.argv[idx + 1]
        except (IndexError, ValueError):
            return
        from PySide6.QtCore import QTimer

        from Nexus.nxm_handler import nxm_log
        nxm_log("Fresh instance: processing --nxm link after window build")
        QTimer.singleShot(500, lambda: self._process_nxm_link(nxm_url))

    def _start_nxm_ipc(self):
        """Start the IPC server so this (running) instance receives NXM links
        handed off by later instances. The callback fires on a worker thread,
        so it emits _nxm_received to marshal onto the UI thread."""
        from gui_qt.safe_emit import safe_emit
        from Nexus.nxm_handler import NxmIPC

        def _on_nxm(url: str):
            safe_emit(self._nxm_received, url)

        NxmIPC.start_server(_on_nxm)

    def _receive_nxm(self, nxm_url: str):
        """UI thread: handle a message delivered via IPC from a second launch
        — either a real nxm:// link (from --nxm at startup or a second
        instance) or an empty-string focus ping (a plain relaunch while
        already running). Either way, raise the window so the user sees it;
        only a non-empty url is actually processed as a download."""
        from Nexus.nxm_handler import nxm_log
        if nxm_url:
            nxm_log("NXM link reached UI thread of running instance")
            self._append_log("[nexus] received NXM link from browser")
        else:
            nxm_log("Focus ping reached UI thread of running instance")
        try:
            self.setWindowState(
                self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
            self.raise_()
            self.activateWindow()
        except Exception:
            pass
        if nxm_url:
            self._process_nxm_link(nxm_url)

    def _process_nxm_link(self, nxm_url: str):
        """Handle an nxm:// link — download a mod or open a collection."""
        from Nexus.nxm_handler import nxm_log, parse_nxm_url
        nxm_log(f"Processing NXM link: {nxm_url}")
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            nxm_log("NXM link ignored — not logged in to Nexus")
            self._append_log("[nexus] NXM link ignored — not logged in")
            return

        try:
            mod_link, coll_link = parse_nxm_url(nxm_url)
        except ValueError as exc:
            nxm_log(f"Bad nxm:// URL — {exc}")
            self._append_log(f"[nexus] bad nxm:// URL — {exc}")
            self._notify(self.tr("Received a malformed NXM link."), "warning")
            return

        if coll_link is not None:
            self._process_nxm_collection_link(coll_link)
            return

        link = mod_link
        # If the Nexus browser / Change Version tab is watching the download
        # folders for this mod (non-premium install), the user picked
        # 'Download with Mod Manager' instead — this flow installs it, so
        # stop the watch (double install).
        for _attr in ("_nexus_view", "_change_version_view"):
            _v = getattr(self, _attr, None)
            if _v is not None:
                try:
                    _v.cancel_manual_watch(link.mod_id)
                except Exception:
                    pass
        # Same for a pending non-premium reinstall watch of this mod.
        try:
            self.cancel_app_manual_watch(link.mod_id)
        except Exception:
            pass
        self._append_log(
            f"[nexus] downloading mod {link.mod_id} file {link.file_id} "
            f"from {link.game_domain}…")

        matched = self._match_game_for_domain(link.game_domain)
        if matched and matched[0] != self._gs.game_name:
            self._on_game_changed(matched[0])
            self._append_log(f"[nexus] switched to game '{matched[0]}'")

        self._notify(self.tr("Downloading mod from Nexus…"), "info")
        dl_key = self._new_dl_key()
        self._nexus_download_progress(dl_key, "", 0, 0)   # show popup immediately


        def _worker():
            from gui_qt.safe_emit import safe_emit
            from Nexus.nexus_download import NexusDownloader
            from Utils.config_paths import get_download_cache_dir_for_game
            # Fetch mod + file info for accurate meta.ini (best-effort).
            mod_info = file_info = None
            try:
                mod_info, file_info = api.get_mod_and_file_info_graphql(
                    link.game_domain, link.mod_id, link.file_id)
            except Exception as exc:
                self._op_log.emit(
                    f"[nexus] could not fetch mod info ({exc}) — meta partial")
            dest_name = matched[0] if matched else (self._gs.game_name or "")
            dest = get_download_cache_dir_for_game(dest_name)
            dl_label = getattr(file_info, "file_name", "") or ""
            downloader = NexusDownloader(api, download_dir=dest)
            result = downloader.download_from_nxm(
                link, dest_dir=dest,
                known_file_name=dl_label,
                progress_cb=lambda d, t: safe_emit(
                    self._req_install_prog, dl_key, dl_label, int(d), int(t)))
            safe_emit(self._nxm_download_done,
                      (result, mod_info, file_info, dl_key))

        threading.Thread(target=_worker, daemon=True, name="nxm-download").start()

    def _on_nxm_download_done(self, payload):
        """UI thread: an NXM download finished. Install it (via the shared
        install pipeline) with a prebuilt meta from the link data."""
        result, mod_info, file_info, dl_key = payload
        self._nexus_download_progress(dl_key, "", 0, -1)   # hide this download's card
        if not (result.success and result.file_path):
            self._append_log(f"[nexus] download failed — {result.error}")
            self._notify(self.tr("Nexus download failed — {0}").format(result.error), "error")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._append_log(
                f"[nexus] downloaded {result.file_name} — no configured game "
                "selected; install manually from Downloads.")
            self._notify(self.tr("Downloaded — no game selected; see Downloads tab."),
                         "warning")
            return

        from Nexus.nexus_meta import build_meta_from_download
        meta = None
        try:
            meta = build_meta_from_download(
                game_domain=result.game_domain, mod_id=result.mod_id,
                file_id=result.file_id, archive_name=result.file_name,
                mod_info=mod_info, file_info=file_info)
        except Exception as exc:
            self._append_log(f"[nexus] could not build metadata: {exc}")

        if result.from_cache:
            self._append_log(
                f"[nexus] using the archive already downloaded: {result.file_name}")
        path = str(result.file_path)
        metas = {path: meta} if meta is not None else None
        self._install_paths([path], metas=metas)

    def _process_nxm_collection_link(self, coll_link):
        """Switch to the matching game and open the collection's detail tab."""
        self._append_log(
            f"[nexus] opening collection '{coll_link.slug}' "
            f"from {coll_link.game_domain}")
        matched = self._match_game_for_domain(coll_link.game_domain)
        if not matched:
            self._notify(
                self.tr("No configured game for Nexus domain '{0}'.").format(coll_link.game_domain), "warning")
            self._append_log(
                f"[nexus] no configured game for domain "
                f"'{coll_link.game_domain}' — cannot open collection")
            return
        if getattr(matched[1], "collections_disabled", False):
            self._notify(
                self.tr("Collections aren't supported for '{0}'.").format(matched[0]), "warning")
            return
        if matched[0] != self._gs.game_name:
            self._on_game_changed(matched[0])
            self._append_log(f"[nexus] switched to game '{matched[0]}'")

        from Nexus.nexus_api import NexusCollection
        collection = NexusCollection(
            slug=coll_link.slug, game_domain=coll_link.game_domain)
        revision = coll_link.revision_id or None
        self._open_collection_detail_tab(collection, revision_number=revision)

    def _nexus_login_sso(self):
        """Start the browser OAuth flow. Keeps the client on self so the
        'Paste login code' fallback can complete the same session.

        A previous attempt (abandoned tab, browser never completed the
        redirect, etc.) can still be 'running' — rather than permanently
        blocking every future login until its 5-minute timeout elapses,
        cancel it (releases the local callback port) and start clean. This
        is what a user clicking Login again clearly wants."""
        from Nexus.nexus_oauth import CLIENT_ID, NexusOAuthClient
        if not CLIENT_ID:
            self._notify(self.tr("Nexus login is unavailable in this build."), "warning")
            return
        if self._oauth_client is not None and self._oauth_client.is_running:
            self._oauth_client.cancel()
        self._oauth_client = NexusOAuthClient(
            on_token=lambda t: self._oauth_event.emit("token", t),
            on_error=lambda m: self._oauth_event.emit("error", m),
            on_status=lambda m: self._oauth_event.emit("status", m),
        )
        self._oauth_client.start()
        self._notify(self.tr("Opening browser to log in to Nexus Mods…"), "info")

    def _nexus_paste_code(self):
        """Fallback when the localhost redirect was blocked: paste the Base64
        code from the Nexus 'Having issues?' page into the live login session."""
        if self._oauth_client is None or not self._oauth_client.is_running:
            self._notify(self.tr("Start 'Login via SSO' first, then paste the code."),
                         "warning")
            return
        def _pasted(blob):
            if not blob or not blob.strip():
                return
            if self._oauth_client is None or not self._oauth_client.is_running:
                self._notify(self.tr("The login session has ended — start "
                             "'Login via SSO' again."), "warning")
                return
            ok2, msg = self._oauth_client.submit_manual_code(blob)
            self._notify(msg, "info" if ok2 else "warning")

        from gui_qt.overlays.text_input_overlay import TextInputOverlay
        TextInputOverlay.show_over(
            self, "Paste Nexus login code",
            "Paste the code from the Nexus 'Having issues?' page:", _pasted,
            ok_label=self.tr("Submit"))

    def _nexus_clear_credentials(self):
        """Forget the saved OAuth tokens + legacy API key."""
        from Nexus.nexus_api import clear_api_key
        from Nexus.nexus_oauth import clear_oauth_tokens
        clear_api_key()
        clear_oauth_tokens()
        self._nexus_api = None
        # Also tear down any in-flight login attempt so a fresh SSO login
        # (e.g. testing a different account) never gets stuck behind a
        # lingering one — cancel() releases the local callback port.
        if self._oauth_client is not None:
            self._oauth_client.cancel()
            self._oauth_client = None
        if hasattr(self, "_nexus_footer"):
            self._nexus_footer.set_username(None)
        self._notify(self.tr("Nexus credentials cleared."), "warning")
        self._append_log("[nexus] credentials cleared")

    def _nxm_menu_label(self) -> str:
        """Label for the NXM toggle, reflecting the handler's state at build
        time (the menu is built once at startup)."""
        from Nexus.nxm_handler import NxmHandler
        try:
            registered = NxmHandler.is_registered()
        except Exception:
            registered = False
        return "Unregister NXM handler" if registered else "Register NXM handler"

    def _nexus_toggle_nxm(self):
        """Register or unregister the nxm:// protocol handler (for Nexus
        'Download with Manager' links). Toasts the resulting state."""
        from Nexus.nxm_handler import NxmHandler
        try:
            if NxmHandler.is_registered():
                NxmHandler.unregister()
                self._notify(self.tr("NXM handler unregistered."), "warning")
                self._append_log("[nexus] NXM handler unregistered")
            elif NxmHandler.register():
                self._notify(self.tr("NXM handler registered."), "info")
                self._append_log("[nexus] NXM handler registered")
            else:
                self._notify(self.tr("Failed to register — xdg-mime not found?"), "error")
        except Exception as exc:
            self._notify(self.tr("NXM handler error: {0}").format(exc), "error")

    def _on_oauth_event(self, kind: str, payload):
        """OAuth client callbacks marshalled onto the UI thread."""
        if kind == "status":
            self._append_log(f"[nexus] {payload}")
        elif kind == "error":
            self._oauth_client = None
            self._notify(self.tr("Nexus login failed: {0}").format(payload), "error")
        elif kind == "token":
            # Tokens are already persisted by the client before this fires.
            self._oauth_client = None
            self._nexus_api = None
            self._ensure_nexus_api()   # rebuild api + kick the validate() worker
            self._notify(self.tr("Logged in to Nexus Mods."), "info")
            self._append_log("[nexus] OAuth login complete")
            # If onboarding is open, update its Nexus page (Skip → Next).
            ov = getattr(self, "_onboarding_view", None)
            if ov is not None:
                try:
                    ov.on_logged_in()
                except Exception:
                    pass

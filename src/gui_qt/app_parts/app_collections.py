"""Collection install/update pipeline, preflight checks, progress overlay callbacks and collection tabs.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import configparser
import dataclasses
import json
import re as _re
import threading
from pathlib import Path

from PySide6.QtCore import QTimer


class MainWindow:

    def _open_collections_tab(self):
        """Open the Nexus Collections browser as a detachable tab (view-only —
        the install/detail flow is a separate feature). Same guards as the mods
        browser: a configured game with a Nexus domain + OAuth tokens."""
        if self._tabs.has_key("collections"):
            self._tabs.focus_key("collections")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            self._append_log("[nexus] no OAuth tokens — login required")
            return
        from gui_qt.collections.collections_browser_view import CollectionsBrowserView
        view = CollectionsBrowserView(
            api, domain, game, log_fn=self._append_log,
            on_open_detail=self._open_collection_detail_tab,
            get_profile_dir=lambda: self._gs.profile_dir(),
            on_remove_appended=self._remove_appended_collection)
        self._collections_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_collections_view", None))
        self._tabs.open_tab(view, self.tr("Collections"), key="collections")

    def _remove_appended_collection(self, record):
        """Remove an appended collection from the active profile: every mod it
        installed (meta.ini fromCollection / manifest fileid ownership — other
        collections' and manual mods are never touched) plus its
        installed_collections/ record. Confirm → daemon worker (deploy mutex)
        → _appended_col_removed → reload."""
        from Utils.collections.installed_collections import resolve_owned_mod_names
        game = self._gs.game
        pdir = self._gs.profile_dir()
        if game is None or pdir is None:
            return
        if self._deploy_running:
            self._notify(self.tr("A deploy or removal is already running — try again when it finishes."),
                         "warning")
            return
        if self._col_install_running:
            self._notify(self.tr("A collection install is running — try again when it finishes."),
                         "warning")
            return
        # The tab can outlive a profile switch — only act on records that still
        # exist under the CURRENT profile.
        rec_path = record.get("path")
        try:
            valid = (rec_path is not None and Path(rec_path).is_file()
                     and Path(rec_path).parent.parent.resolve()
                     == Path(pdir).resolve())
        except Exception:
            valid = False
        if not valid:
            view = getattr(self, "_collections_view", None)
            if view is not None:
                view.refresh_appended()
            return
        name = str((record.get("card") or {}).get("name")
                   or record.get("slug") or "")
        names = resolve_owned_mod_names(game, pdir, record)
        if names:
            body = self.tr("Remove '{0}' and its {1} mod(s) from this profile?\n\n"
                           "Their files are deleted from the staging folder — "
                           "this cannot be undone.").format(name, len(names))
        else:
            body = self.tr("No installed mods from '{0}' were found in this "
                           "profile.\n\nRemove the appended-collection entry?"
                           ).format(name)

        def _confirmed(ok):
            if not ok or self._deploy_running:
                return
            self._deploy_running = True

            def _worker():
                from gui_qt.safe_emit import safe_emit
                from Utils.collections.installed_collections import (
                    remove_appended_collection,
                )
                done_ok = True
                try:
                    remove_appended_collection(
                        game, pdir, record, names,
                        log_fn=lambda m: self._op_log.emit(f"[collection] {m}"))
                except Exception as exc:
                    done_ok = False
                    self._op_log.emit(f"[collection] remove appended failed: {exc}")
                finally:
                    self._deploy_running = False
                    safe_emit(self._appended_col_removed, name, done_ok)

            threading.Thread(target=_worker, daemon=True,
                             name="appended-col-remove").start()

        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(self, self.tr("Remove appended collection"),
                                 body, _confirmed,
                                 confirm_label=self.tr("Remove"))

    def _on_appended_col_removed(self, name, ok):
        """UI thread: appended-collection removal finished — reload + toast."""
        self._reload_modlist(rescan_index=True)
        self._reload_plugins()
        view = getattr(self, "_collections_view", None)
        if view is not None:
            try:
                view.refresh_appended()
            except Exception:
                pass
        if ok:
            self._notify(self.tr("Removed appended collection '{0}'.").format(name),
                         "success")
        else:
            self._notify(self.tr("Could not remove '{0}' — see the log.").format(name),
                         "error")

    def _open_current_collection(self):
        """Open the detail tab for the collection installed in the active profile.
        No-op (with a toast) unless the active profile is a collection profile."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        pdir = self._gs.profile_dir()
        from Utils.exe_launch.game_helpers import get_collection_url_from_profile
        url = get_collection_url_from_profile(pdir) if pdir is not None else None
        if not url:
            self._notify(self.tr("The active profile isn't a collection profile."),
                         "warning")
            return
        from Nexus.nexus_api import NexusCollection
        from Utils.collections.collection_manifest import parse_collection_url
        slug, url_domain, rev = parse_collection_url(url)
        if not slug:
            self._notify(self.tr("Couldn't read the collection from this profile."),
                         "warning")
            return
        domain = url_domain or getattr(game, "nexus_game_domain", "") or ""
        col = NexusCollection(slug=slug, name=slug, game_domain=domain)
        self._open_collection_detail_tab(col, revision_number=rev)

    def _open_collection_detail_tab(self, collection, revision_number=None):
        """Open a collection's detail panel as a NEW detachable tab (the
        collections browser tab stays open). Card View passes no revision (latest);
        the browser's 'Open Current' passes the installed revision."""
        # Key by slug when the id is 0 (Open Current builds a bare NexusCollection).
        key = f"collection_detail_{collection.id or collection.slug}"
        if revision_number is not None:
            key = f"{key}_r{revision_number}"
        if self._tabs.has_key(key):
            self._tabs.focus_key(key)
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return
        from gui_qt.collections.collection_detail_view import CollectionDetailView
        view = CollectionDetailView(
            api, collection, game, log_fn=self._append_log,
            revision_number=revision_number)
        view.set_install_handler(
            lambda chosen, skipped, intent="install": self._install_collection(
                collection, view, chosen, skipped, intent))
        title = f"Collection: {collection.name or collection.slug}"
        self._tabs.open_tab(view, title, key=key)
        # NXM / "Open Current" build a bare NexusCollection that only knows the
        # slug, so the title shows the id-like slug until the detail loads. Update
        # the tab caption once the real collection name resolves.
        view.title_resolved.connect(
            lambda name, k=key: self._tabs.set_tab_title(k, f"Collection: {name}"))

    # ---- Collection install (premium auto / non-premium manual) ----------
    def _install_collection(self, collection, detail_view, chosen, skipped,
                            intent="install"):
        """Start a collection install: check premium (premium → automatic
        downloads, non-premium → manual per-mod download prompts), create a new
        profile, then run the neutral orchestrator on a daemon thread with every
        callback marshaled through a Signal (UI-thread slots update the
        overlay). Deferred FOMOD/BAIN mods block the worker on a wizard via
        _col_fomod / _col_bain (same handshake as _make_exists_cb)."""
        if self._col_install_running:
            self._notify(self.tr("A collection install is already running."), "warning")
            return
        # A collection install swaps the shared game to a fresh profile — don't
        # start one while a detached-wizard staging job is still touching the
        # shared game (the wrong-profile hazard).
        if getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            self._notify(self.tr("An install is finishing — try the collection "
                                 "again in a moment."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus."), "warning")
            return
        dl_path = getattr(detail_view, "download_link_path", "") or ""
        revision_number = getattr(detail_view, "_revision_number", None)
        if revision_number is None and hasattr(detail_view, "_resolved_viewing_revision"):
            revision_number = detail_view._resolved_viewing_revision()
        # True collection size (installed/uncompressed) shown in the detail header —
        # surfaced in the install overlay's aggregate label (the compressed download
        # total is much smaller and misrepresents the collection's real size).
        total_size = int(getattr(detail_view, "_total_size", 0) or 0)
        # Manifest rule: this collection must be installed as a NEW profile.
        recommend_new = bool(getattr(detail_view, "_recommend_new_profile", False))
        # Local-manifest import (Import profile): a parsed manifest to feed straight
        # into the orchestrator + a local .mosaic whose bundled mods/profile files
        # are extracted after install. Both empty for a normal Nexus collection.
        local_manifest = getattr(detail_view, "_local_manifest", None)
        bundle_zip = getattr(detail_view, "_bundle_zip_path", "") or ""
        # Nexus install: reuse the manifest the detail view already fetched
        # (Tk _collection_schema_cache parity). Only when its revision matches
        # the one being installed — otherwise the orchestrator downloads the
        # right one itself. Prevents a second CDN fetch at install time, whose
        # silent failure loses every FOMOD/BAIN auto-selection.
        if local_manifest is None:
            fetched = getattr(detail_view, "_fetched_manifest", None)
            fetched_rev = getattr(detail_view, "_fetched_manifest_rev", None)
            if fetched and (revision_number is None
                            or fetched_rev == revision_number):
                local_manifest = fetched
        mods = detail_view.install_mods(skipped)
        # Unticked optionals, taken from the FULL mod list (mods above already
        # excludes them) — the orchestrator removes these from an existing
        # profile on continue/append/update.
        skipped_mods = detail_view.skipped_optional_mods(skipped)
        if not mods:
            self._notify(self.tr("This collection has no installable mods."), "info")
            return
        slug = getattr(collection, "slug", "") or ""
        domain = (getattr(game, "nexus_game_domain", "")
                  or getattr(collection, "game_domain", "") or "")
        # Off-site mods (manual downloads) from the detail view's manifest —
        # remembered so the completion handler can remind the user about them.
        offsite = list(getattr(detail_view, "_offsite", None) or [])

        # Premium gate runs off-thread (validate() is rate-limited); on success it
        # creates the profile + starts the pipeline, all marshaled back to the UI.
        self._col_install_running = True
        self._notify(self.tr("Checking Nexus account…"), "info")

        def _premium_worker():
            from Utils.ui_config import load_nexus_last_premium, save_nexus_last_premium
            try:
                user = api.validate()
                is_premium = bool(getattr(user, "is_premium", False))
                try:
                    save_nexus_last_premium(is_premium)
                except Exception:
                    pass
            except Exception as exc:
                # GH#278: a transient validate() failure (network hiccup, rate
                # limit) must not silently demote a premium user to manual
                # mode — fall back to the last successfully-validated status.
                last = load_nexus_last_premium()
                is_premium = bool(last)
                self._op_log.emit(
                    f"[collection] premium check failed: {exc} — using "
                    f"last-known status ({'premium' if is_premium else 'not premium'})")
            try:
                from Utils.ui_config import load_force_manual_install
                force_manual = bool(load_force_manual_install())
            except Exception:
                force_manual = False
            self._col_finished.emit(
                "_premium", {"ok": is_premium and not force_manual,
                             "manual": not (is_premium and not force_manual),
                             "collection": collection, "domain": domain,
                             "slug": slug, "dl_path": dl_path,
                             "revision": revision_number, "mods": mods,
                             "total_size": total_size,
                             "skipped": set(skipped),
                             "skipped_mods": skipped_mods,
                             "game": game, "api": api,
                             "recommend_new": recommend_new, "intent": intent,
                             "local_manifest": local_manifest,
                             "bundle_zip": bundle_zip,
                             "offsite": offsite})

        threading.Thread(target=_premium_worker, daemon=True,
                         name="col-premium").start()

    # ---- Collection preflight (before New/Append/Continue) ---------------
    def _collection_preflight(self, info, proceed):
        """UI thread. Check the game can support this collection BEFORE any
        profile is created or anything is downloaded, then call *proceed()*.

        Today: a Skyrim SE collection that lists the SRS runtime-swap mod needs
        the older runtime SKSE64 supports. Mosaic applies that mod's patches
        itself (Utils.modding_tools.skyrim_runtime) and so never installs the
        mod. Nothing to report (the common case) proceeds with no dialog."""
        from Utils.collections import collection_preflight as pf
        game = info["game"]
        all_mods = list(info.get("mods") or [])
        swap_mod = pf.find_runtime_swap_mod(all_mods) if pf.is_skyrim_se(game) else None
        if swap_mod is not None:
            info["runtime_swap_mod"] = swap_mod
            info["mods"], _left_out = pf.without_runtime_swap_mods(all_mods)
            self._append_log("[preflight] leaving out the runtime swap mod "
                             f"({getattr(swap_mod, 'mod_name', '')}) — Mosaic applies its "
                             "patches itself")
        ctx = {"info": info, "proceed": proceed, "all_mods": all_mods,
               "overlay": None, "prepared": None, "hpatchz": None}
        self._preflight_ctx = ctx
        threading.Thread(target=self._preflight_check_worker, args=(ctx, False),
                         daemon=True, name="col-preflight").start()

    def _preflight_dirs(self, game):
        from Utils.config_paths import (
            get_download_cache_dir_for_game,
            list_all_cache_dirs,
        )
        from Utils.wizard_support.wizard_archives import get_downloads_dir
        return ([*list_all_cache_dirs(game.name), get_downloads_dir()],
                get_download_cache_dir_for_game(game.name))

    def _preflight_gather(self, ctx, transition):
        from Utils.collections import collection_preflight as pf
        from Utils.config_paths import get_game_config_dir
        from Utils.exe_launch.exe_launch import _wine_process_alive
        info = ctx["info"]; game = info["game"]
        try:
            staging = game.get_effective_mod_staging_path()
        except Exception:
            staging = None
        return pf.run_preflight(
            game=game, mods=ctx["all_mods"], manifest=info.get("local_manifest"),
            game_root=game.get_game_path(), state_dir=get_game_config_dir(game.name),
            transition=transition,
            game_running=_wine_process_alive("SkyrimSE.exe") if pf.is_skyrim_se(game) else False,
            staging_root=staging, cache_dir=self._preflight_dirs(game)[1],
            install_size=int(info.get("total_size", 0) or 0))

    def _preflight_check_worker(self, ctx, recheck):
        """Worker: find (never download) the swap archive, then run the checks."""
        from Utils.collections import collection_preflight as pf
        from Utils.collections import runtime_swap_fix as fix
        from Utils.modding_tools import skyrim_runtime as sr
        info = ctx["info"]; game = info["game"]
        try:
            prepared = ctx.get("prepared")
            swap_mod = info.get("runtime_swap_mod")
            if prepared is None and swap_mod is not None:
                search, dest = self._preflight_dirs(game)
                if pf.find_swap_archive(swap_mod, search) is not None:
                    try:
                        prepared = fix.prepare_swap(
                            swap_mod, domain=info.get("domain") or "skyrimspecialedition",
                            search_dirs=search, dest_dir=dest, downloader=None,
                            log_fn=self._op_log.emit)
                        ctx["prepared"] = prepared
                    except sr.RuntimeSwapError as exc:
                        self._op_log.emit(f"[preflight] cached runtime swap archive unusable: {exc}")
            checks = self._preflight_gather(ctx, prepared.transition if prepared else None)
            self._preflight_ev.emit("rechecked" if recheck else "checked", (ctx, checks))
        except Exception as exc:                         # noqa: BLE001
            self._preflight_ev.emit("error", (ctx, str(exc)))

    def _on_preflight_ev(self, kind, payload):
        """UI thread: everything the preflight workers report."""
        from Utils.collections import collection_preflight as pf
        ctx, data = payload
        overlay = ctx.get("overlay")
        if kind == "checked":
            checks = data
            if not (pf.blocking_failures(checks) or pf.warnings(checks)):
                self._preflight_finish(ctx, True)
                return
            from gui_qt.collections.collection_preflight_overlay import PreflightOverlay
            ctx["overlay"] = PreflightOverlay.show_over(
                self, checks, lambda ov, c=ctx: self._preflight_fix(c, ov),
                lambda result, c=ctx: self._preflight_finish(c, result == "continue"))
        elif kind == "busy":
            if overlay is not None:
                overlay.set_busy(data)
        elif kind == "need-restore":
            self._preflight_restore_then_apply(ctx)
        elif kind == "fixed":
            if overlay is not None:
                overlay.set_busy(self.tr("Checking the game again…"))
            threading.Thread(target=self._preflight_check_worker, args=(ctx, True),
                             daemon=True, name="col-preflight").start()
        elif kind == "rechecked":
            checks = data
            if overlay is None:
                self._preflight_finish(ctx, not pf.blocking_failures(checks))
            elif pf.blocking_failures(checks):
                overlay.set_checks(checks)
                overlay.set_error(self.tr("The game still isn't ready — see above."))
            elif pf.warnings(checks):
                overlay.set_checks(checks)
                overlay.set_error("")
            else:
                overlay.close_ok()
        elif kind == "fix-failed":
            self._append_log(f"[preflight] fix failed: {data}")
            if overlay is not None:
                overlay.set_error(str(data))
        elif kind == "error":
            self._append_log(f"[preflight] check failed unexpectedly: {data}")
            if ctx["info"].get("runtime_swap_mod") is not None:
                # This collection depends on the check — don't guess.
                self._notify(self.tr("Couldn't verify the game version: {0}").format(data),
                             "error")
                self._preflight_finish(ctx, False)
            else:
                self._preflight_finish(ctx, True)      # nothing else depends on it

    def _preflight_finish(self, ctx, go):
        """UI thread: preflight is over — continue the install, or abandon it."""
        prepared = ctx.get("prepared")
        if prepared is not None:
            prepared.cleanup()
            ctx["prepared"] = None
        ctx["overlay"] = None
        if go:
            ctx["proceed"]()
        else:
            self._col_install_running = False
            self._notify(self.tr("Collection install cancelled."), "info")

    def _preflight_fix(self, ctx, overlay):
        """UI thread: the user pressed 'Fix and continue'."""
        overlay.set_busy(self.tr("Preparing the Skyrim runtime patch…"))
        threading.Thread(target=self._preflight_prepare_worker, args=(ctx,),
                         daemon=True, name="col-preflight-fix").start()

    def _preflight_prepare_worker(self, ctx):
        """Worker: hpatchz + the verified swap archive; then apply, or ask the UI
        to Restore first when the game is deployed."""
        from Utils.collections import runtime_swap_fix as fix
        from Utils.modding_tools import skyrim_runtime as sr
        info = ctx["info"]; game = info["game"]
        try:
            ctx["hpatchz"] = sr.ensure_hpatchz(log_fn=self._op_log.emit)
            if ctx.get("prepared") is None:
                self._preflight_ev.emit("busy", (ctx, self.tr("Getting the runtime patch…")))
                downloader = None
                if info.get("ok") and info.get("api") is not None:
                    from Nexus.nexus_download import NexusDownloader
                    downloader = NexusDownloader(info["api"])
                search, dest = self._preflight_dirs(game)
                domain = info.get("domain") or "skyrimspecialedition"
                mod = info["runtime_swap_mod"]
                ctx["prepared"] = fix.prepare_swap(
                    mod, domain=domain, search_dirs=search, dest_dir=dest,
                    downloader=downloader,
                    nexus_url=f"https://www.nexusmods.com/{domain}/mods/{mod.mod_id}?tab=files",
                    log_fn=self._op_log.emit)
            if sr.is_deployed(game.get_game_path()):
                self._preflight_ev.emit("need-restore", (ctx, None))
                return
            self._preflight_apply(ctx)
        except sr.RuntimeSwapError as exc:
            self._preflight_ev.emit("fix-failed", (ctx, str(exc)))
        except Exception as exc:                         # noqa: BLE001
            self._preflight_ev.emit("fix-failed", (ctx, self.tr("Unexpected error: {0}").format(exc)))

    def _preflight_apply(self, ctx):
        """Worker: apply the verified swap (game must be restored)."""
        from Utils.config_paths import get_game_config_dir
        from Utils.modding_tools import skyrim_runtime as sr
        game = ctx["info"]["game"]
        try:
            self._preflight_ev.emit("busy", (ctx, self.tr("Switching the game version…")))
            sr.apply_transition(
                game.get_game_path(), ctx["prepared"].transition,
                get_game_config_dir(game.name), hpatchz=ctx["hpatchz"],
                log_fn=self._op_log.emit, prefix_path=game.get_prefix_path())
            self._preflight_ev.emit("fixed", (ctx, None))
        except sr.RuntimeSwapError as exc:
            self._preflight_ev.emit("fix-failed", (ctx, str(exc)))
        except Exception as exc:                         # noqa: BLE001
            self._preflight_ev.emit("fix-failed", (ctx, self.tr("Unexpected error: {0}").format(exc)))

    def _preflight_restore_then_apply(self, ctx):
        """UI thread: the game is deployed — Restore it (the normal restore path),
        then apply the swap on a worker."""
        overlay = ctx.get("overlay")
        if overlay is not None:
            overlay.set_busy(self.tr("Restoring the game to vanilla first…"))

        def _restored(ok):
            if not ok:
                self._preflight_ev.emit("fix-failed", (ctx, self.tr(
                    "Restore failed — see the log. Fix that and press Fix again.")))
                return
            threading.Thread(target=self._preflight_apply, args=(ctx,),
                             daemon=True, name="col-preflight-apply").start()

        if not self._wizard_run_restore(_restored, ignore_col_install=True):
            self._preflight_ev.emit("fix-failed", (ctx, self.tr(
                "Couldn't start Restore right now (a deploy or restore may be running). "
                "Try again in a moment.")))

    def _choose_collection_mode(self, info):
        """UI thread: show the New/Append (or Continue) mode overlay, then start
        the pipeline with the chosen mode. Mirrors Tk _continue_install_collection:
        if this exact collection+revision URL is already in a profile → Continue;
        else → New/Append."""
        from Utils.exe_launch.game_helpers import (
            _profiles_for_game,
            find_profile_with_collection_url,
        )
        game = info["game"]; slug = info["slug"]; domain = info["domain"]
        rev = info["revision"]
        url = f"https://www.nexusmods.com/games/{domain}/collections/{slug}"
        if rev is not None:
            url += f"/revisions/{rev}"
        try:
            existing = find_profile_with_collection_url(game.name, url)
        except Exception:
            existing = None

        def _done(result):
            if result is None:
                self._col_install_running = False
                self._notify(self.tr("Collection install cancelled."), "info")
                return
            info["mode_result"] = result
            self._start_collection_pipeline(info)

        if existing:
            from gui_qt.collections.collection_mode_overlay import ContinueOverlay
            ContinueOverlay.show_over(self, existing, _done)
        else:
            from gui_qt.collections.collection_mode_overlay import ModeOverlay
            try:
                profiles = _profiles_for_game(game.name)
            except Exception:
                profiles = []
            # Manifest rule (collectionConfig.recommendNewProfile) → disable Append.
            force_new = bool(info.get("recommend_new"))
            ModeOverlay.show_over(self, profiles, _done, force_new_profile=force_new)

    def _resume_collection_install(self, info):
        """UI thread: RESUME a paused install — clear the paused flag + registry,
        then continue into the profile that already claims this collection.
        Already-installed mods skip by file_id (Tk parity)."""
        game = info["game"]; slug = info["slug"]
        from Utils.exe_launch.game_helpers import find_profile_with_collection_slug
        from Utils.profile.profile_state import write_collection_install_paused
        try:
            pname = find_profile_with_collection_slug(game.name, slug)
        except Exception:
            pname = None
        if not pname:
            self._col_install_running = False
            self._notify(self.tr("Could not find the paused profile."), "error")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname
        try:
            write_collection_install_paused(profile_dir, False)
        except Exception:
            pass
        try:
            from gui_qt.collections.collection_detail_view import _PAUSED_COLLECTIONS
            _PAUSED_COLLECTIONS.discard(slug)
        except Exception:
            pass
        self._refresh_open_collection_buttons()
        info["mode_result"] = ("continue", pname, False, False)
        self._start_collection_pipeline(info)

    def _run_collection_update(self, info):
        """UI thread: UPDATE an installed collection to the viewed revision.
        Port of Tk _on_update_collection + _apply_collection_update: compute the
        diff, confirm via UpdateOverlay, remove stale/bundled/patched mods, stash
        an order-preserving update_context, then continue-install."""
        game = info["game"]; slug = info["slug"]; mods = info["mods"]
        from Utils.exe_launch.game_helpers import find_profile_with_collection_slug
        try:
            pname = find_profile_with_collection_slug(game.name, slug)
        except Exception:
            pname = None
        if not pname:
            self._col_install_running = False
            self._notify(self.tr("Could not find the installed collection profile."), "error")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname

        # Guard: the collection's profile must be the active one (cross-profile
        # removal is out of scope, matching Tk).
        active = getattr(game, "_active_profile_dir", None)
        if active is None or Path(active).resolve() != profile_dir.resolve():
            self._col_install_running = False
            self._notify(self.tr("Switch to profile '{0}' first, then Update.").format(pname),
                         "warning")
            return

        # Compute the diff (old cached manifest vs the new mod list).
        import json as _json

        from Utils.collections.collection_diff import diff_collection
        from Utils.mods.modlist import read_modlist
        from Utils.profile.profile_state import read_collection_revision
        old_manifest = {}
        try:
            mf = profile_dir / "collection.json"
            if mf.is_file():
                old_manifest = _json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            old_manifest = {}
        modlist_path = profile_dir / "modlist.txt"
        try:
            installed_names_lower = {
                e.name.lower() for e in read_modlist(modlist_path)
                if not e.is_separator} if modlist_path.is_file() else set()
        except Exception:
            installed_names_lower = set()
        try:
            staging_path = game.get_effective_mod_staging_path()
        except Exception:
            staging_path = None
        try:
            diff = diff_collection(
                old_manifest=old_manifest, new_mods=mods,
                staging_path=Path(staging_path) if staging_path else Path("."),
                installed_names_lower=installed_names_lower,
                collection_slug=slug)
        except Exception as exc:
            self._col_install_running = False
            self._notify(self.tr("Could not compute update diff: {0}").format(exc), "error")
            return

        # Human labels for the update/add buckets via the new mod list.
        fid_to_name = {getattr(m, "file_id", 0): (getattr(m, "mod_name", "")
                       or f"file {getattr(m, 'file_id', 0)}") for m in mods}
        to_update_labels = [
            f"{old} → {fid_to_name.get(fid, fid)}"
            for old, fid in zip(diff.to_update_old, diff.to_update_new_fids)]
        to_add_labels = [fid_to_name.get(fid, str(fid))
                         for fid in diff.to_install_fids]

        from_rev = read_collection_revision(profile_dir)
        to_rev = info["revision"]

        def _done(apply_it):
            if not apply_it:
                self._col_install_running = False
                self._notify(self.tr("Collection update cancelled."), "info")
                return
            self._apply_collection_update(info, profile_dir, pname, slug, diff)

        from gui_qt.collections.collection_update_overlay import UpdateOverlay
        UpdateOverlay.show_over(
            self, profile_name=pname, from_rev=from_rev, to_rev=to_rev,
            to_remove=list(diff.to_remove), to_update=to_update_labels,
            to_add=to_add_labels, orphans=list(diff.orphans), on_done=_done)

    def _apply_collection_update(self, info, profile_dir, pname, slug, diff):
        """UI thread: user confirmed — scan staging for this collection's
        bundled/patched mods in a worker (meta.ini per mod = too slow for the
        UI thread on big collections), then finish on the UI thread."""
        from gui_qt.safe_emit import safe_emit
        game = info["game"]

        def worker():
            from Utils.mods.modlist import read_modlist
            try:
                snapshot = list(read_modlist(profile_dir / "modlist.txt")) \
                    if (profile_dir / "modlist.txt").is_file() else []
            except Exception:
                snapshot = []

            # Bundled/patched mods for THIS collection are force-reinstalled
            # (their contents/patches may have changed) — port of Tk 1974-2001.
            slug_lower = (slug or "").strip().lower()
            bundled_or_patched: set[str] = set()
            try:
                staging_path = Path(game.get_effective_mod_staging_path())
            except Exception:
                staging_path = None
            if staging_path is not None and staging_path.is_dir() and slug_lower:
                for mod_dir in staging_path.iterdir():
                    if not mod_dir.is_dir():
                        continue
                    meta = mod_dir / "meta.ini"
                    if not meta.is_file():
                        continue
                    cp = configparser.ConfigParser()
                    try:
                        cp.read(meta, encoding="utf-8")
                    except Exception:
                        continue
                    if not cp.has_section("General"):
                        continue
                    if (cp["General"].get("fromCollection") or "").strip().lower() != slug_lower:
                        continue
                    if (cp["General"].getboolean("fromCollectionBundled", fallback=False)
                            or cp["General"].getboolean("fromCollectionPatched", fallback=False)):
                        bundled_or_patched.add(mod_dir.name.lower())
            safe_emit(self._col_update_scan_done,
                      {"info": info, "profile_dir": profile_dir, "pname": pname,
                       "diff": diff, "snapshot": snapshot,
                       "bundled_or_patched": bundled_or_patched})

        threading.Thread(target=worker, daemon=True,
                         name="col-update-scan").start()

    def _finish_collection_update(self, payload):
        """UI thread: remove stale + bundled/patched mods, build
        update_context, then continue-install."""
        from Utils.mods import mod_remove
        info = payload["info"]; profile_dir = payload["profile_dir"]
        pname = payload["pname"]; diff = payload["diff"]
        snapshot = payload["snapshot"]
        game = info["game"]

        all_remove_lower = ({n.lower() for n in diff.removals}
                            | payload["bundled_or_patched"])
        removed_lower: set[str] = set()
        if all_remove_lower:
            to_remove_names = [e.name for e in snapshot
                               if not e.is_separator
                               and e.name.lower() in all_remove_lower]
            if to_remove_names:
                try:
                    mod_remove.remove_mods(
                        game, profile_dir, to_remove_names,
                        log_fn=lambda m: self._append_log(str(m)))
                    removed_lower = {n.lower() for n in to_remove_names}
                except Exception as exc:
                    self._col_install_running = False
                    self._notify(self.tr("Update failed during removal: {0}").format(exc), "error")
                    return

        filtered_snapshot = [
            e for e in snapshot
            if e.is_separator or e.name.lower() not in removed_lower]

        info["update_context"] = {"snapshot": filtered_snapshot, "schema_order": {}}
        info["mode_result"] = ("continue", pname, False, False)
        self._start_collection_pipeline(info)

    def _refresh_open_collection_buttons(self):
        """Refresh the Install/Update/Resume button on any open collection detail
        tab (after a pause/resume state change)."""
        try:
            from gui_qt.collections.collection_detail_view import CollectionDetailView
            for view in self.findChildren(CollectionDetailView):
                try:
                    view._update_install_btn_state()
                except Exception:
                    pass
        except Exception:
            pass

    def _start_collection_pipeline(self, info):
        """UI thread: premium confirmed — create the profile, open the overlay,
        and launch the orchestrator on a daemon thread."""
        game = info["game"]; api = info["api"]; collection = info["collection"]
        slug = info["slug"]; domain = info["domain"]
        revision_number = info["revision"]; mods = info["mods"]
        skipped = info["skipped"]; dl_path = info["dl_path"]
        total_size = int(info.get("total_size", 0) or 0)
        self._col_install_slug = slug
        update_context = info.get("update_context")
        # Local-manifest import: feed the parsed manifest to the orchestrator (no
        # Nexus fetch) and remember the .mosaic so its bundled mods + profile
        # files are extracted once the Nexus mods finish installing.
        local_manifest = info.get("local_manifest")
        self._col_bundle_zip = info.get("bundle_zip") or ""
        self._col_offsite = list(info.get("offsite") or [])

        # Resolve the install mode chosen in the mode overlay (default: new).
        mode_result = info.get("mode_result") or ("new", None, False, False)
        mode, append_profile_name, ov_existing, skip_existing = mode_result
        # Mods whose optional was unticked — the orchestrator removes these from
        # an existing profile (append/continue). NB: must come from the full
        # detail-view list; *mods* already excludes them.
        skipped_mods = list(info.get("skipped_mods") or [])

        from Utils.exe_launch.game_helpers import _create_profile, _profiles_for_game
        from Utils.profile.profile_state import write_collection_optional_skipped

        # overwrite_existing is None for new/continue (fresh modlist write); a bool
        # for append (preserves existing load order via _append_reconcile_modlist).
        overwrite_existing = None
        skip_existing_arg = False

        if mode == "new":
            raw = collection.name or slug or "Collection"
            base = _re.sub(r"[^\w\s\-]", "", raw).strip().replace(" ", "_") or "Collection"
            profile_name = (f"{base}_Rev{revision_number}"[:64]
                            if revision_number is not None else base[:64])
            _existing = set(_profiles_for_game(game.name))
            _pn = profile_name
            _i = 2
            while _pn in _existing:
                _pn = f"{profile_name}_{_i}"[:64]
                _i += 1
            profile_name = _pn
            try:
                profile_dir = Path(_create_profile(
                    game.name, profile_name, profile_specific_mods=True))
            except Exception as exc:
                self._col_install_running = False
                self._notify(self.tr("Could not create profile: {0}").format(exc), "error")
                return
            # New/continue claim the collection: record URL + revision.
            self._stamp_collection_profile(
                profile_dir, domain, slug, revision_number, skipped)
        else:
            # append / continue → install into an existing profile.
            profile_dir = game.get_profile_root() / "profiles" / append_profile_name
            if not profile_dir.is_dir():
                self._col_install_running = False
                self._notify(self.tr("Profile '{0}' not found.").format(append_profile_name), "error")
                return
            if mode == "append":
                overwrite_existing = bool(ov_existing)
                skip_existing_arg = bool(skip_existing)
                # Append doesn't claim the collection URL; still record skipped set.
                try:
                    write_collection_optional_skipped(profile_dir, skipped)
                except Exception:
                    pass
            else:  # continue
                self._stamp_collection_profile(
                    profile_dir, domain, slug, revision_number, skipped)

        # GH#278: a cancelled install may delete the profile ONLY when this
        # run created it (new mode). Continue/append/resume/update target a
        # pre-existing profile that must survive a cancel.
        self._col_created_profile = (mode == "new")
        self._col_profile_name = profile_dir.name

        # Card display fields for the appended-collections record
        # (installed_collections/<slug>.json — see Utils.collections.installed_collections).
        append_card_info = None
        if overwrite_existing is not None:
            try:
                append_card_info = dataclasses.asdict(collection)
            except Exception:
                append_card_info = {
                    k: getattr(collection, k, None)
                    for k in ("id", "slug", "name", "summary", "user_name",
                              "endorsements", "total_downloads", "mod_count",
                              "tile_image_url", "game_domain")}
            if not append_card_info.get("game_domain"):
                append_card_info["game_domain"] = domain

        old_profile_dir = getattr(game, "_active_profile_dir", None)

        # Overlay + control. Manual (non-premium) installs get the per-mod
        # download-prompt card; premium gets the download/extract progress
        # overlay. Both expose the same slot surface to the _on_col_* handlers.
        from Utils.collections.collection_install import CollectionInstallControl
        control = CollectionInstallControl()
        self._col_install_control = control
        manual_mode = bool(info.get("manual"))
        title = f"Installing collection: {collection.name or slug}"
        if manual_mode:
            from gui_qt.collections.collection_manual_overlay import (
                CollectionManualOverlay,
            )
            self._col_install_overlay = CollectionManualOverlay.show_over(
                self, collection.name or slug, profile_dir.name, len(mods),
                control.manual_queue, on_pause=self._on_col_pause_clicked,
                on_cancel=self._on_col_cancel_clicked)
        else:
            from gui_qt.collections.collection_install_overlay import (
                CollectionInstallOverlay,
            )
            from Utils.ui_config import load_download_speed_limit
            self._col_install_overlay = CollectionInstallOverlay.show_over(
                self, title, on_pause=self._on_col_pause_clicked,
                on_cancel=self._on_col_cancel_clicked,
                limit_mbps=load_download_speed_limit(),
                on_limit_change=self._on_col_limit_changed)

        callbacks = self._build_collection_callbacks()

        def _worker():
            from Nexus.nexus_download import NexusDownloader
            from Utils.collections.collection_install import run_collection_install
            from Utils.config_paths import get_download_cache_dir_for_game
            downloader = NexusDownloader(
                api, download_dir=get_download_cache_dir_for_game(game.name or ""))
            try:
                run_collection_install(
                    game=game, api=api, downloader=downloader, mods=mods,
                    download_link_path=dl_path, profile_dir=profile_dir,
                    old_profile_dir=old_profile_dir, collection_slug=slug,
                    revision_number=revision_number, collection_total_size=total_size,
                    skipped_fids=skipped,
                    skipped_mods=skipped_mods, overwrite_existing=overwrite_existing,
                    skip_existing=skip_existing_arg, update_context=update_context,
                    collection_schema_cache=local_manifest,
                    manual_mode=manual_mode,
                    append_card_info=append_card_info,
                    callbacks=callbacks, control=control)
            except Exception as exc:
                import traceback
                self._op_log.emit(f"[collection] install error: {exc}\n"
                                  f"{traceback.format_exc()}")
                # An unexpected error is NOT a cancel: keep the profile and the
                # download cache so the install can be continued, rather than
                # throwing away hours of progress (see the "failed" branch).
                self._col_finished.emit("failed", {"profile_dir": str(profile_dir),
                                                   "error": str(exc)})

        threading.Thread(target=_worker, daemon=True, name="col-install").start()

    def _stamp_collection_profile(self, profile_dir, domain, slug, revision_number,
                                  skipped):
        """Record the collection URL + revision + skipped-optionals on a profile
        that claims this collection (new / continue modes)."""
        from Utils.exe_launch.game_helpers import save_collection_url_to_profile
        from Utils.profile.profile_state import (
            write_collection_optional_skipped,
            write_collection_revision,
        )
        try:
            url = f"https://www.nexusmods.com/games/{domain}/collections/{slug}"
            if revision_number is not None:
                url += f"/revisions/{revision_number}"
            save_collection_url_to_profile(profile_dir, url)
        except Exception as exc:
            self._append_log(f"[collection] could not save URL: {exc}")
        try:
            if revision_number is not None:
                write_collection_revision(profile_dir, revision_number)
            # Always write — an empty set clears a previously saved selection.
            write_collection_optional_skipped(profile_dir, set(skipped or ()))
        except Exception:
            pass
        # Reflect a just-updated revision immediately if this is the active
        # profile — the folder name itself is only ever stamped once, at
        # creation, so the tooltip is the only thing that won't go stale.
        game = self._gs.game
        if (game is not None
                and getattr(game, "_active_profile_dir", None) == profile_dir):
            self._update_profile_collection_tooltip()

    def _build_collection_callbacks(self):
        """Build a CollectionInstallCallbacks whose every field is a single
        Signal.emit — NO callback touches a widget (all UI mutation happens in the
        connected UI-thread slots). Matches the thread-safety rule that cost a
        segfault before."""
        from Utils.collections.collection_install import CollectionInstallCallbacks
        return CollectionInstallCallbacks(
            on_status=lambda m: self._col_status.emit(m),
            on_progress=lambda v: self._col_progress.emit(v),
            on_agg_download=lambda c, t, s: self._col_agg.emit(int(c), int(t), float(s)),
            on_display_total=lambda n: self._col_display_total.emit(int(n)),
            on_cache_progress=lambda h, d: self._col_cache_progress.emit(int(h), int(d)),
            on_dl_mod_start=lambda f, n, s: self._col_dl.emit("start", (f, n, s)),
            on_dl_mod_update=lambda f, c, t: self._col_dl.emit("update", (f, c, t)),
            on_dl_mod_finish=lambda f: self._col_dl.emit("finish", f),
            on_extract_queue=lambda f, n: self._col_extract.emit("queue", (f, n)),
            on_extract_add=lambda f, n: self._col_extract.emit("add", (f, n)),
            on_extract_update=lambda f, c, t: self._col_extract.emit("update", (f, c, t)),
            on_extract_remove=lambda f: self._col_extract.emit("remove", f),
            on_row_installed=lambda f: self._col_row.emit(int(f)),
            on_manual_mod=lambda d: self._col_manual.emit(dict(d)),
            on_log=lambda m: self._op_log.emit(str(m)),
            on_done=lambda i, s, t, p: self._col_finished.emit("done", (i, s, t, p)),
            on_paused=lambda i, p: self._col_finished.emit("paused", (i, p)),
            on_cancelled=lambda pd: self._col_finished.emit("cancelled", {"profile_dir": str(pd)}),
            resolve_fomod=self._make_col_fomod_cb(),
            resolve_bain=self._make_col_bain_cb(),
        )

    # ---- collection progress slots (UI thread) ---------------------------
    def _on_col_status(self, text):
        if self._col_install_overlay is not None:
            self._col_install_overlay.set_status(text)

    def _on_col_progress(self, value):
        pass   # overlay drives from the aggregate bytes; per-mod count unused here

    def _on_col_agg(self, cur, tot, mbps):
        if self._col_install_overlay is not None:
            self._col_install_overlay.set_agg(cur, tot, mbps)

    def _on_col_display_total(self, n):
        if self._col_install_overlay is not None:
            self._col_install_overlay.set_display_total(n)

    def _on_col_cache_progress(self, cache_hit_done, dl_done):
        if self._col_install_overlay is not None:
            self._col_install_overlay.set_cache_progress(cache_hit_done, dl_done)

    def _on_col_dl(self, verb, payload):
        ov = self._col_install_overlay
        if ov is None:
            return
        if verb == "start":
            ov.dl_start(*payload)
        elif verb == "update":
            ov.dl_update(*payload)
        elif verb == "finish":
            ov.dl_finish(payload)

    def _on_col_extract(self, verb, payload):
        ov = self._col_install_overlay
        if ov is None:
            return
        if verb == "queue":
            ov.extract_queue(*payload)
        elif verb == "add":
            ov.extract_add(*payload)
        elif verb == "update":
            ov.extract_update(*payload)
        elif verb == "remove":
            ov.extract_remove(payload)

    def _on_col_row(self, file_id):
        if self._col_install_overlay is not None:
            self._col_install_overlay.row_installed(file_id)

    def _on_col_manual(self, payload):
        """Manual-mode producer prompts the next mod (on_manual_mod)."""
        ov = self._col_install_overlay
        if ov is not None and hasattr(ov, "update_mod"):
            ov.update_mod(payload)

    # ---- deferred FOMOD / BAIN blocking handshake ------------------------
    def _make_col_fomod_cb(self):
        """resolve_fomod for the orchestrator: runs on the WORKER thread, asks the
        UI to open the FOMOD wizard, and BLOCKS until the user Finishes/Cancels."""

        def _cb(config, base, name, installed, active, loose, saved=None):
            holder = {"result": None}
            ev = threading.Event()
            self._col_fomod.emit({"config": config, "base": base, "name": name,
                                  "installed": installed, "active": active,
                                  "loose": loose, "saved": saved,
                                  "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_col_fomod_ui(self, payload):
        """UI thread: open the FOMOD wizard as a tab; unblock the worker on
        finish/cancel/close. Only ONE deferred wizard is ever open (the deferred
        loop is sequential) so there's no nesting."""
        from gui_qt.views.fomod_wizard_view import FomodWizardView
        holder, ev = payload["holder"], payload["event"]
        done = {"v": False}
        # Hide the install overlay while the wizard is up — it covers the tab
        # and would otherwise block the deferred FOMOD picker (no work is running
        # while we wait for the user, so nothing to display).
        self._hide_col_overlay()

        def _finish_ev(result):
            if done["v"]:
                return
            done["v"] = True
            holder["result"] = result
            self._tabs.close_tab("col_fomod_wizard")
            self._show_col_overlay()
            ev.set()

        _sel_path = None
        _game_name = getattr(getattr(self._gs, "game", None), "name", "")
        if _game_name:
            from Utils.config_paths import get_fomod_selections_path
            _sel_path = get_fomod_selections_path(_game_name, payload["name"])
        _prof_sel_path = self._profile_fomod_selection_path(
            getattr(getattr(self._gs, "game", None), "_active_profile_dir", None),
            payload["name"])
        # Merge the live plugin-panel state (see the manual-install path below):
        # plugins.txt can lag the model, so a FOMOD `state="Active"` dep wouldn't
        # match until a save/sort. Union the panel's enabled/all sets.
        _c_inst = set(payload.get("installed") or set())
        _c_act = set(payload.get("active") or set())
        try:
            pm = getattr(self, "_plugin_model", None)
            if pm is not None:
                live_all = pm.all_lower()
                live_active = pm.enabled_lower()
                _c_inst |= live_all
                _c_act = (_c_act | live_active) - (live_all - live_active)
        except Exception:
            pass
        view = FomodWizardView(payload["config"], payload["base"], payload["name"],
                               on_finish=lambda sel: _finish_ev(sel),
                               on_cancel=lambda: _finish_ev(None),
                               saved_selections=payload.get("saved"),
                               selections_path=_sel_path,
                               profile_selections_path=_prof_sel_path,
                               installed_files=_c_inst,
                               active_files=_c_act,
                               loose_files=payload.get("loose"))
        # Closing the tab (or a stop request) counts as cancel so we never hang.
        view.destroyed.connect(lambda *_: _finish_ev(None))
        self._tabs.open_tab(view, self.tr("Install: {0}").format(payload['name']),
                            key="col_fomod_wizard")

    def _make_col_bain_cb(self):

        def _cb(subpkgs, root, name):
            holder = {"result": None}
            ev = threading.Event()
            self._col_bain.emit({"subpkgs": subpkgs, "root": root, "name": name,
                                 "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_col_bain_ui(self, payload):
        from gui_qt.views.bain_picker_view import BainPickerView
        holder, ev = payload["holder"], payload["event"]
        done = {"v": False}
        # Hide the install overlay while the BAIN picker is up (see FOMOD above).
        self._hide_col_overlay()

        def _finish_ev(result):
            if done["v"]:
                return
            done["v"] = True
            holder["result"] = result
            self._tabs.close_tab("col_bain_picker")
            self._show_col_overlay()
            ev.set()

        view = BainPickerView(payload["subpkgs"], payload["root"], payload["name"],
                              on_done=lambda r: _finish_ev(r))
        view.destroyed.connect(lambda *_: _finish_ev(None))
        self._tabs.open_tab(view, self.tr("Install: {0}").format(payload['name']),
                            key="col_bain_picker")

    def _hide_col_overlay(self):
        """Hide the collection install overlay (e.g. while a deferred FOMOD/BAIN
        wizard tab is up so it isn't covered)."""
        ov = self._col_install_overlay
        if ov is not None:
            try:
                ov.hide()
            except Exception:
                pass

    def _show_col_overlay(self):
        """Re-show the collection install overlay after a deferred wizard closes
        (no-op if the install already finished + dismissed the overlay)."""
        ov = self._col_install_overlay
        if ov is not None:
            try:
                ov.show()
                ov.raise_()
            except Exception:
                pass

    def _on_col_limit_changed(self, mbps: float):
        """Overlay speed-limit spinbox changed: apply to in-flight downloads
        immediately (global token bucket) and persist for next time."""
        from Utils.downloads import bandwidth_limit
        from Utils.ui_config import save_download_speed_limit
        bandwidth_limit.set_limit_mbps(mbps)
        try:
            save_download_speed_limit(mbps)
        except Exception:
            pass

    # ---- pause / cancel --------------------------------------------------
    def _on_col_pause_clicked(self):
        ctl = self._col_install_control
        if ctl is not None:
            ctl.pause.set()
            ctl.stop.set()

    def _on_col_cancel_clicked(self):
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay

        def _done(confirmed):
            if not confirmed:
                return
            ctl = self._col_install_control
            if ctl is not None:
                ctl.cancel.set()
                ctl.pause.set()
                ctl.stop.set()
            if self._col_install_overlay is not None:
                self._col_install_overlay.set_status(self.tr("Cancelling…"))

        pname = getattr(self, "_col_profile_name", "") or ""
        if getattr(self, "_col_created_profile", False):
            msg = self.tr("This will stop the install and delete the new "
                          "profile '{0}'.").format(pname)
        else:
            msg = self.tr("This will stop the install. Profile '{0}' and its "
                          "already-installed mods will be kept.").format(pname)
        ConfirmOverlay.show_over(
            self, self.tr("Cancel install?"), msg,
            _done, confirm_label=self.tr("Cancel Install"),
            cancel_label=self.tr("Keep Going"))

    # ---- completion ------------------------------------------------------
    def _on_col_finished(self, kind, payload):
        """UI thread: handle the premium gate result AND the terminal states."""
        if kind == "_premium":
            if payload.get("manual"):
                # Non-premium (or [dev] force_manual_install): same pipeline,
                # but the manual overlay + sequential wait-for-download
                # producer replace the automatic download pool.
                self._notify(self.tr("Nexus Premium not detected — manual download "
                             "mode."), "info")
            # Route by intent (identical for premium and manual) — but only
            # once the preflight is happy with the game (see
            # _collection_preflight; it can fix the Skyrim runtime itself).
            intent = payload.get("intent", "install")

            def _route(payload=payload, intent=intent):
                if intent == "update":
                    self._run_collection_update(payload)
                elif intent == "resume":
                    self._resume_collection_install(payload)
                else:
                    # Ask the user how to install (New/Append/Continue) BEFORE
                    # creating any profile.
                    self._choose_collection_mode(payload)

            self._collection_preflight(payload, _route)
            return

        # Terminal states.
        self._col_install_running = False
        # run_collection_install checked the download cache for every off-site
        # (manifest source.type "browse"/"direct") mod and auto-installed any
        # it found there — control.unresolved_offsite is what's ACTUALLY still
        # missing, which supersedes the full off-site list the detail view
        # built before the install ran (self._col_offsite, set at _premium
        # time). Must read this before clearing the control below.
        if self._col_install_control is not None:
            _still_missing = getattr(self._col_install_control,
                                     "unresolved_offsite", None)
            if _still_missing is not None:
                self._col_offsite = list(_still_missing)
        # Mods that did not end up in the profile (the audit's structured list).
        self._col_failed = list(getattr(self._col_install_control, "failed_mods", None) or [])
        self._col_warnings = list(getattr(self._col_install_control, "audit_warnings", None) or [])
        self._col_install_control = None
        # A detached-wizard staging job may have been held off while this
        # collection install ran (see _run_staged_finish guard) — drain it now.
        if self._staged_finish_queue:
            QTimer.singleShot(0, self._run_staged_finish)
        # Release any Deploy/Restore deferred behind this collection install.
        self._drain_pending_after_staged()
        ov = self._col_install_overlay

        if kind == "failed":
            # Unexpected error in the install worker. Nothing is deleted: the
            # profile keeps everything installed so far and the downloaded
            # archives stay in the cache, so Install -> Continue picks up
            # where this stopped (already-installed mods are skipped by file id).
            pd = payload.get("profile_dir") if isinstance(payload, dict) else None
            err = payload.get("error", "") if isinstance(payload, dict) else ""
            if ov is not None:
                ov.finish(self.tr("Stopped by an error — your progress was kept."))
                QTimer.singleShot(4000, self._dismiss_col_overlay)
            profile_name = Path(pd).name if pd else ""
            if profile_name and pd and Path(pd).is_dir():
                self._select_installed_collection_profile(profile_name)
            self._refresh_open_collection_buttons()
            self._notify(
                self.tr("The collection install stopped: {0}. Nothing was deleted — "
                        "press Install again and choose Continue to resume.").format(err or self.tr("unexpected error")),
                "error", sticky=True)
            return

        if kind == "cancelled":
            pd = payload.get("profile_dir") if isinstance(payload, dict) else None
            if ov is not None:
                ov.set_status(self.tr("Cancelling…"))
            game = self._gs.game
            # GH#278: only a profile this run created may be deleted.
            delete_profile = bool(getattr(self, "_col_created_profile", False))

            def _cleanup_worker():
                from Utils.collections.collection_install import (
                    cleanup_cancelled_install,
                )
                from Utils.ui_config import load_clear_archive_after_install
                try:
                    cleanup_cancelled_install(
                        game, Path(pd) if pd else None,
                        delete_profile=delete_profile,
                        clear_cache=bool(load_clear_archive_after_install()),
                        log_fn=lambda m: self._op_log.emit(str(m)))
                except Exception as exc:
                    self._op_log.emit(f"[collection] cancel cleanup failed: {exc}")
                self._col_finished.emit("_cancel_cleaned", {
                    "deleted": delete_profile,
                    "profile": Path(pd).name if pd else ""})

            threading.Thread(target=_cleanup_worker, daemon=True,
                             name="col-cancel-cleanup").start()
            return

        if kind == "_cancel_cleaned":
            if ov is not None:
                ov.dismiss()
                self._col_install_overlay = None
            # cleanup_cancelled_install left game._active_profile_dir = None;
            # re-assert now because the switch below early-returns when the
            # target profile is already the active one.
            self._gs.reassert_active_profile()
            # Switch back to a sane profile + reload: the surviving target
            # profile when it was kept (continue/append/resume/update cancel),
            # else the default profile.
            kept = (isinstance(payload, dict) and not payload.get("deleted")
                    and payload.get("profile"))
            try:
                profs = self._gs.profiles()
                if kept and kept in profs:
                    self._on_profile_changed(kept)
                elif profs:
                    self._on_profile_changed(profs[0])
            except Exception:
                pass
            self._notify(self.tr("Collection install cancelled."), "info")
            return

        if kind == "paused":
            installed, profile_name = payload
            if ov is not None:
                ov.finish(self.tr("Paused — {0} installed.").format(installed))
                QTimer.singleShot(1500, self._dismiss_col_overlay)
            self._select_installed_collection_profile(profile_name)
            # Register the paused slug + refresh any open detail view so its
            # button flips to "Resume" (Tk parity — _PAUSED_INSTALLS).
            slug = getattr(self, "_col_install_slug", "") or ""
            if slug:
                try:
                    from gui_qt.collections.collection_detail_view import (
                        _PAUSED_COLLECTIONS,
                    )
                    _PAUSED_COLLECTIONS.add(slug)
                except Exception:
                    pass
            self._refresh_open_collection_buttons()
            self._notify(self.tr("Install paused — {0} mod(s) installed.").format(installed), "info")
            return

        # done
        installed, skipped_n, total, profile_name = payload
        # Local-manifest import: extract bundled mods + profile files from the
        # .mosaic over the freshly-installed profile, then reload. Handled on a
        # worker (unzipping can be sizeable) → reload marshaled back via a Signal.
        bundle_zip = getattr(self, "_col_bundle_zip", "") or ""
        self._col_bundle_zip = ""
        if bundle_zip and Path(bundle_zip).is_file():
            self._finish_import_bundle(bundle_zip, profile_name, ov,
                                       installed, total, skipped_n)
            return
        if ov is not None:
            ov.finish(self.tr("Done — {0}/{1} installed.").format(installed, total))
            QTimer.singleShot(1500, self._dismiss_col_overlay)
        self._select_installed_collection_profile(profile_name)
        msg = (self.tr("Loadout installed — {0}/{1} mod(s)") if getattr(self, "_col_loadout", None)
               else self.tr("Collection installed — {0}/{1} mod(s)")).format(installed, total)
        self._notify(msg + (self.tr(" ({0} skipped)").format(skipped_n) if skipped_n else ""),
                     "warning" if (getattr(self, "_col_failed", None)
                                   or getattr(self, "_col_warnings", None)) else "success")
        # Failed mods first (they need action), then the off-site reminder.
        self._show_failed_mods_report(then=self._show_offsite_reminder)

    def _dismiss_col_overlay(self):
        if self._col_install_overlay is not None:
            self._col_install_overlay.dismiss()
            self._col_install_overlay = None

    def _select_installed_collection_profile(self, profile_name, rescan_index=False):
        """Switch the profile selector to the freshly-installed collection profile
        and reload the panels + Open-Current. *rescan_index* forces a full mod-index
        rebuild (needed after an import extracts bundle mods straight to disk)."""
        try:
            profs = self._gs.profiles()
            self._profile_selector.set_items(profs, current=profile_name)
            self._gs.set_profile(profile_name)
            self._profile_selector.set_current(profile_name)
            # Rebuild pinned actions so "Remove current profile…" shows for the
            # freshly-created (removable) collection profile.
            self._refresh_profile_actions()
        except Exception as exc:
            self._append_log(f"[collection] profile switch failed: {exc}")
        self._reload_modlist(rescan_index=rescan_index)
        self._reload_plugins()
        try:
            self._update_deployed_profile_highlight()
        except Exception:
            pass

    # ---- Collections ▸ Reset load order ----------------------------------
    def _reset_collection_load_order(self):
        """Re-apply the active collection profile's intended load order from its
        manifest. No-op (with a toast) unless the active profile is a collection
        profile. Runs the file rewrites on a worker → toast + modlist reload."""
        if self._reset_running:
            self._notify(self.tr("A load-order reset is already running."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        pdir = self._gs.profile_dir()
        from Utils.exe_launch.game_helpers import get_collection_url_from_profile
        url = get_collection_url_from_profile(pdir) if pdir is not None else None
        if not url:
            self._notify(self.tr("The active profile isn't a collection profile."),
                         "warning")
            return

        self._reset_running = True
        self._notify(self.tr("Resetting collection load order…"), "info")
        domain = getattr(game, "nexus_game_domain", "") or ""
        game_name = getattr(game, "name", "") or ""
        # slug from the stored URL: …/collections/<slug>[/revisions/N]
        slug = ""
        try:
            after = url.split("/collections/", 1)[1]
            slug = after.split("/", 1)[0]
        except Exception:
            slug = ""


        def worker():
            res = {"error": "unknown"}
            try:
                from Utils.collections.collection_manifest import (
                    load_collection_manifest,
                )
                from Utils.collections.collection_reset import (
                    reset_collection_load_order,
                )
                # Prefer the profile's already-saved collection.json (offline).
                manifest = {}
                saved = pdir / "collection.json"
                if saved.is_file():
                    try:
                        manifest = json.loads(saved.read_text(encoding="utf-8"))
                    except Exception:
                        manifest = {}
                if not manifest and slug:
                    api = self._ensure_nexus_api()
                    if api is not None:
                        (_n, _s, _c, _mods, dl_path,
                         revs, _card) = api.get_collection_detail(slug, domain)
                        rev = None
                        try:
                            pub = [int(r.get("revisionNumber") or 0)
                                   for r in (revs or [])
                                   if (r.get("revisionStatus") or "").lower()
                                   == "published"]
                            rev = max(pub) if pub else None
                        except Exception:
                            rev = None
                        manifest = load_collection_manifest(
                            api, game_name, slug, rev, dl_path,
                            log_fn=lambda m: self._op_log.emit(str(m)))
                if not manifest:
                    res = {"error": "no_manifest"}
                else:
                    res = reset_collection_load_order(
                        pdir, manifest,
                        log_fn=lambda m: self._op_log.emit(str(m)),
                        game=game)
            except Exception as exc:
                self._op_log.emit(f"Reset load order failed: {exc}")
                res = {"error": str(exc)}
            self._reset_done.emit(res)

        threading.Thread(target=worker, daemon=True,
                         name="collection-reset").start()

"""Modlist row actions: Nexus endorse/track, mod.io like, requirement installs and batch downloads.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from gui_qt.app_parts.app_updates import _load_bg3_modio
from Utils.config_paths import get_download_cache_dir_for_game


class MainWindow:

    def _on_modlist_endorse(self, names, endorse: bool):
        """Endorse / abstain the given mods on Nexus (right-click). Runs on a
        worker thread via the shared API, updates each mod's meta.ini `endorsed`
        flag, then refreshes the modlist so the ★ flag icon updates. Mirrors Tk
        _vote_selected_mods."""
        names = [n for n in (names or []) if n]
        if not names:
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus."), "warning")
            return
        game = self._gs.game
        domain = getattr(game, "nexus_game_domain", "") or ""
        self._gs.reassert_active_profile()
        staging = self._gs.staging_dir()
        if staging is None or not domain:
            return
        msg = (self.tr("Endorsing {0} mod(s)…") if endorse
               else self.tr("Abstaining from {0} mod(s)…")).format(len(names))
        self._notify(msg, "info")

        def _worker():
            from Nexus.nexus_meta import read_meta, write_meta
            ok = 0
            for nm in names:
                meta_path = staging / nm / "meta.ini"
                if not meta_path.is_file():
                    continue
                try:
                    meta = read_meta(meta_path)
                    if not meta.mod_id:
                        continue
                    if endorse:
                        api.endorse_mod(domain, meta.mod_id, meta.version or "")
                    else:
                        api.abstain_mod(domain, meta.mod_id, meta.version or "")
                    meta.endorsed = endorse
                    write_meta(meta_path, meta)
                    ok += 1
                except Exception as exc:
                    self._op_log.emit(f"Nexus: {'endorse' if endorse else 'abstain'} "
                                      f"failed for '{nm}': {exc}")
            self._endorse_done.emit({"ok": ok, "endorse": endorse,
                                     "names": list(names)})

        threading.Thread(target=_worker, daemon=True, name="endorse").start()

    def _on_endorse_done(self, payload):
        """UI thread: report the endorse/abstain result + refresh the ★ flag."""
        ok = payload.get("ok", 0)
        if ok:
            msg = (self.tr("Endorsed {0} mod(s).") if payload.get("endorse")
                   else self.tr("Abstained from {0} mod(s).")).format(ok)
            self._notify(msg, "success")
            self._refresh_modlist_flags(payload.get("names"))
        else:
            self._notify(self.tr("No mods were updated (already in that state or no "
                         "Nexus id)."), "info")

    def _mod_has_nexus_id(self, mod_name: str) -> bool:
        """Whether *mod_name* has a linked Nexus mod ID in its meta.ini — the
        gate for showing the "Fetch name from Nexus" rename-dialog button.
        False for mods never matched to a Nexus page (installed offline, or
        from a non-Nexus source)."""
        staging = self._gs.staging_dir()
        if staging is None:
            return False
        meta_path = staging / mod_name / "meta.ini"
        if not meta_path.is_file():
            return False
        try:
            from Nexus.nexus_meta import read_meta
            return read_meta(meta_path).mod_id > 0
        except Exception:
            return False

    def _fetch_nexus_name(self, mod_name: str, overlay) -> None:
        """Populate *overlay*'s text field with *mod_name*'s real Nexus
        mod-page title ("Fetch name from Nexus" button) via a live ``get_mod``
        lookup on a background thread.

        Deliberately does NOT trust meta.ini's cached ``nexus_name`` field,
        even though install-time resolution usually sets it correctly — a
        user hit a real case where the field was present but this button
        still showed a stale/wrong name, and meta.ini gets rewritten by
        several other background passes after install (endorsement/
        requirements checks), so treating the cache as authoritative here
        risks quietly reproducing whatever wrote it wrong. This is an
        explicit, occasional user action, not something hit on every
        render, so the cost of one extra API call to guarantee a correct
        answer is worth it."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        meta_path = staging / mod_name / "meta.ini"
        try:
            from Nexus.nexus_meta import read_meta
            meta = read_meta(meta_path)
        except Exception:
            self._notify(self.tr("Could not read this mod's Nexus metadata."),
                        "warning")
            return
        if not meta.mod_id:
            return  # button shouldn't be visible in this case — safety net
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                        "warning")
            return
        domain = meta.game_domain or getattr(self._gs.game, "nexus_game_domain", "") or ""
        mod_id = meta.mod_id

        def _worker():
            try:
                info = api.get_mod(domain, mod_id)
                name = getattr(info, "name", "") or ""
                if name:
                    self._nexus_name_fetch_done.emit(overlay, name, "")
                else:
                    self._nexus_name_fetch_done.emit(
                        overlay, "", self.tr("Nexus returned no name for this mod."))
            except Exception as exc:
                self._nexus_name_fetch_done.emit(
                    overlay, "", self.tr("Fetch failed: {0}").format(exc))

        threading.Thread(target=_worker, daemon=True, name="fetch-nexus-name").start()

    def _on_nexus_name_fetch_done(self, overlay, name: str, error: str) -> None:
        """UI thread: apply a successful "Fetch name from Nexus" result, or
        report the error. The overlay may already be closed (Cancel/Esc/
        confirmed) by the time a slow fetch returns — set_text on a deleted
        Qt widget raises RuntimeError, which is expected and harmless here."""
        if error:
            self._notify(error, "warning")
            return
        try:
            overlay.set_text(name)
        except RuntimeError:
            pass

    def _ensure_modio_oauth(self):
        """Return a stored mod.io OAuth access token, or None (not logged
        in via Wizard ▸ mod.io API Key ▸ Log in with email). Mirrors the guard shape
        of _ensure_nexus_api, but there's no validate()/refresh step yet —
        see modio_oauth.py for why.

        Never raises: this is called on the UI thread from _on_check_updates,
        where a keyring failure (locked/absent backend) or a module-load error
        must not take down the whole update check. Same defensive shape as
        _modio_key_present."""
        try:
            tokens = _load_bg3_modio("modio_oauth").load_modio_tokens()
        except Exception as exc:
            self._append_log(f"mod.io: could not read the saved login ({exc}).")
            return None
        if not tokens:
            return None
        return tokens.get("access_token") or None

    def _on_modlist_modio_like(self, names, like: bool):
        """Like / unlike the given mods on mod.io (right-click). Runs on a
        worker thread, updates each mod's meta.ini `liked` flag, then
        refreshes the modlist so the flag icon updates. Mirrors
        _on_modlist_endorse; needs a mod.io OAuth login (not just the
        read-only API key — see modio_oauth.py)."""
        names = [n for n in (names or []) if n]
        if not names:
            return
        access_token = self._ensure_modio_oauth()
        if access_token is None:
            self._notify(
                self.tr("Log in to mod.io first: Wizard ▸ mod.io API Key ▸ "
                        "Log in with email."),
                "warning")
            return
        api_key = _load_bg3_modio("modio_key").load_modio_key()
        if not api_key:
            self._notify(self.tr("mod.io API key not configured."), "warning")
            return
        self._gs.reassert_active_profile()
        staging = self._gs.staging_dir()
        if staging is None:
            return
        msg = (self.tr("Liking {0} mod(s)…") if like
               else self.tr("Unliking {0} mod(s)…")).format(len(names))
        self._notify(msg, "info")

        def _worker():
            # NOTE: deliberately does NOT verify via get_my_ratings() after
            # posting — confirmed live (2026-08-29) that GET /me/ratings can
            # return stale data immediately after a successful POST (the POST
            # itself returns a clear success message, e.g. "Your previous
            # rating for this mod has been removed", while the very next GET
            # still shows the old value). mod.io's own POST response is the
            # only reliable success signal; trust it, mirroring how
            # _on_modlist_endorse trusts Nexus's endorse_mod() without a
            # separate read-back check.
            modio_api = _load_bg3_modio("modio_api")
            modio_meta = _load_bg3_modio("modio_meta")
            api = modio_api.ModioAPI(api_key)
            ok = 0
            for nm in names:
                meta_path = staging / nm / "meta.ini"
                if not meta_path.is_file():
                    continue
                try:
                    meta = modio_meta.read_modio_meta(meta_path)
                    if not meta.mod_id:
                        continue
                    api.rate_mod(meta.mod_id, 1 if like else 0, access_token)
                    meta.liked = like
                    modio_meta.write_modio_meta(meta_path, meta)
                    ok += 1
                except Exception as exc:
                    # mod.io's own error message is usually specific and
                    # actionable (e.g. "You have already submitted a negative
                    # rating for this mod", "This game is not currently
                    # accepting downvotes against mods") — surfaced as-is
                    # rather than guessing a cause.
                    self._op_log.emit(f"mod.io: {'like' if like else 'unlike'} "
                                      f"failed for '{nm}': {exc}")
            self._modio_like_done.emit({"ok": ok, "like": like, "names": list(names)})

        threading.Thread(target=_worker, daemon=True, name="modio-like").start()

    def _on_modio_like_done(self, payload):
        """UI thread: report the like/unlike result + refresh the flag."""
        ok = payload.get("ok", 0)
        if ok:
            msg = (self.tr("Liked {0} mod(s) on mod.io.") if payload.get("like")
                   else self.tr("Unliked {0} mod(s) on mod.io.")).format(ok)
            self._notify(msg, "success")
            self._refresh_modlist_flags(payload.get("names"))
        else:
            self._notify(self.tr("mod.io didn't update any mods — see the log "
                         "for its exact reason (already in that state, no "
                         "mod.io id, or a rejected request)."), "warning")

    def _on_modlist_track(self, names):
        """Start tracking the given mods on Nexus (right-click). Runs on a worker
        thread via the shared API, reading each mod's Nexus id from its meta.ini.
        Mirrors _on_modlist_endorse (track has no meta.ini flag to persist)."""
        names = [n for n in (names or []) if n]
        if not names:
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus."), "warning")
            return
        game = self._gs.game
        domain = getattr(game, "nexus_game_domain", "") or ""
        self._gs.reassert_active_profile()
        staging = self._gs.staging_dir()
        if staging is None or not domain:
            return
        self._notify(self.tr("Tracking {0} mod(s)…").format(len(names)), "info")

        def _worker():
            from Nexus.nexus_meta import read_meta
            ok = 0
            for nm in names:
                meta_path = staging / nm / "meta.ini"
                if not meta_path.is_file():
                    continue
                try:
                    meta = read_meta(meta_path)
                    if not meta.mod_id:
                        continue
                    api.track_mod(domain, meta.mod_id)
                    ok += 1
                except Exception as exc:
                    self._op_log.emit(f"Nexus: track failed for '{nm}': {exc}")
            self._track_done.emit({"ok": ok})

        threading.Thread(target=_worker, daemon=True, name="track").start()

    def _on_track_done(self, payload):
        """UI thread: report the track result."""
        ok = payload.get("ok", 0)
        if ok:
            self._notify(self.tr("Tracking {0} mod(s).").format(ok), "success")
        else:
            self._notify(self.tr("No mods were tracked (no Nexus id)."), "info")

    def _on_copy_done(self, payload):
        """UI thread: report the copy/move result. For a move, drop the moved
        mods' rows from this profile's modlist (remove_mods left modlist.txt to
        us — mirrors Tk _finish_copy_popup calling _remove_selected_mods), which
        persists modlist.txt via the model, then reload."""
        self._copy_running = False
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        c = payload.get("copied", 0)
        self._notify(
            (self.tr("Moved {0}/{1} mod(s) to '{2}'.") if payload.get("move")
             else self.tr("Copied {0}/{1} mod(s) to '{2}'."))
            .format(c, payload.get('total', 0), payload.get('target', '')),
            "success" if c else "info")
        removed_names = set(payload.get("removed_names") or [])
        if removed_names:
            model = self._modlist_model
            # Remove by name, highest row first (indices shift as we delete).
            rows = [r for r in range(model.rowCount())
                    if (e := model.entry(r)) is not None
                    and not e.is_separator and e.name in removed_names]
            for r in sorted(rows, reverse=True):
                model.remove_row(r, save=False)
            if rows:
                model.save()  # single save → one filemap rebuild for the batch
        if payload.get("removed"):
            self._reload_modlist()

    def _new_dl_key(self) -> str:
        """Unique tracking key for one download operation, so concurrent
        downloads can be told apart in the combined progress card."""
        self._dl_seq = getattr(self, "_dl_seq", 0) + 1
        return f"dl-{self._dl_seq}"

    def _nexus_download_progress(self, key: str, name: str,
                                 downloaded: int, total: int):
        """Drive the combined Nexus-download progress card. UI thread only.
        All concurrent downloads (each identified by *key*) aggregate into ONE
        card: the bar shows summed bytes across them. Finished downloads stay
        in the totals until the last one completes, so the bar never jumps
        backwards. total<0 marks *key* finished (done/failed/cancelled)."""
        self._ensure_feedback()
        if self._progress_popup is None:
            return
        dls = self._active_downloads
        if total < 0:
            e = dls.get(key)
            if e is not None and e["total"] > 0:
                e["done"] = e["total"]
                e["fin"] = True
            else:
                dls.pop(key, None)   # size never reported — just drop it
        else:
            e = dls.setdefault(key, {"fin": False})
            e["name"], e["done"], e["total"] = name, downloaded, total
        active = [e for e in dls.values() if not e["fin"]]
        if not active:
            dls.clear()
            self._progress_popup.clear(key="downloads")
            return
        if len(dls) == 1:
            nm = active[0].get("name") or ""
            phase = (self.tr("Downloading {0}…").format(nm)
                     if nm else self.tr("Downloading…"))
        else:
            phase = self.tr("Downloading {0} files ({1} remaining)…").format(
                len(dls), len(active))
        done = sum(e["done"] for e in dls.values() if e["total"] > 0)
        tot = sum(e["total"] for e in dls.values())
        if any(e["total"] <= 0 for e in active):
            done = tot = 0   # a size is still unknown — indeterminate bar
        self._progress_popup.set_progress(
            done, tot, phase, title=self.tr("Nexus Download"),
            bytes_mode=True, key="downloads")

    def _resolve_req_premium(self, api) -> bool:
        """Worker thread: is the account premium (direct downloads)? Falls back
        to the last-known status on a validate() failure, and honours the
        [dev] force_manual_install switch."""
        from Utils.ui_config import load_nexus_last_premium, save_nexus_last_premium
        try:
            user = api.validate()
            premium = bool(getattr(user, "is_premium", False))
            try:
                save_nexus_last_premium(premium)
            except Exception:
                pass
        except Exception as exc:
            # GH#278: a transient validate() failure (network hiccup, rate
            # limit — most likely to hit the very first Nexus call of a
            # session, racing _ensure_nexus_api()'s own startup validate())
            # must not silently demote a premium user to manual mode, nor
            # abort the install outright — fall back to the last
            # successfully-validated status and keep going.
            premium = bool(load_nexus_last_premium())
            self._append_log(f"[nexus] premium check failed: {exc} — using "
                             f"last-known status "
                             f"({'premium' if premium else 'not premium'})")
        if premium:
            # [dev] force_manual_install = true → exercise the manual
            # browser-download flow even on a premium account (same
            # switch as the browser / Change Version / reinstall).
            from Utils.ui_config import load_force_manual_install
            if load_force_manual_install():
                self._append_log("[nexus] [dev] force_manual_install — "
                                 "using the manual browser-download flow.")
                premium = False
        return premium

    def _install_nexus_mods_batch(self, items):
        """Missing Requirements "Install selected": *items* is
        [(mod_id, domain, name), ...]. Fetch every file list, let the user
        review them in one batch chooser, then download (premium: concurrently)
        or open the browser pages one at a time (non-premium)."""
        if self._req_installing or self._req_batch_busy or self._req_batch_key:
            self._notify(self.tr("An install is already in progress."), "info")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in to Nexus first."), "warning")
            return
        items = [(int(m or 0), d, n) for m, d, n in items if int(m or 0) > 0]
        if not items:
            return
        self._req_batch_busy = True
        self._append_log(f"[nexus] fetching file lists for {len(items)} mod(s)…")
        from types import SimpleNamespace

        def worker():
            premium = self._resolve_req_premium(api)

            def files_for(it):
                mid, dom, nm = it
                entry = SimpleNamespace(mod_id=mid, name=nm, domain_name=dom)
                try:
                    return entry, list(api.get_mod_files(dom, mid).files)
                except Exception as exc:
                    self._append_log(f"[nexus] couldn't fetch files for {nm}: {exc}")
                    return entry, None

            with ThreadPoolExecutor(max_workers=4) as pool:
                mods = list(pool.map(files_for, items))
            self._req_batch_ready.emit(mods, premium)

        threading.Thread(target=worker, daemon=True,
                         name="req-batch-files").start()

    def _on_req_batch_ready(self, mods, premium):
        from gui_qt.nexus.nexus_batch_chooser import NexusBatchChooser

        def _done(plan):
            self._req_batch_busy = False
            if not plan:
                self._append_log("[nexus] install selected cancelled.")
                return
            if premium:
                self._start_req_batch_downloads(plan)
            else:
                self._req_batch_queue = [
                    (e, f) for e, files in plan for f in files]
                self._append_log(
                    f"[nexus] {len(self._req_batch_queue)} file(s) to download "
                    f"in your browser, one at a time. Click Slow download on "
                    f"each Nexus page that opens; the next one opens once the "
                    f"file has arrived.")
                self._advance_req_batch()

        NexusBatchChooser.show_over(self, mods, _done)

    def _start_req_batch_downloads(self, plan):
        try:
            from Utils.ui_config import load_collection_settings
            limit = max(1, int(load_collection_settings().get("max_concurrent", 8)))
        except Exception:
            limit = 4
        sem = threading.BoundedSemaphore(limit)
        n_files = sum(len(files) for _e, files in plan)
        self._append_log(f"[nexus] downloading {n_files} file(s) from "
                         f"{len(plan)} mod(s), up to {limit} at a time…")
        for entry, files in plan:
            ctx = {"mod_id": entry.mod_id, "domain": entry.domain_name,
                   "name": entry.name}
            for f in files:
                self._start_req_download(ctx, f, sem=sem)

    def _advance_req_batch(self):
        """Open the next queued file's download page (non-premium). Called
        again when the current file's watch ends — archive found, timed out,
        or cancelled (incl. a 'Download with Mod Manager' that took over)."""
        self._req_batch_key = ""
        if not self._req_batch_queue:
            self._update_req_batch_ui()
            return
        entry, f = self._req_batch_queue.pop(0)
        ctx = {"mod_id": entry.mod_id, "domain": entry.domain_name,
               "name": entry.name}
        self._req_batch_key = self._start_req_manual(ctx, f) or ""
        if not self._req_batch_key:
            self._advance_req_batch()
        else:
            self._append_log(
                f"[nexus] {len(self._req_batch_queue)} more file(s) queued "
                "after this one.")
            self._update_req_batch_ui()

    def _update_req_batch_ui(self):
        v = getattr(self, "_missing_reqs_view", None)
        if v is not None:
            v.set_queue_status(bool(self._req_batch_key),
                               len(self._req_batch_queue))

    def _stop_req_batch(self):
        """Drop the queued files not yet opened. The current file's watch keeps
        running — its download may already be under way in the browser."""
        pending = len(self._req_batch_queue)
        self._req_batch_queue = []
        self._req_batch_key = ""
        self._append_log(f"[nexus] stopped the browser download queue "
                         f"({pending} file(s) not opened).")
        self._update_req_batch_ui()

    def _req_batch_watch_ended(self, dl_key):
        if self._req_batch_key and dl_key == self._req_batch_key:
            self._advance_req_batch()

    def _install_nexus_mod_by_id(self, mod_id: int, domain: str, name: str):
        if self._req_installing:
            self._notify(self.tr("An install is already in progress."), "info")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in to Nexus first."), "warning")
            return
        mod_id = int(mod_id or 0)
        if mod_id <= 0:
            self._notify(self.tr("That requirement has no Nexus mod page."), "warning")
            return
        self._req_installing = True
        ctx = {"mod_id": mod_id, "domain": domain, "name": name}
        self._append_log(f"[nexus] preparing install for {name}…")

        def worker():
            files = None
            premium = self._resolve_req_premium(api)
            try:
                # Fetch the file list for BOTH tiers: the chooser needs it, and
                # the non-premium folder watcher needs real names + sizes.
                resp = api.get_mod_files(domain, mod_id)
                files = list(resp.files)
            except Exception as exc:
                self._append_log(f"[nexus] install prep failed: {exc}")
                if not premium:
                    # No file list → no watch possible; fall back to the plain
                    # files page (site 'Download with Mod Manager' still works).
                    from Utils.xdg import open_url
                    open_url(f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
                             f"?tab=files", log_fn=self._append_log)
                    self._append_log("[nexus] premium required for direct "
                                     "download — opened the files page (the "
                                     "download won't auto-install; use "
                                     "'Download with Mod Manager').")
            ctx["premium"] = premium
            self._req_install_files.emit(ctx, files)

        threading.Thread(target=worker, daemon=True, name="req-install-files").start()

    def _on_req_install_files(self, ctx, files):
        """UI thread: pick which file to install (chooser if >1 main/optional/misc),
        then route to the API download (premium) or the shared manual
        browser-download flow (non-premium / [dev] force_manual_install)."""
        if files is None:           # fetch error (fallback already handled)
            self._req_installing = False
            return
        from gui_qt.nexus.nexus_file_chooser import NexusFileChooser, installable_files
        picks = installable_files(files)
        if not picks:
            self._notify(self.tr("No downloadable files for that mod."), "warning")
            self._req_installing = False
            return
        start = (self._start_req_download if ctx.get("premium")
                 else self._start_req_manual)
        if len(picks) > 1:
            def _picked(chosen):
                if chosen is None:
                    self._req_installing = False
                    return
                start(ctx, chosen)

            NexusFileChooser.show_over(self, ctx["name"], picks, _picked)
        else:
            start(ctx, picks[0])

    def _start_req_download(self, ctx, f, sem=None):
        domain, mod_id, name = ctx["domain"], ctx["mod_id"], ctx["name"]
        dl_label = f.file_name or name
        dl_key = self._new_dl_key()
        self._append_log(f"[nexus] downloading {dl_label}…")
        self._nexus_download_progress(dl_key, dl_label, 0, 0)   # show popup immediately

        class _Info:
            pass
        info_fallback = _Info()
        info_fallback.mod_id = mod_id
        info_fallback.domain_name = domain
        info_fallback.name = name

        def worker():
            archive = meta = None
            if sem is not None:
                sem.acquire()
            try:
                from Nexus.nexus_download import NexusDownloader
                from Nexus.nexus_meta import build_meta_from_download
                from Utils.config_paths import get_download_cache_dir_for_game
                dest = get_download_cache_dir_for_game(
                    getattr(self._gs.game, "name", "") or "")
                size = (f.size_in_bytes or 0) or (f.size_kb * 1024)
                result = NexusDownloader(
                    self._nexus_api, download_dir=dest).download_file(
                    game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                    dest_dir=dest, known_file_name=f.file_name,
                    expected_size_bytes=size,
                    progress_cb=lambda d, t: self._req_install_prog.emit(
                        dl_key, dl_label, int(d), int(t)))
                if result.success and result.file_path is not None:
                    archive = str(result.file_path)
                    try:
                        # The bare fallback stub only carries mod_id/domain/name —
                        # fetch the real mod info (author, category, description,
                        # uploader) so this Missing-Requirements install writes a
                        # complete meta.ini, same as a direct Nexus/nxm install
                        # does, instead of leaving it to a later Check Updates
                        # pass to backfill.
                        mod_info = info_fallback
                        try:
                            fetched, _fi = self._nexus_api.get_mod_and_file_info_graphql(
                                domain, mod_id, f.file_id)
                            if fetched is not None:
                                mod_info = fetched
                        except Exception:
                            pass
                        meta = build_meta_from_download(
                            game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                            archive_name=result.file_name, mod_info=mod_info,
                            file_info=f)
                    except Exception:
                        meta = None
                else:
                    self._append_log(f"[nexus] download failed: "
                                     f"{result.error or 'unknown error'}")
            except Exception as exc:
                self._append_log(f"[nexus] download error: {exc}")
            finally:
                if sem is not None:
                    sem.release()
            self._req_install_dl.emit(archive, meta, dl_key)

        threading.Thread(target=worker, daemon=True, name="req-install-dl").start()

    def _start_req_manual(self, ctx, f):
        """Non-premium (or [dev] force_manual_install) Missing-Requirements
        install via the shared start_manual_install flow: skip the browser when
        the archive is already downloaded, else open the file's download page +
        watch the download folders. 'Download with Mod Manager' still works —
        that nxm:// flow installs it and cancels this watch."""
        from gui_qt.safe_emit import safe_emit
        from Nexus.manual_download_watch import start_manual_install
        from Utils.xdg import open_url
        domain, mod_id, name = ctx["domain"], ctx["mod_id"], ctx["name"]
        dl_label = f.file_name or name
        dl_key = self._new_dl_key()
        self._nexus_download_progress(dl_key, dl_label, 0, 0)  # show popup card

        class _Info:            # requirement rows carry no NexusModInfo —
            pass                # minimal fallback for build_meta_from_download
        info = _Info()
        info.mod_id = mod_id
        info.domain_name = domain
        info.name = name

        # Helper callbacks run on the WATCHER thread — marshal via Signals.
        def on_archive(path, meta, _file):
            if not self._claim_app_manual_watch(mod_id, dl_key):
                return
            safe_emit(self._req_install_dl, str(path), meta, dl_key)

        def on_progress(done, total):
            safe_emit(self._req_install_prog, dl_key, dl_label, int(done), int(total))

        def on_timeout():
            if not self._claim_app_manual_watch(mod_id, dl_key):
                return
            self._op_log.emit(f"[nexus] stopped waiting for a browser download "
                              f"of '{dl_label}' (nothing arrived).")
            safe_emit(self._req_install_dl, None, None, dl_key)

        self.cancel_app_manual_watch(mod_id)   # re-click → fresh watch
        watcher, _already = start_manual_install(
            api=self._nexus_api, game_domain=domain, mod_id=mod_id, files=[f],
            open_url_fn=lambda u: open_url(u, log_fn=self._append_log),
            log_fn=self._append_log, log_label=dl_label,
            mod_info_fallback=info,
            on_archive=on_archive, on_progress=on_progress,
            on_timeout=on_timeout,
            cache_dir=get_download_cache_dir_for_game(
                getattr(self._gs.game, "name", "") or ""))
        self._app_manual_watchers[int(mod_id or 0)] = (watcher, dl_key)
        # The watch runs in the background — release the guard so the user can
        # install other missing requirements meanwhile (watches are per-mod).
        self._req_installing = False
        return dl_key

    def _on_req_install_dl(self, archive, meta, dl_key):
        self._req_installing = False
        self._req_batch_watch_ended(dl_key)
        self._nexus_download_progress(dl_key, "", 0, -1)   # hide this download's card
        if not archive:
            return
        self._append_log(f"[nexus] downloaded → {archive}; installing…")
        self._install_paths([archive],
                            {archive: meta} if meta is not None else None)

    def _on_modlist_flag_clicked(self, row: int, flag: int):
        """A flag icon in the modlist Flags column was clicked → its action
        (Tk parity, gui/modlist_panel ~3960): update→Change Version,
        modio-update→Quick Update (mod.io), missing→Missing Requirements,
        note→note editor, bundle→Bundle Options."""
        from gui_qt.modlist.modlist_data import (
            FLAG_BUNDLE,
            FLAG_MISSING_REQS,
            FLAG_MODIO_UPDATE,
            FLAG_NOTE,
            FLAG_RERUN_FOMOD,
            FLAG_UPDATE,
        )
        e = self._modlist_model.entry(row)
        if e is None or e.is_separator:
            return
        if flag == FLAG_UPDATE:
            self._open_change_version_tab(e.name)
        elif flag == FLAG_RERUN_FOMOD:
            # Re-run the FOMOD installer for this mod (its archive is re-extracted
            # and the wizard reopens). Reinstall rewrites fomodPendingDeps, so the
            # flag self-corrects once the now-relevant option is selected.
            self._reinstall_mods([e.name])
        elif flag == FLAG_MODIO_UPDATE:
            # Mirrors Nexus's FLAG_UPDATE: opens the Change Version (mod.io)
            # picker instead of installing immediately, so the user can review
            # the file history (+ changelog) before anything downloads.
            # Quick Update (mod.io) — the old one-click behaviour — is still
            # available from the right-click menu.
            self._open_change_version_modio_tab(e.name)
        elif flag == FLAG_MISSING_REQS:
            self._open_missing_reqs_tab(e.name)
        elif flag == FLAG_NOTE:
            from gui_qt.modlist.modlist_menu import _open_note_editor
            _open_note_editor(self._modlist_view, [e.name])
        elif flag == FLAG_BUNDLE:
            self._open_bundle_tab(e.name)

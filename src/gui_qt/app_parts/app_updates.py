"""Check for updates, reinstall/redownload, and Nexus / mod.io quick-update flows.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import concurrent.futures as _cf
import importlib.util
import sys as _sys
import threading
import time as _time
from pathlib import Path

from Utils.config_paths import get_download_cache_dir_for_game


def _load_bg3_modio(stem: str):
    """Load a Games/Baldur's Gate 3/<stem>.py module by file path (the folder
    name has a space, so it isn't importable by dotted path). Cached in
    sys.modules under f"{stem}_bg3" — shared with the Tk copy and the wizard."""
    mod_name = f"{stem}_bg3"
    cached = _sys.modules.get(mod_name)
    if cached is not None:
        return cached
    bg3_dir = (Path(__file__).resolve().parent.parent.parent
               / "Games" / "Baldur's Gate 3")
    spec = importlib.util.spec_from_file_location(mod_name, str(bg3_dir / f"{stem}.py"))
    module = importlib.util.module_from_spec(spec)
    _sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _modio_key_present(game) -> bool:
    """True if this is BG3 and a mod.io API key is configured."""
    try:
        if getattr(game, "game_id", "") != "baldurs_gate_3":
            return False
        return bool(_load_bg3_modio("modio_key").load_modio_key())
    except Exception:
        return False


def _check_modio_updates(game, staging, log_fn, only_names=None, access_token=None):
    """BG3-only mod.io update check. Returns a list of ModioUpdateInfo (mods
    with a newer file, or unknown installed version), or [] for other games /
    when no mod.io key is configured. *only_names* restricts the check to those
    staging folder names. *access_token* (mod.io email login), when given,
    also syncs Like/Unlike state from mod.io — see check_for_updates's own
    docstring for why this is a Check-for-Updates concern, not something
    done right after a Like/Unlike. Never raises."""
    try:
        if getattr(game, "game_id", "") != "baldurs_gate_3":
            return []
        api_key = _load_bg3_modio("modio_key").load_modio_key()
        if not api_key:
            return []
        checker = _load_bg3_modio("modio_update_checker")
        return checker.check_for_updates(
            Path(staging), api_key, progress_cb=log_fn, only_names=only_names,
            access_token=access_token)
    except Exception as e:
        log_fn(f"mod.io: update check failed — {e}")
        return []

def _modhold_present(game, staging) -> bool:
    """True when this game has a Modhold slug and a mod in *staging* came from
    Modhold. Reading a mod needs no key, so no login is required to check."""
    try:
        if not getattr(game, "modhold_game_slug", ""):
            return False
        from Modhold.modhold_update_checker import has_modhold_mods
        return has_modhold_mods(Path(staging))
    except Exception:
        return False


def _check_modhold_updates(staging, log_fn, only_names=None):
    """Modhold update check. Returns a list of ModholdUpdateInfo (mods with a newer
    file, or an unknown installed file). *only_names* restricts the check to those
    staging folder names. Never raises."""
    try:
        from Modhold.modhold_api import ModholdAPI
        from Modhold.modhold_update_checker import check_for_updates
        return check_for_updates(Path(staging), ModholdAPI(), progress_cb=log_fn,
                                 only_names=only_names)
    except Exception as e:
        log_fn(f"Modhold: update check failed — {e}")
        return []


class MainWindow:

    def _on_check_updates(self, names=None):
        """Check for mod updates + missing requirements. Runs the Nexus check
        and (for BG3, when a mod.io key is set) the mod.io check in parallel —
        mirroring the Tk _run_check_updates. *names* limits the check to a set
        of mod folder names (right-click subset); None = all."""
        if self._updates_running:
            self._notify(self.tr("An update check is already running."), "info")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # A background worker (restore, collection rollback, deploy) can leave
        # game._active_profile_dir stale or None, which makes staging_dir()
        # silently fall back to the SHARED mods/ folder — empty for a
        # profile_specific_mods profile. Everything downstream (the meta.ini
        # writes here, and the flag refresh in _on_updates_ready) would then
        # read the wrong folder. Same guard as the per-mod handlers below.
        self._gs.reassert_active_profile()

        domain = getattr(game, "nexus_game_domain", "") or ""
        api = self._ensure_nexus_api() if domain else None
        have_nexus = domain and api is not None
        have_modio = _modio_key_present(game)
        # Optional — only used to sync Like/Unlike state (see
        # _check_modio_updates); None (not logged in via email) just skips
        # that part, update-checking itself only ever needed the api_key.
        modio_access_token = self._ensure_modio_oauth()
        # Modhold needs no login to read a mod, so a profile with only Modhold mods
        # (and no Nexus login) can still check for updates.
        _staging_pre = self._gs.staging_dir()
        have_modhold = _staging_pre is not None and _modhold_present(game, _staging_pre)

        # mod.io (BG3) can run without a Nexus login. Only bail for "needs Nexus
        # login" when there's also no mod.io key to fall back on.
        if not have_nexus and not have_modio and not have_modhold:
            if getattr(game, "game_id", "") == "baldurs_gate_3":
                self._notify(
                    self.tr("Log in to Nexus (Nexus ▸ Login) or set a mod.io API key "
                    "(Wizard ▸ mod.io API Key) to check for updates."), "warning")
            elif not domain:
                self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            else:
                self._notify(
                    self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                    "warning")
            return

        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return

        # Normalise the subset to a set (or None for "all").
        subset = set(names) if names else None
        self._updates_running = True
        btn = getattr(self, "_check_updates_btn", None)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText(self.tr("Checking…"))
        n = len(subset) if subset else "all"
        # Sticky toast: stays on screen until _on_updates_ready dismisses it,
        # so it doesn't vanish mid-check on the transient auto-dismiss timer.
        self._updates_toast = self._notify(
            self.tr("Checking for updates ({0})…").format(n), "info", sticky=True)

        from Nexus.nexus_update_checker import check_for_updates

        def _worker():
            # Carry the checked subset (None = all) so _on_updates_ready can do a
            # scoped, filemap-free flag refresh instead of a full reload.
            # nexus_skipped: the game has a Nexus page but we proceeded without
            # a Nexus login (only possible because a mod.io key covered the
            # early guard above) — surfaced in _on_updates_ready so this never
            # silently reads as "all mods up to date" when Nexus wasn't
            # actually checked.
            out = {
                "nexus": None, "modio": [], "modhold": [], "subset": subset,
                "nexus_skipped": bool(domain and not have_nexus),
            }
            try:
                # Run the mod.io check (BG3) in parallel with the Nexus check —
                # they hit different APIs and write disjoint meta.ini keys.
                modio_box = {"results": []}

                def _modio_work():
                    modio_box["results"] = _check_modio_updates(
                        game, staging,
                        lambda m: self._op_log.emit(f"[modio] {m}"),
                        only_names=subset, access_token=modio_access_token)

                modio_thread = None
                if have_modio:
                    modio_thread = threading.Thread(
                        target=_modio_work, daemon=True, name="check-modio")
                    modio_thread.start()

                # Modhold runs alongside too: its own API, its own meta.ini keys.
                modhold_box = {"results": []}

                def _modhold_work():
                    modhold_box["results"] = _check_modhold_updates(
                        staging, lambda m: self._op_log.emit(f"[modhold] {m}"),
                        only_names=subset)

                modhold_thread = None
                if have_modhold:
                    modhold_thread = threading.Thread(
                        target=_modhold_work, daemon=True, name="check-modhold")
                    modhold_thread.start()

                if have_nexus:
                    try:
                        out["nexus"] = check_for_updates(
                            api, staging, game_domain=domain, save_results=True,
                            enabled_only=subset,
                            progress_cb=lambda m: self._op_log.emit(f"[nexus] {m}"),
                        )
                    except Exception as exc:
                        self._op_log.emit(f"[nexus] update check failed: {exc}")

                if modio_thread is not None:
                    modio_thread.join()
                    out["modio"] = modio_box["results"]
                if modhold_thread is not None:
                    modhold_thread.join()
                    out["modhold"] = modhold_box["results"]
            except Exception as exc:
                self._append_log(f"update check failed: {exc}")
                self._updates_ready.emit(None)
                return
            self._updates_ready.emit(out)

        threading.Thread(target=_worker, daemon=True, name="check-updates").start()

    def _on_updates_ready(self, result):
        """Worker finished — re-enable the button, summarise, refresh the rows.
        The sticky "Checking…" toast is dismissed here (morphed into the final
        summary) so it never disappears while the check is still running."""
        self._updates_running = False
        btn = getattr(self, "_check_updates_btn", None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText(self.tr("Check Updates"))
        # Turn the sticky "Checking…" toast into the final summary (or drop it).
        toast = getattr(self, "_updates_toast", None)
        self._updates_toast = None

        def _finish(text: str, state: str):
            if toast is not None:
                toast.dismiss(text, state=state)
            else:
                self._notify(text, state)

        if result is None:
            _finish(self.tr("Update check failed — see the log."), "error")
            return

        nexus = result.get("nexus")
        modio = result.get("modio") or []
        parts = []
        if nexus is not None:
            updates, missing = nexus
            if updates:
                parts.append(f"Nexus: {len(updates)} update"
                             f"{'s' if len(updates) != 1 else ''}")
            if missing:
                parts.append(f"{len(missing)} missing requirement"
                             f"{'s' if len(missing) != 1 else ''}")
        modio_updates = [u for u in modio if not getattr(u, "unknown", False)]
        modio_unknown = [u for u in modio if getattr(u, "unknown", False)]
        if modio_updates:
            parts.append(f"mod.io: {len(modio_updates)} update"
                         f"{'s' if len(modio_updates) != 1 else ''}")
        if modio_unknown:
            parts.append(f"{len(modio_unknown)} mod.io version"
                         f"{'s' if len(modio_unknown) != 1 else ''} unknown")
        modhold = result.get("modhold") or []
        modhold_updates = [u for u in modhold if not getattr(u, "unknown", False)]
        modhold_unknown = [u for u in modhold if getattr(u, "unknown", False)]
        if modhold_updates:
            parts.append(f"Modhold: {len(modhold_updates)} update"
                         f"{'s' if len(modhold_updates) != 1 else ''}")
        if modhold_unknown:
            parts.append(f"{len(modhold_unknown)} Modhold version"
                         f"{'s' if len(modhold_unknown) != 1 else ''} unknown")
        if result.get("nexus_skipped"):
            # Nexus has a page for this game but wasn't actually queried (no
            # valid login) — say so explicitly rather than letting an empty
            # `nexus` result read as "checked, nothing found".
            parts.append(self.tr("Nexus: not logged in"))

        if parts:
            _finish(", ".join(parts) + ".", "warning")
        else:
            _finish(self.tr("All mods are up to date."), "info")
        # Check Updates only rewrote meta.ini flags (update / missing-reqs) —
        # the deployed file set is unchanged, so skip the full reload + filemap
        # rebuild and just re-read the affected flags (endorse/note parity).
        # subset is None for a full check (re-reads all flags) or the checked
        # names for a right-click subset; either way, no filemap rebuild.
        self._refresh_modlist_flags(result.get("subset"))

    def _reinstall_mods(self, mod_names):
        """Reinstall one or more mods from their recorded installation archives
        (Tk parity, gui/modlist_nexus_actions._reinstall_mod). Each mod's archive
        is located across the Downloads dir + configured caches + extra locations
        (Utils.downloads.download_locations / config_paths); mods with no on-disk archive
        are skipped with a log line. Resolved archives go through _install_paths
        with the mod's existing folder name forced (silent Replace-All, keeping
        the modlist position + endorsed flag), so no Mod-Already-Exists dialog."""
        if getattr(self, "_install_running", False):
            self._notify(self.tr("An install is already in progress."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        names = list(mod_names or [])
        if not names:
            return

        from gui_qt.modlist.modlist_menu import _installation_archive, _read_mod_meta
        preferred: dict[str, str] = {}   # archive path → forced folder name
        paths: list[str] = []
        redownload: list[tuple] = []     # (mod_name, domain, mod_id, file_id, filename)
        missing: list[str] = []          # no archive AND no Nexus info to redownload
        for nm in names:
            arc = _installation_archive(self._modlist_view, nm)
            if arc is not None:
                paths.append(str(arc))
                preferred[str(arc)] = nm
                continue
            # Archive gone — fall back to a Nexus redownload if this mod carries
            # a modid + fileid (and a resolvable game domain) in its meta.ini.
            meta = _read_mod_meta(self._modlist_view, nm)
            mod_id = int(getattr(meta, "mod_id", 0) or 0) if meta is not None else 0
            file_id = int(getattr(meta, "file_id", 0) or 0) if meta is not None else 0
            from Nexus.nexus_meta import normalise_game_domain
            domain = normalise_game_domain(
                getattr(meta, "game_domain", "") or "") if meta is not None else ""
            if not domain:
                domain = getattr(game, "nexus_game_domain", "") or ""
            if mod_id > 0 and file_id > 0 and domain:
                redownload.append((nm, domain, mod_id, file_id,
                                   getattr(meta, "installation_file", "") or ""))
            else:
                missing.append(nm)
                self._append_log(f"[reinstall] {nm} — install archive not found and "
                                 "no Nexus mod/file id to redownload, skipped.")

        if not paths and not redownload:
            self._notify(self.tr("No install archive found for the selected mod(s)."),
                         "warning")
            return
        if missing:
            self._notify(
                self.tr("Reinstalling {0} mod(s); {1} skipped "
                "(no archive found).").format(
                    len(paths) + len(redownload), len(missing)), "info")

        # Redownload-only reinstalls go through the Nexus path (premium download
        # or browser fallback). If some mods still have their archive, install
        # those now and redownload the rest afterwards.
        if paths:
            # clear_archives=False: reinstall CONSUMES an existing archive the
            # user kept — deleting it would make the next reinstall impossible.
            self._install_paths(paths, preferred_names=preferred,
                                clear_archives=False)
        if redownload:
            self._redownload_and_reinstall(redownload)

    def _redownload_and_reinstall(self, items):
        """Reinstall mods whose install archive is gone by redownloading the
        exact recorded file from Nexus (modid + fileid from meta.ini), then
        installing via _install_paths with the mod's folder name forced (silent
        Replace-All). Premium users get a direct download; non-premium users get
        each mod's Nexus files page opened in the browser (site 'Download with
        Mod Manager' flow). `items` = [(mod_name, domain, mod_id, file_id,
        filename), …]."""
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(
                self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                "warning")
            return
        game = self._gs.game
        from Utils.ui_config import load_nexus_last_premium, save_nexus_last_premium
        try:
            is_premium = bool(api.validate().is_premium)
            try:
                save_nexus_last_premium(is_premium)
            except Exception:
                pass
        except Exception as exc:
            # GH#278: a transient validate() failure (network hiccup, rate
            # limit) must not silently demote a premium user to manual mode
            # — fall back to the last successfully-validated status.
            is_premium = bool(load_nexus_last_premium())
            self._append_log(f"[reinstall] premium check failed: {exc} — "
                             f"using last-known status "
                             f"({'premium' if is_premium else 'not premium'})")
        if is_premium:
            # [dev] force_manual_install = true → exercise the manual
            # browser-download flow even on a premium account (same switch
            # the browser / Change Version tabs honour).
            from Utils.ui_config import load_force_manual_install
            if load_force_manual_install():
                self._append_log("[reinstall] [dev] force_manual_install — "
                                 "using the manual browser-download flow.")
                is_premium = False

        if not is_premium:
            # Non-premium: same manual browser-download flow as the Nexus browser
            # and Change Version tabs — if the archive is already on disk install
            # it straight away, otherwise open the file's download page and watch
            # the download folders so it auto-installs when the browser download
            # lands (nxm:// 'Download with Mod Manager' also works and cancels
            # the watch to avoid a double install).
            self._start_reinstall_manual(items)
            return

        self._append_log(
            f"[reinstall] redownloading {len(items)} missing archive(s)…")
        self._notify(
            self.tr("Reinstall — redownloading {0} mod(s)…").format(len(items)),
            "info")

        from Utils.ui_config import load_collection_settings
        try:
            dl_workers = max(1, int(load_collection_settings().get("max_concurrent", 8)))
        except Exception:
            dl_workers = 8


        # One shared progress card for the whole batch (aggregate bytes).
        self._reinstall_dl_phase = self.tr(
            "Redownloading {0} mod(s)…").format(len(items))
        progress = {}                     # mod_name → [cur_bytes, total_bytes]
        for nm, *_ in items:
            progress[nm] = [0, 0]
        progress_lock = threading.Lock()
        last_emit = [0.0]

        def _post_aggregate(force=False):
            with progress_lock:
                now = _time.monotonic()
                if not force and now - last_emit[0] < 0.1:
                    return
                last_emit[0] = now
                cur = sum(c for c, _t in progress.values())
                tot = sum(t for _c, t in progress.values())
            self._reinstall_dl_progress.emit(cur, tot)

        _post_aggregate(force=True)

        def _download_all():
            from Nexus.nexus_download import NexusDownloader
            from Nexus.nexus_meta import build_meta_from_download
            from Utils.config_paths import get_download_cache_dir_for_game
            dest = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
            downloader = NexusDownloader(api, download_dir=dest)
            dl_items = []          # (mod_name, archive_path, prebuilt_meta)
            failed = []            # (mod_name, reason)
            lock = threading.Lock()

            def _one(item):
                mod_name, domain, mod_id, file_id, filename = item
                try:
                    def _on_progress(cur, tot, _m=mod_name):
                        with progress_lock:
                            slot = progress[_m]
                            slot[0] = int(cur)
                            if tot:
                                slot[1] = int(tot)
                        _post_aggregate()

                    result = downloader.download_file(
                        game_domain=domain, mod_id=mod_id, file_id=file_id,
                        dest_dir=dest, known_file_name=filename,
                        progress_cb=_on_progress)
                    if not (result.success and result.file_path):
                        with lock:
                            failed.append((mod_name,
                                           f"download failed — {result.error}"))
                        return
                    with progress_lock:
                        slot = progress[mod_name]
                        slot[1] = slot[1] or slot[0]
                        slot[0] = slot[1]
                    _post_aggregate(force=True)
                    prebuilt = None
                    try:
                        mod_info = api.get_mod(domain, mod_id)
                    except Exception:
                        mod_info = None
                    try:
                        prebuilt = build_meta_from_download(
                            game_domain=domain, mod_id=mod_id, file_id=file_id,
                            archive_name=result.file_name, mod_info=mod_info)
                    except Exception as exc:
                        self._op_log.emit(
                            f"[reinstall] Warning — could not build metadata: {exc}")
                    with lock:
                        dl_items.append((mod_name, str(result.file_path), prebuilt))
                except Exception as exc:
                    with lock:
                        failed.append((mod_name, f"download error ({exc})"))

            with _cf.ThreadPoolExecutor(max_workers=dl_workers) as pool:
                list(pool.map(_one, items))
            self._reinstall_downloaded.emit(dl_items, failed)

        threading.Thread(target=_download_all, daemon=True,
                         name="reinstall-dl").start()

    # ---- non-premium reinstall: browser download + folder watch ------------
    def _start_reinstall_manual(self, items):
        """Reinstall missing archives for free users via the shared manual
        browser-download flow (parity with the Nexus browser / Change Version
        tabs). Fetches each file's REAL metadata first (name + size — the folder
        watcher matches archives by size ±1% + mod id, so a zero-size stub only
        matches on an exact name, which browser downloads rarely keep), then per
        mod: install immediately if the archive is already on disk, else open its
        download page and arm a folder watcher that auto-installs when the
        browser download arrives. `items` =
        [(mod_name, domain, mod_id, file_id, filename), …]."""
        from gui_qt.safe_emit import safe_emit

        api = getattr(self, "_nexus_api", None)

        class _F:            # NexusModFile-like fallback if the fetch fails
            def __init__(self, file_id, file_name):
                self.file_id = file_id
                self.file_name = file_name
                self.name = file_name
                self.size_in_bytes = 0
                self.size_kb = 0

        def _prep():
            # One file-list fetch per mod (GraphQL modFiles — no REST cost),
            # shared when several items point at the same mod.
            files_cache: dict = {}
            enriched = []
            for nm, domain, mod_id, file_id, filename in items:
                f = None
                key = (domain, int(mod_id or 0))
                if api is not None:
                    try:
                        files = files_cache.get(key)
                        if files is None:
                            files = list(api.get_mod_files(domain, mod_id).files)
                            files_cache[key] = files
                        f = next((x for x in files
                                  if int(getattr(x, "file_id", 0) or 0)
                                  == int(file_id or 0)), None)
                    except Exception as exc:
                        files_cache.setdefault(key, [])
                        self._op_log.emit(
                            f"[reinstall] {nm} — could not fetch file info "
                            f"({exc}); archive detection may be less reliable.")
                if f is None:
                    f = _F(int(file_id or 0), filename or "")
                enriched.append((nm, domain, mod_id, file_id, f))
            safe_emit(self._reinstall_manual_ready, enriched)

        threading.Thread(target=_prep, daemon=True,
                         name="reinstall-manual-prep").start()

    def _on_reinstall_manual_ready(self, enriched):
        """UI thread: file metadata resolved — run each mod through the shared
        start_manual_install flow (skip the browser when the archive is already
        downloaded, else open its download page + watch the download folders).
        `enriched` = [(mod_name, domain, mod_id, file_id, NexusModFile-like), …]."""
        from gui_qt.safe_emit import safe_emit
        from Nexus.manual_download_watch import start_manual_install
        from Utils.xdg import open_url

        api = getattr(self, "_nexus_api", None)
        opened = 0
        for nm, domain, mod_id, file_id, f in enriched:
            dl_key = self._new_dl_key()
            self._nexus_download_progress(dl_key, nm, 0, 0)  # show popup card

            # Helper callbacks run on the WATCHER thread — marshal via Signals.
            # (_op_log / _append_log are thread-safe.)
            def on_archive(path, meta, _file, _nm=nm, _mid=mod_id, _key=dl_key):
                if not self._claim_app_manual_watch(_mid, _key):
                    return
                safe_emit(self._reinstall_manual_found,
                          _nm, str(path), meta, _key)

            def on_progress(done, total, _nm=nm, _key=dl_key):
                safe_emit(self._req_install_prog, _key, _nm, int(done), int(total))

            def on_timeout(_nm=nm, _mid=mod_id, _key=dl_key):
                if not self._claim_app_manual_watch(_mid, _key):
                    return
                self._op_log.emit(
                    f"[reinstall] {_nm} — stopped waiting for a browser "
                    "download (nothing arrived; reinstall from Downloads once "
                    "downloaded).")
                # total<0 clears the card (on the UI thread via the Signal).
                safe_emit(self._req_install_prog, _key, "", 0, -1)

            self.cancel_app_manual_watch(mod_id)   # re-trigger → fresh watch
            watcher, already = start_manual_install(
                api=api, game_domain=domain, mod_id=mod_id, files=[f],
                open_url_fn=lambda u: open_url(u, log_fn=self._append_log),
                log_fn=lambda m, _nm=nm: self._append_log(f"[reinstall] {_nm} — {m}"),
                log_label=nm,
                on_archive=on_archive, on_progress=on_progress,
                on_timeout=on_timeout,
                cache_dir=get_download_cache_dir_for_game(
                    getattr(self._gs.game, "name", "") or ""))
            self._app_manual_watchers[int(mod_id or 0)] = (watcher, dl_key)
            if not already:
                opened += 1

        if opened:
            self._notify(
                self.tr("Premium required to redownload. Opened {0} download "
                "page(s) — they'll reinstall automatically once downloaded.")
                .format(opened), "info")

    def cancel_app_manual_watch(self, mod_id: int):
        """Stop a pending app-level browser-download watch (reinstall or
        missing-requirements install; no-op if none). Called on re-trigger,
        and by the nxm:// handler when a 'Download with Mod Manager' for the
        same mod arrives (that flow installs it, so the watch must not —
        double install)."""
        t = self._app_manual_watchers.pop(int(mod_id or 0), None)
        if t is not None:
            watcher, dl_key = t
            watcher.stop()
            self._nexus_download_progress(dl_key, "", 0, -1)
            self._req_batch_watch_ended(dl_key)

    def _claim_app_manual_watch(self, mod_id, dl_key) -> bool:
        """Claim completion of an app-level manual watch (watcher thread).
        False when the watch was already cancelled or replaced by a newer one
        for the same mod — the stale watcher must not emit its completion
        signals (double install / clobbering the new watch's progress card)."""
        mid = int(mod_id or 0)
        t = self._app_manual_watchers.pop(mid, None)
        if t is None:
            return False
        if t[1] != dl_key:
            self._app_manual_watchers[mid] = t   # a newer watch owns the slot
            return False
        return True

    def _on_reinstall_manual_found(self, nm, archive, meta, dl_key):
        """UI thread: a non-premium reinstall's browser download landed (or an
        existing archive was found). Install with the folder name forced (silent
        Replace-All), keeping the archive for future reinstalls. *meta* was built
        on the watcher thread."""
        self._nexus_download_progress(dl_key, "", 0, -1)   # clear the card
        if not archive:
            return
        self._append_log(f"[reinstall] {nm} — reinstalling {archive}…")
        metas = {archive: meta} if meta is not None else None
        # clear_archives=False: keep the archive so it can be reinstalled again.
        self._install_paths([archive], metas=metas,
                            preferred_names={archive: nm}, clear_archives=False)

    def _on_reinstall_dl_progress(self, cur: int, tot: int):
        """UI thread: drive the shared reinstall redownload progress card."""
        self._ensure_feedback()
        if self._progress_popup is None:
            return
        self._progress_popup.set_progress(
            cur, tot, getattr(self, "_reinstall_dl_phase", None),
            title=self.tr("Reinstall"), bytes_mode=True, key="reinstall-dl")

    def _on_reinstall_downloaded(self, dl_items, failed):
        """UI thread: redownloads finished. Install the batch via _install_paths
        with the folder name forced per archive (silent Replace-All)."""
        if self._progress_popup is not None:
            self._progress_popup.clear(key="reinstall-dl")
        for name, reason in failed:
            self._append_log(f"[reinstall] {name}: {reason}")
        if not dl_items:
            if failed:
                self._notify(
                    self.tr("Reinstall: {0} mod(s) couldn't be redownloaded — "
                    "see the log.").format(len(failed)), "warning")
            return
        paths = [p for _n, p, _m in dl_items]
        metas = {p: m for _n, p, m in dl_items if m is not None}
        preferred = {p: n for n, p, _m in dl_items}
        if failed:
            self._notify(
                self.tr("Redownloaded {0} mod(s); {1} failed — see the log.").format(
                    len(dl_items), len(failed)), "warning")
        # clear_archives=False: keep the freshly downloaded archive so the mod
        # can be reinstalled again without another download.
        self._install_paths(paths, metas=metas, preferred_names=preferred,
                            clear_archives=False)

    def _quick_update_mods(self, mod_names):
        """Auto-install the latest name-matched version for each update-flagged
        mod (Tk parity, gui/modlist_nexus_actions._quick_update_mods). Mods whose
        latest file isn't a name match are skipped — the user updates those via
        Change Version. Three phases, all off the UI thread: resolve the target
        file_ids (parallel) → download them (parallel, premium-gated) → install
        the whole batch via _install_paths with the folder name forced (silent
        Replace-All)."""
        if getattr(self, "_quick_updating", False):
            self._notify(self.tr("A Quick Update is already running."), "info")
            return
        if getattr(self, "_install_running", False):
            self._notify(self.tr("An install is already in progress."), "warning")
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
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        targets = list(mod_names or [])
        if not targets:
            self._notify(self.tr("No mods with a pending update to quick-update."), "info")
            return

        self._quick_updating = True
        domain = getattr(game, "nexus_game_domain", "") or ""
        self._notify(self.tr("Quick Update — checking {0} mod(s)…").format(len(targets)), "info")
        self._append_log(f"[nexus] Quick Update — checking {len(targets)} mod(s)…")


        def _resolve_worker():
            from Utils.exe_launch.quick_update import resolve_quick_update_target
            queue = []
            skipped = []   # (mod_name, reason)

            def _one(nm):
                return resolve_quick_update_target(api, staging, nm, domain)

            with _cf.ThreadPoolExecutor(max_workers=4) as pool:
                for nm, (status, payload) in zip(targets, pool.map(_one, targets)):
                    if status == "queued":
                        queue.append(payload)
                    else:
                        skipped.append((nm, payload))
                        self._op_log.emit(f"[nexus] {nm} — {payload}, skipped.")
            self._qu_resolved.emit(queue, skipped)

        threading.Thread(target=_resolve_worker, daemon=True,
                         name="quick-update-resolve").start()

    def _on_qu_resolved(self, queue, skipped):
        """UI thread: resolve finished. Nothing to update → summarise. Otherwise
        gate on premium and download every resolved file in parallel."""
        if not queue:
            self._qu_finish(0, [], skipped)
            return
        api = self._ensure_nexus_api()
        game = self._gs.game
        # validate() is rate-limited, and _ensure_nexus_api() above may have
        # just fired its own validate() call seconds ago (it always does one
        # off-thread when building the api) — a second call here can easily
        # get rate-limited itself. A bare exception must not silently demote
        # a premium user to "not premium": fall back to the last
        # successfully-validated status instead, same fix already applied to
        # the collection-install premium gate for this exact failure mode
        # (GH#278) — Quick Update just never got it, so every mod silently
        # skipped with a misleading "Premium required" even for a genuinely
        # premium account whenever this collided with the api-build validate().
        from Utils.ui_config import load_nexus_last_premium, save_nexus_last_premium
        try:
            is_premium = bool(api.validate().is_premium)
            try:
                save_nexus_last_premium(is_premium)
            except Exception:
                pass
        except Exception as exc:
            is_premium = bool(load_nexus_last_premium())
            self._append_log(
                f"[nexus] Quick Update premium check failed: {exc} — using "
                f"last-known status ({'premium' if is_premium else 'not premium'})")
        if not is_premium:
            self._append_log("[nexus] Premium required for Quick Update direct "
                             "downloads.")
            self._qu_finish(
                0, [(it[0], "Premium required for direct download") for it in queue],
                skipped)
            return

        self._append_log(f"[nexus] Quick Update — downloading {len(queue)} mod(s)…")
        self._notify(self.tr("Quick Update — downloading {0} mod(s)…").format(len(queue)), "info")

        from Utils.ui_config import load_collection_settings
        try:
            dl_workers = max(1, int(load_collection_settings().get("max_concurrent", 8)))
        except Exception:
            dl_workers = 8


        # One shared progress card for the whole batch (Tk parity: a per-download
        # popup per mod stacks up fast). Progress is the aggregate byte count
        # across all parallel downloads; totals are seeded from the Nexus file
        # sizes and corrected by each download's own reported total.
        self._qu_dl_phase = self.tr("Downloading {0} mod(s)…").format(len(queue))
        progress = {}                     # mod_name → [cur_bytes, total_bytes]
        for item in queue:
            fi = item[4]
            size = 0
            if fi is not None:
                size = (getattr(fi, "size_in_bytes", None)
                        or (getattr(fi, "size_kb", 0) or 0) * 1024 or 0)
            progress[item[0]] = [0, int(size)]
        progress_lock = threading.Lock()
        last_emit = [0.0]

        def _post_aggregate(force=False):
            with progress_lock:
                now = _time.monotonic()
                if not force and now - last_emit[0] < 0.1:
                    return
                last_emit[0] = now
                cur = sum(c for c, _t in progress.values())
                tot = sum(t for _c, t in progress.values())
            self._qu_dl_progress.emit(cur, tot)

        _post_aggregate(force=True)       # show the card before downloads start

        def _download_all():
            from Nexus.nexus_download import NexusDownloader
            from Nexus.nexus_meta import build_meta_from_download
            from Utils.config_paths import get_download_cache_dir_for_game
            dest = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
            downloader = NexusDownloader(api, download_dir=dest)
            dl_items = []          # (mod_name, archive_path, prebuilt_meta)
            failed = []            # (mod_name, reason)
            lock = threading.Lock()

            def _one(item):
                mod_name, game_domain, meta, file_id, file_info = item
                try:
                    try:
                        mod_info = api.get_mod(game_domain, meta.mod_id)
                    except Exception as exc:
                        with lock:
                            failed.append((mod_name, f"could not fetch mod info ({exc})"))
                        return
                    size = 0
                    if file_info is not None:
                        size = (getattr(file_info, "size_in_bytes", None)
                                or (getattr(file_info, "size_kb", 0) or 0) * 1024 or 0)

                    def _on_progress(cur, tot, _m=mod_name):
                        with progress_lock:
                            slot = progress[_m]
                            slot[0] = int(cur)
                            if tot:
                                slot[1] = int(tot)
                        _post_aggregate()

                    result = downloader.download_file(
                        game_domain=game_domain, mod_id=meta.mod_id,
                        file_id=file_id, dest_dir=dest,
                        known_file_name=getattr(file_info, "file_name", "") or "",
                        expected_size_bytes=int(size),
                        progress_cb=_on_progress)
                    if not (result.success and result.file_path):
                        with lock:
                            failed.append((mod_name,
                                           f"download failed — {result.error}"))
                        return
                    # Snap this mod's slice to done (its total may have been an
                    # estimate) so the shared bar never sits below the truth.
                    with progress_lock:
                        slot = progress[mod_name]
                        slot[1] = slot[1] or slot[0]
                        slot[0] = slot[1]
                    _post_aggregate(force=True)
                    try:
                        prebuilt = build_meta_from_download(
                            game_domain=game_domain, mod_id=meta.mod_id,
                            file_id=file_id, archive_name=result.file_name,
                            mod_info=mod_info, file_info=file_info)
                        prebuilt.has_update = False
                    except Exception as exc:
                        self._op_log.emit(
                            f"[nexus] Warning — could not build metadata: {exc}")
                        prebuilt = None
                    with lock:
                        dl_items.append((mod_name, str(result.file_path), prebuilt))
                except Exception as exc:
                    with lock:
                        failed.append((mod_name, f"download error ({exc})"))

            with _cf.ThreadPoolExecutor(max_workers=dl_workers) as pool:
                list(pool.map(_one, queue))
            self._qu_skipped = skipped   # carried to _qu_finish via the installer
            self._qu_downloaded.emit(dl_items, failed)

        threading.Thread(target=_download_all, daemon=True,
                         name="quick-update-dl").start()

    def _on_qu_dl_progress(self, cur: int, tot: int):
        """UI thread: drive the shared Quick Update download card (aggregate
        bytes across every parallel download in the batch)."""
        self._ensure_feedback()
        if self._progress_popup is None:
            return
        self._progress_popup.set_progress(
            cur, tot, getattr(self, "_qu_dl_phase", None),
            title=self.tr("Quick Update"), bytes_mode=True, key="qu-dl")

    def _on_qu_downloaded(self, dl_items, failed):
        """UI thread: every download finished. Install the batch via _install_paths
        with the folder name forced per archive (silent Replace-All), then finish."""
        if self._progress_popup is not None:
            self._progress_popup.clear(key="qu-dl")
        skipped = getattr(self, "_qu_skipped", [])
        if not dl_items:
            self._qu_finish(0, failed, skipped)
            return
        paths = [p for _n, p, _m in dl_items]
        metas = {p: m for _n, p, m in dl_items if m is not None}
        preferred = {p: n for n, p, _m in dl_items}
        expected = len(dl_items)

        def _done(ok, total, names):
            # ok/total here are the archive install results; combine with the
            # download failures + resolve skips for the batch summary.
            more_failed = list(failed)
            if ok < expected:
                more_failed.append(
                    (f"{expected - ok} mod(s)", "install failed — see log"))
            self._qu_finish(ok, more_failed, skipped)

        self._install_paths(paths, metas=metas, preferred_names=preferred,
                            on_all_done=_done)

    def _qu_finish(self, updated, failed, skipped):
        """Log the batch summary + toast; warn about mods that couldn't be
        quick-updated. Re-checks flags happen via _install_paths' _reload_modlist;
        the resolve/download-only paths (nothing installed) reload here."""
        self._quick_updating = False
        self._qu_skipped = []
        if updated == 0:
            # No install ran → refresh flags ourselves (installed path already did).
            self._reload_modlist()
        self._append_log(
            f"[nexus] Quick Update — {updated} updated, "
            f"{len(skipped)} skipped (no name match), {len(failed)} failed.")
        for name, reason in failed:
            self._append_log(f"[nexus] Quick Update — {name}: {reason}")
        if updated:
            self._notify(self.tr("Quick Update: updated {0} mod(s)").format(updated), "success")
        problems = len(skipped) + len(failed)
        if not problems:
            if not updated:
                self._notify(self.tr("Quick Update: nothing to update."), "info")
            return
        parts = []
        if skipped:
            parts.append(f"{len(skipped)} had no name-matched file")
        if failed:
            parts.append(f"{len(failed)} failed to download or install")
        self._notify(
            f"Quick Update: {problems} mod(s) couldn't be updated — "
            + " and ".join(parts) + ". See the log; use Change Version manually.",
            "warning")

    def _quick_update_modio_mods(self, mod_names):
        """Auto-install the latest file for each mod.io-update-flagged mod.
        Two phases, both off the UI thread: resolve each mod's download URL
        (parallel) → download them (parallel) → install the whole batch via
        _install_paths with the folder name forced (silent Replace-All)."""
        if getattr(self, "_quick_updating_modio", False):
            self._notify(self.tr("A mod.io Quick Update is already running."), "info")
            return
        if getattr(self, "_install_running", False):
            self._notify(self.tr("An install is already in progress."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        api_key = _load_bg3_modio("modio_key").load_modio_key()
        if not api_key:
            self._notify(
                self.tr("No mod.io API key configured — Wizard ▸ mod.io API Key."),
                "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        targets = list(mod_names or [])
        if not targets:
            self._notify(
                self.tr("No mods with a pending mod.io update to quick-update."), "info")
            return

        self._quick_updating_modio = True
        self._qmu_api_key = api_key
        self._notify(
            self.tr("Quick Update (mod.io) — checking {0} mod(s)…").format(len(targets)),
            "info")
        self._append_log(f"[modio] Quick Update — checking {len(targets)} mod(s)…")


        def _resolve_worker():
            modio_api = _load_bg3_modio("modio_api")
            modio_qu = _load_bg3_modio("modio_quick_update")
            try:
                api = modio_api.ModioAPI(api_key)
            except Exception as e:
                self._op_log.emit(f"[modio] cannot init API — {e}")
                self._qmu_resolved.emit(
                    [], [(nm, "mod.io API init failed") for nm in targets])
                return
            queue = []
            skipped = []   # (mod_name, reason)

            def _one(nm):
                return modio_qu.resolve_modio_quick_update_target(api, staging, nm)

            with _cf.ThreadPoolExecutor(max_workers=4) as pool:
                for nm, (status, payload) in zip(targets, pool.map(_one, targets)):
                    if status == "queued":
                        queue.append(payload)
                    else:
                        skipped.append((nm, payload))
                        self._op_log.emit(f"[modio] {nm} — {payload}, skipped.")
            self._qmu_resolved.emit(queue, skipped)

        threading.Thread(target=_resolve_worker, daemon=True,
                         name="quick-update-modio-resolve").start()

    def _on_qmu_resolved(self, queue, skipped):
        """UI thread: resolve finished. Nothing to update → summarise. Otherwise
        download every resolved file in parallel (no premium gate — mod.io
        files are public, unlike Nexus direct downloads)."""
        if not queue:
            self._qmu_finish(0, [], skipped)
            return
        self._append_log(f"[modio] Quick Update — downloading {len(queue)} mod(s)…")
        self._notify(
            self.tr("Quick Update (mod.io) — downloading {0} mod(s)…").format(len(queue)),
            "info")

        from Utils.ui_config import load_collection_settings
        try:
            dl_workers = max(1, int(load_collection_settings().get("max_concurrent", 8)))
        except Exception:
            dl_workers = 8


        self._qmu_dl_phase = self.tr("Downloading {0} mod(s)…").format(len(queue))
        progress = {}                     # mod_name → [cur_bytes, total_bytes]
        for mod_name, _meta, file in queue:
            progress[mod_name] = [0, int(getattr(file, "filesize", 0) or 0)]
        progress_lock = threading.Lock()
        last_emit = [0.0]

        def _post_aggregate(force=False):
            with progress_lock:
                now = _time.monotonic()
                if not force and now - last_emit[0] < 0.1:
                    return
                last_emit[0] = now
                cur = sum(c for c, _t in progress.values())
                tot = sum(t for _c, t in progress.values())
            self._qmu_dl_progress.emit(cur, tot)

        _post_aggregate(force=True)       # show the card before downloads start

        def _download_all():
            modio_api = _load_bg3_modio("modio_api")
            from Utils.config_paths import get_download_cache_dir_for_game
            dest = get_download_cache_dir_for_game(getattr(self._gs.game, "name", "") or "")
            try:
                api = modio_api.ModioAPI(getattr(self, "_qmu_api_key", ""))
            except Exception as e:
                self._qmu_downloaded.emit(
                    [], [(nm, f"mod.io API init failed ({e})") for nm, _m, _f in queue])
                return
            dl_items = []          # (mod_name, archive_path, meta, file)
            failed = []            # (mod_name, reason)
            lock = threading.Lock()

            def _one(item):
                mod_name, meta, file = item
                try:
                    def _on_progress(cur, tot, _m=mod_name):
                        with progress_lock:
                            slot = progress[_m]
                            slot[0] = int(cur)
                            if tot:
                                slot[1] = int(tot)
                        _post_aggregate()

                    path = api.download_file(file, dest, progress_cb=_on_progress)
                    # Snap this mod's slice to done (its total may have been an
                    # estimate) so the shared bar never sits below the truth.
                    with progress_lock:
                        slot = progress[mod_name]
                        slot[1] = slot[1] or slot[0]
                        slot[0] = slot[1]
                    _post_aggregate(force=True)
                    with lock:
                        dl_items.append((mod_name, str(path), meta, file))
                except Exception as exc:
                    with lock:
                        failed.append((mod_name, f"download error ({exc})"))

            with _cf.ThreadPoolExecutor(max_workers=dl_workers) as pool:
                list(pool.map(_one, queue))
            self._qmu_skipped = skipped   # carried to _qmu_finish via the installer
            self._qmu_downloaded.emit(dl_items, failed)

        threading.Thread(target=_download_all, daemon=True,
                         name="quick-update-modio-dl").start()

    def _on_qmu_dl_progress(self, cur: int, tot: int):
        """UI thread: drive the shared Quick Update (mod.io) download card
        (aggregate bytes across every parallel download in the batch)."""
        self._ensure_feedback()
        if self._progress_popup is None:
            return
        self._progress_popup.set_progress(
            cur, tot, getattr(self, "_qmu_dl_phase", None),
            title=self.tr("Quick Update (mod.io)"), bytes_mode=True, key="qmu-dl")

    def _on_qmu_downloaded(self, dl_items, failed):
        """UI thread: every download finished. Install the batch via
        _install_paths with the folder name forced per archive (silent
        Replace-All). No prebuilt meta is passed through _install_paths itself
        — the shared install pipeline's own mod.io identification
        (_try_resolve_modio in Utils.mods.mod_install) always runs and writes
        SOME mod.io meta first, using its md5/uncompressed-size heuristics to
        re-derive the file from the archive. Those heuristics can mismatch
        (two releases sharing an uncompressed pak size, an md5 miss) and land
        on the wrong historical file — so once install finishes we overwrite
        meta.ini ourselves with the file we *know* we fetched (_stamp_modio_meta),
        via a second _reload_modlist so the corrected flags actually show."""
        if self._progress_popup is not None:
            self._progress_popup.clear(key="qmu-dl")
        skipped = getattr(self, "_qmu_skipped", [])
        if not dl_items:
            self._qmu_finish(0, failed, skipped)
            return
        paths = [p for _n, p, _m, _f in dl_items]
        preferred = {p: n for n, p, _m, _f in dl_items}
        by_name = {n: (m, f) for n, _p, m, f in dl_items}
        expected = len(dl_items)

        def _done(ok, total, names):
            more_failed = list(failed)
            if ok < expected:
                more_failed.append(
                    (f"{expected - ok} mod(s)", "install failed — see log"))
            staging = self._gs.staging_dir()
            stamped = False
            if staging is not None:
                for nm in (names or []):
                    entry = by_name.get(nm)
                    if entry is None:
                        continue
                    meta, file = entry
                    self._stamp_modio_meta(staging / nm / "meta.ini", meta, file)
                    stamped = True
            if stamped:
                # The generic install-done handler already reloaded the modlist
                # using the heuristic-guessed meta.ini we just overwrote —
                # reload again so the Flags column reflects the corrected data.
                self._reload_modlist()
            self._qmu_finish(ok, more_failed, skipped)

        self._install_paths(paths, preferred_names=preferred, on_all_done=_done)

    def _stamp_modio_meta(self, meta_path, meta, file):
        """Overwrite meta.ini's mod.io file_id/version with the file we KNOW
        we just fetched for a Quick Update, instead of trusting the install
        pipeline's own resolve_modio_meta re-identification (md5/uncompressed
        -size heuristics against the archive), which can mismatch when
        another release happens to share the same uncompressed pak size.

        Re-reads meta.ini fresh rather than reusing *meta* (resolved BEFORE
        the download started, by resolve_modio_quick_update_target) — the
        generic install pipeline's own resolve_modio_meta() call already ran
        moments ago, post-install, with a fresh mod.io query, and may have
        found a newer latest_file_id than existed when this update was
        resolved. Blindly setting latest_file_id=file.file_id here would
        stomp that fresher value back down to "no update," and previously
        also silently reset liked/ignore_update/ignored_version to their
        dataclass defaults since a freshly-constructed ModioMeta doesn't
        carry them — see modio_meta.py's _KEY_LIKED comment for why a caller
        must round-trip these instead of rebuilding from scratch. Only
        file_id/version/installed are facts this call is actually confident
        about; everything else comes from the current on-disk state.

        Never raises — a failure here just leaves the heuristic's guess in
        place, same as before this existed."""
        try:
            modio_meta = _load_bg3_modio("modio_meta")
            from datetime import datetime, timezone
            current = modio_meta.read_modio_meta(meta_path)
            new_meta = modio_meta.ModioMeta(
                mod_id=meta.mod_id,
                file_id=file.file_id,
                version=file.version,
                name=current.name or meta.name,
                profile_url=current.profile_url or meta.profile_url,
                uploader=current.uploader or meta.uploader,
                tags=current.tags or meta.tags,
                latest_file_id=current.latest_file_id or file.file_id,
                latest_version=current.latest_version or file.version,
                installed=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                ignore_update=current.ignore_update,
                ignored_version=current.ignored_version,
                liked=current.liked,
            )
            modio_meta.write_modio_meta(meta_path, new_meta)
        except Exception as e:
            self._append_log(
                f"[modio] could not stamp confirmed file id for "
                f"'{meta_path.parent.name}': {e}")

    def _qmu_finish(self, updated, failed, skipped):
        """Log the batch summary + toast (mod.io Quick Update). Re-checks flags
        happen via _install_paths' _reload_modlist; the resolve-only path
        (nothing installed) reloads here."""
        self._quick_updating_modio = False
        self._qmu_skipped = []
        if updated == 0:
            self._reload_modlist()
        self._append_log(
            f"[modio] Quick Update — {updated} updated, "
            f"{len(skipped)} skipped, {len(failed)} failed.")
        for name, reason in skipped:
            self._append_log(f"[modio] Quick Update — {name}: {reason}")
        for name, reason in failed:
            self._append_log(f"[modio] Quick Update — {name}: {reason}")
        if updated:
            self._notify(
                self.tr("Quick Update (mod.io): updated {0} mod(s)").format(updated),
                "success")
        problems = len(skipped) + len(failed)
        if not problems:
            if not updated:
                self._notify(self.tr("Quick Update (mod.io): nothing to update."), "info")
            return
        self._notify(
            f"Quick Update (mod.io): {problems} mod(s) couldn't be updated — "
            "see the log.", "warning")

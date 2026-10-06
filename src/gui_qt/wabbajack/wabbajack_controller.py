"""UI flow for importing a Wabbajack modlist: pick a ``.wabbajack`` file,
show it with its preflight result, then install it into a new profile.

Kept out of ``gui_qt/app.py``: the main window only creates this controller
and calls :meth:`start_import`. Threading follows the Collection installer's
rule -- every callback the worker sees is a bare ``Signal.emit``; all widget
work happens in this object's slots on the UI thread. The two blocking
requests (manual download, LoversLab login) hand the worker a
``threading.Event`` that the UI sets once the user has answered.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from gui_qt.safe_emit import safe_emit


def install_summary(report, profile_name: str) -> "tuple[str, bool]":
    """``(text, ok)`` describing a finished install, for the overlay."""
    if report.cancelled:
        return (f"Install cancelled. The profile '{profile_name}' keeps what was "
                "installed so far; remove it from the profile menu if you don't "
                "want it."), False
    parts = [f"Installed {len(report.installed_mods)} mod"
             f"{'' if len(report.installed_mods) == 1 else 's'} into the profile "
             f"'{profile_name}'."]
    if report.failed_archives:
        parts.append(f"{len(report.failed_archives)} download(s) couldn't be completed.")
    if report.failed_directives:
        parts.append(f"{len(report.failed_directives)} file(s) couldn't be built.")
    if report.held_profile_files:
        parts.append(f"{len(report.held_profile_files)} profile settings file(s), such as "
                     "INIs, were saved in the profile's wabbajack_profile_files folder "
                     "but not applied.")
    if report.unplaced_files:
        groups = Counter(f.split("/", 1)[0] if "/" in f else "(top level)"
                         for f in report.unplaced_files)
        top = ", ".join(f"{name} ({n})" for name, n in groups.most_common(3))
        more = f" and {len(groups) - 3} more" if len(groups) > 3 else ""
        parts.append(f"Skipped {len(report.unplaced_files)} file(s) Mosaic doesn't use, such "
                     f"as Mod Organizer 2's own program files: {top}{more}.")
    if not report.ok:
        parts.append("See the log for the details.")
    return " ".join(parts), report.ok


def _profile_base_name(modlist_name: str) -> str:
    base = re.sub(r"[^\w\s\-]", "", modlist_name or "").strip().replace(" ", "_")
    return (base or "Wabbajack")[:64]


class WabbajackController(QObject):
    _picked = Signal(object)                 # Path | None from the portal picker
    _parsed = Signal(object, object, object)  # path, ModList | None, error | None
    _status = Signal(str)
    _log = Signal(str)
    _dl = Signal(str, object)                # "start"|"progress"|"finish", payload
    _build = Signal(int, int)
    _finished = Signal(object, object)       # report | None, error | None
    _ask_manual = Signal(object)             # {"archive", "reason", "holder", "event"}
    _ask_login = Signal(object)              # {"holder", "event"}

    def __init__(self, window):
        super().__init__(window)
        self._win = window
        self._view = None
        self._overlay = None
        self._control = None
        self._running = False
        self._picked.connect(self._on_picked)
        self._parsed.connect(self._on_parsed)
        self._status.connect(lambda t: self._overlay and self._overlay.set_status(t))
        self._log.connect(lambda t: self._win._append_log(t))
        self._dl.connect(self._on_dl)
        self._build.connect(lambda d, t: self._overlay and self._overlay.build_progress(d, t))
        self._finished.connect(self._on_finished)
        self._ask_manual.connect(self._on_ask_manual)
        self._ask_login.connect(self._on_ask_login)

    def tr(self, text):
        return self._win.tr(text)

    # -- pick + parse -----------------------------------------------------------
    def start_import(self) -> None:
        game = self._win._gs.game
        if game is None or not game.is_configured():
            self._win._notify(self.tr("No configured game selected."), "warning")
            return
        if self._running:
            self._win._notify(self.tr("A Wabbajack install is already running."), "warning")
            return
        from Utils.wine_proton.portal_filechooser import pick_file
        pick_file(self.tr("Import Wabbajack modlist"),
                  lambda p: safe_emit(self._picked, p),
                  filters=[("Wabbajack modlist (*.wabbajack)", ["*.wabbajack"]),
                           ("All files", ["*"])])

    def _on_picked(self, picked) -> None:
        if not picked:
            return
        path = Path(picked)
        self._win._notify(self.tr("Reading {0}…").format(path.name), "info")

        def _worker():
            from Utils.wabbajack.wabbajack_file import WabbajackFileError, read_modlist
            try:
                safe_emit(self._parsed, path, read_modlist(path), None)
            except WabbajackFileError as exc:
                safe_emit(self._parsed, path, None, str(exc))
            except Exception as exc:
                safe_emit(self._parsed, path, None, f"Couldn't read {path.name}: {exc}")

        threading.Thread(target=_worker, daemon=True, name="wabbajack-parse").start()

    def _preflight(self, modlist):
        from Utils.config_paths import get_download_cache_dir_for_game
        from Utils.ui_config import load_nexus_last_premium
        from Utils.wabbajack.downloaders import loverslab_auth
        from Utils.wabbajack.wabbajack_preflight import run_preflight
        game = self._win._gs.game
        nexus_ok = self._win._ensure_nexus_api() is not None and bool(load_nexus_last_premium())
        try:
            game_root = game.get_game_path()
        except Exception:
            game_root = None
        return run_preflight(
            modlist, active_game_name=game.name,
            staging_root=game.get_effective_mod_staging_path(),
            cache_dir=get_download_cache_dir_for_game(game.name),
            nexus_premium=nexus_ok, game_root=game_root,
            loverslab_logged_in=bool(loverslab_auth.load_session()))

    def _on_parsed(self, path, modlist, error) -> None:
        if error is not None:
            self._win._notify(error, "error")
            return
        if self._win._gs.game is None:
            return
        from gui_qt.wabbajack.wabbajack_import_view import WabbajackImportView
        key = "wabbajack_import"
        if self._win._tabs.has_key(key):
            self._win._tabs.close_tab(key)
        self._path, self._modlist = path, modlist
        from Utils.wabbajack.wabbajack_install import profile_choices
        self._view = WabbajackImportView(
            modlist, self._preflight(modlist), profiles=profile_choices(modlist),
            on_install=self._start_install, on_login=self._login_from_view)
        self._win._tabs.open_tab(
            self._view, self.tr("Wabbajack: {0}").format(modlist.name or path.stem), key=key)

    def _login_from_view(self) -> None:
        from gui_qt.wabbajack.loverslab_login_overlay import LoversLabLoginOverlay

        def done(ok):
            if ok and self._view is not None:
                self._view.set_checks(self._preflight(self._modlist))

        LoversLabLoginOverlay.show_over(self._win, done)

    # -- install ----------------------------------------------------------------
    def _start_install(self, profile=None) -> None:
        from Utils.exe_launch.game_helpers import _create_profile, _profiles_for_game
        from Utils.wabbajack.wabbajack_install import (
            WabbajackInstallCallbacks,
            WabbajackInstallControl,
        )
        from gui_qt.wabbajack.wabbajack_install_overlay import WabbajackInstallOverlay
        if self._running:
            return
        game = self._win._gs.game
        modlist = self._modlist
        base = _profile_base_name(modlist.name)
        existing = set(_profiles_for_game(game.name))
        name, n = base, 2
        while name in existing:
            name, n = f"{base}_{n}"[:64], n + 1
        try:
            profile_dir = Path(_create_profile(game.name, name, profile_specific_mods=True))
        except Exception as exc:
            self._win._notify(self.tr("Could not create profile: {0}").format(exc), "error")
            return

        self._running = True
        self._mo2_profile = profile
        self._profile_name = name
        if self._view is not None:
            self._view.set_installing(True)
        self._control = WabbajackInstallControl()
        self._overlay = WabbajackInstallOverlay.show_over(
            self._win, self.tr("Installing {0}").format(modlist.name or name),
            len(modlist.archives), modlist.total_archive_size,
            on_cancel=self._control.cancel.set, on_close=self._on_overlay_closed)
        callbacks = WabbajackInstallCallbacks(
            on_status=lambda t: safe_emit(self._status, str(t)),
            on_log=lambda t: safe_emit(self._log, str(t)),
            on_download_start=lambda h, nm, sz: safe_emit(self._dl, "start", (h, nm, sz)),
            on_download_progress=lambda h, c, t: safe_emit(self._dl, "progress", (h, c, t)),
            on_download_finish=lambda h, ok: safe_emit(self._dl, "finish", (h, ok)),
            on_build_progress=lambda d, t: safe_emit(self._build, int(d), int(t)),
            request_manual_download=self._blocking(self._ask_manual, ("archive", "reason")),
            request_loverslab_login=self._blocking(self._ask_login, ()),
        )
        api = self._win._ensure_nexus_api()
        threading.Thread(target=self._install_worker,
                         args=(game, api, profile_dir, callbacks, self._control),
                         daemon=True, name="wabbajack-install").start()

    def _install_worker(self, game, api, profile_dir, callbacks, control) -> None:
        from Nexus.nexus_download import NexusDownloader
        from Utils.config_paths import get_download_cache_dir_for_game
        from Utils.wabbajack.wabbajack_directives import path_substitutions
        from Utils.wabbajack.wabbajack_install import run_wabbajack_install
        from Utils.wine_proton.wine_paths import to_wine_path
        try:
            download_dir = get_download_cache_dir_for_game(game.name)
            # Resolve the new profile's staging first: the substitution table
            # must describe where this profile's files actually live.
            game.set_active_profile_dir(profile_dir)
            game.load_paths()
            game_path = game.get_game_path()
            substitutions = path_substitutions(
                game_path=to_wine_path(game_path) if game_path else None,
                install_path=to_wine_path(Path(game.get_effective_mod_staging_path()).parent),
                download_path=to_wine_path(download_dir))
            report = run_wabbajack_install(
                wabbajack_path=self._path, modlist=self._modlist, game=game,
                profile_dir=profile_dir, download_dir=download_dir,
                profile_name=self._mo2_profile,
                nexus_downloader=(NexusDownloader(api, download_dir=download_dir)
                                  if api is not None else None),
                substitutions=substitutions, callbacks=callbacks, control=control)
            safe_emit(self._finished, report, None)
        except Exception as exc:
            import traceback
            safe_emit(self._log, f"[wabbajack] install error: {exc}\n{traceback.format_exc()}")
            safe_emit(self._finished, None, str(exc))

    @staticmethod
    def _blocking(signal, arg_names):
        """A worker-side callback that asks the UI and waits for the answer."""
        def ask(*args):
            payload = dict(zip(arg_names, args))
            payload.update(holder={"result": None}, event=threading.Event())
            safe_emit(signal, payload)
            payload["event"].wait()
            return payload["holder"]["result"]
        return ask

    # -- UI-thread slots --------------------------------------------------------
    def _on_dl(self, kind, payload) -> None:
        if self._overlay is None:
            return
        getattr(self._overlay, {"start": "dl_start", "progress": "dl_progress",
                                "finish": "dl_finish"}[kind])(*payload)

    def _answer(self, payload, result) -> None:
        payload["holder"]["result"] = result
        payload["event"].set()

    def _on_ask_manual(self, payload) -> None:
        from gui_qt.wabbajack.manual_download_overlay import ManualDownloadOverlay
        if self._control is not None and self._control.cancel.is_set():
            self._answer(payload, None)
            return
        ManualDownloadOverlay.show_over(
            self._win, payload["archive"], payload["reason"],
            lambda path: self._answer(payload, path))

    def _on_ask_login(self, payload) -> None:
        from gui_qt.wabbajack.loverslab_login_overlay import LoversLabLoginOverlay
        LoversLabLoginOverlay.show_over(self._win, lambda ok: self._answer(payload, bool(ok)))

    def _on_finished(self, report, error) -> None:
        self._running = False
        if self._view is not None:
            self._view.set_installing(False)
        if error is not None:
            if self._overlay is not None:
                self._overlay.finish(self.tr("The install stopped with an error: {0}. "
                                             "See the log for details.").format(error), False)
            return
        for name, why in report.failed_archives:
            self._win._append_log(f"[wabbajack] download failed: {name}: {why}")
        for to, why in report.failed_directives:
            self._win._append_log(f"[wabbajack] couldn't build {to}: {why}")
        text, ok = install_summary(report, self._profile_name)
        if self._overlay is not None:
            self._overlay.finish(text, ok)
        if self._profile_name:
            self._win._select_installed_collection_profile(self._profile_name)

    def _on_overlay_closed(self) -> None:
        self._overlay = None

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
import shutil
import threading
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from gui_qt.safe_emit import safe_emit


def install_summary(report, profile_names) -> "tuple[str, bool]":
    """``(text, ok)`` describing a finished install, for the overlay.
    ``profile_names`` is the Mosaic profile(s) this run wrote."""
    names = [profile_names] if isinstance(profile_names, str) else list(profile_names)
    one = len(names) <= 1
    where = (f"the profile '{names[0]}'" if names else "the profile") if one \
        else f"{len(names)} profiles ({', '.join(names)})"
    if report.cancelled:
        kept, it = ((f"The profile '{names[0]}' keeps", "it") if one and names
                    else (f"The {len(names)} profiles keep", "them"))
        return (f"Install cancelled. {kept} what was installed so far; remove "
                f"{it} from the profile menu if you don't want {it}."), False
    parts = [f"Installed {len(report.installed_mods)} mod"
             f"{'' if len(report.installed_mods) == 1 else 's'} into {where}."]
    if report.failed_archives:
        parts.append(f"{len(report.failed_archives)} download(s) couldn't be completed.")
    if report.failed_directives:
        parts.append(f"{len(report.failed_directives)} file(s) couldn't be built.")
    if report.skipped_mods:
        parts.append(f"Skipped {len(report.skipped_mods)} mod(s) that only other "
                     "versions of the modlist use.")
    if report.root_builder_mods:
        parts.append(f"{len(report.root_builder_mods)} mod(s) use Root Builder and were "
                     "set up to install into the game folder: "
                     f"{', '.join(report.root_builder_mods)}.")
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


def _unique_profile_name(wanted: str, taken: "set[str]", game=None) -> str:
    """``wanted``, or ``wanted_2``, ``wanted_3``… until it is free.

    ``taken`` is matched case-insensitively, and for a game that derives a
    folder from the profile name (VtMB's ``-game Mosaic_<profile>``) the
    derived name has to be free too: two profiles that differ only in
    punctuation would otherwise deploy into the same folder.
    """
    def derived(n: str) -> str:
        fn = getattr(game, "get_mod_dir_name", None)
        try:
            return fn(n).lower() if fn is not None else ""
        except Exception:
            return ""

    lowered = {n.lower() for n in taken}
    derived_taken = {d for d in (derived(n) for n in taken) if d}
    name, n = wanted, 2
    while name.lower() in lowered or (derived(name) and derived(name) in derived_taken):
        name, n = f"{wanted}_{n}"[:64], n + 1
    return name


def _profile_names_for(modlist_name: str, mo2_profiles: "list[str]",
                       existing: "set[str]", game=None) -> "dict[str, str]":
    """``{MO2 profile: Mosaic profile}`` for one install.

    A modlist with several profiles names each after its own MO2 profile --
    that is the name the user picked and the one they'll see in the profile
    menu -- and only a list with none falls back to the modlist's name.
    """
    taken = set(existing)
    out: "dict[str, str]" = {}
    for mo2 in mo2_profiles or [""]:
        wanted = _profile_base_name(mo2) if mo2 else _profile_base_name(modlist_name)
        name = _unique_profile_name(wanted, taken, game)
        taken.add(name)
        out[mo2] = name
    return out


class WabbajackController(QObject):
    _picked = Signal(object)                 # Path | None from the portal picker
    _parsed = Signal(object, object, object)  # path, ModList | None, error | None
    _status = Signal(str)
    _log = Signal(str)
    _dl = Signal(str, object)                # "start"|"progress"|"finish", payload
    _build = Signal(int, int)
    _needed = Signal(int, object)             # archives, bytes this run needs
    _finished = Signal(object, object)       # report | None, error | None
    _ask_manual = Signal(object)             # {"archive", "reason", "holder", "event"}
    _ask_login = Signal(object)              # {"holder", "event"}
    _ask_resume = Signal(object)             # {"present", "total", "holder", "event"}

    def __init__(self, window):
        super().__init__(window)
        self._win = window
        self._view = None
        self._overlay = None
        self._control = None
        self._running = False
        self._profile_name = ""
        self._profile_names: "list[str]" = []
        self._profile_dirs: "list[Path]" = []
        # Manual archives already sitting in the download folders, found by
        # the preflight queue pane. Only the count is used (to re-render the
        # check): the install re-finds the files themselves via
        # find_downloaded(), since they never leave the download folders.
        self._manual_found: "dict[str, Path]" = {}
        self._picked.connect(self._on_picked)
        self._parsed.connect(self._on_parsed)
        self._status.connect(lambda t: self._overlay and self._overlay.set_status(t))
        self._log.connect(lambda t: self._win._append_log(t))
        self._dl.connect(self._on_dl)
        self._build.connect(lambda d, t: self._overlay and self._overlay.build_progress(d, t))
        self._needed.connect(
            lambda n, b: self._overlay and self._overlay.set_download_totals(n, b))
        self._finished.connect(self._on_finished)
        self._ask_manual.connect(self._on_ask_manual)
        self._ask_login.connect(self._on_ask_login)
        self._ask_resume.connect(self._on_ask_resume)

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

    # -- gallery browser ----------------------------------------------------------
    BROWSER_KEY = "wabbajack_browser"

    def open_browser(self) -> None:
        """Open (or focus) the Wabbajack modlist browser tab."""
        game = self._win._gs.game
        if game is None or not game.is_configured():
            self._win._notify(self.tr("No configured game selected."), "warning")
            return
        if self._win._tabs.has_key(self.BROWSER_KEY):
            self._win._tabs.focus_key(self.BROWSER_KEY)
            return
        from Utils.config_paths import get_config_dir
        from gui_qt.wabbajack.wabbajack_browser_view import WabbajackBrowserView
        root = get_config_dir() / "wabbajack"
        view = WabbajackBrowserView(
            game, cache_path=root / "gallery.json", files_dir=root / "modlists",
            on_file_ready=self.open_file, log_fn=self._win._append_log,
            notify=self._win._notify)
        self._browser = view
        view.destroyed.connect(lambda *_: setattr(self, "_browser", None))
        self._win._tabs.open_tab(view, self.tr("Wabbajack"), key=self.BROWSER_KEY)

    def on_game_changed(self, game) -> None:
        view = getattr(self, "_browser", None)
        if view is None:
            return
        if game is None:
            self._win._tabs.close_tab(self.BROWSER_KEY)
            return
        view.set_game(game)

    def open_file(self, path) -> None:
        """Read a ``.wabbajack`` file and open its import tab."""
        if self._running:
            self._win._notify(self.tr("A Wabbajack install is already running."), "warning")
            return
        self._on_picked(path)

    # -- LoversLab ----------------------------------------------------------------
    def loverslab_login(self) -> None:
        from gui_qt.wabbajack.loverslab_login_overlay import LoversLabLoginOverlay

        def done(ok):
            if ok:
                self._win._notify(self.tr("Logged in to LoversLab."), "info")

        LoversLabLoginOverlay.show_over(self._win, done)

    def loverslab_logout(self) -> None:
        from Utils.wabbajack.downloaders import loverslab_auth
        loverslab_auth.clear_session()
        self._win._notify(self.tr("LoversLab login cleared."), "info")

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
        from Utils.wabbajack.downloaders.game_file_source import runtime_swap_originals
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
            originals=runtime_swap_originals(game.name),
            loverslab_logged_in=bool(loverslab_auth.load_session()),
            manual_resolved=len(self._manual_found))

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
        self._manual_found.clear()
        from Utils.wabbajack.wabbajack_install import profile_choices
        repairable = self._repair_targets()
        self._view = WabbajackImportView(
            modlist, self._preflight(modlist), profiles=profile_choices(modlist),
            repair_profiles=sorted(repairable.values()),
            preselect=sorted(p for p in repairable if p),
            on_repair=lambda _p=None: self._start_install(repair=self._repair_targets()),
            on_install=self._start_install, on_login=self._login_from_view,
            on_manual=self._manual_from_view)
        self._win._tabs.open_tab(
            self._view, self.tr("Wabbajack: {0}").format(modlist.name or path.stem), key=key)

    def _manual_from_view(self, archive_hash, path) -> None:
        """A manual archive turned up in the user's downloads (or was picked by
        hand). Re-run preflight so the Manual downloads row counts it, the same
        way the LoversLab login does."""
        self._manual_found[archive_hash] = path
        if self._view is not None and self._modlist is not None:
            self._view.set_checks(self._preflight(self._modlist))

    def _login_from_view(self) -> None:
        from gui_qt.wabbajack.loverslab_login_overlay import LoversLabLoginOverlay

        def done(ok):
            if ok and self._view is not None:
                self._view.set_checks(self._preflight(self._modlist))

        LoversLabLoginOverlay.show_over(self._win, done)

    # -- install ----------------------------------------------------------------
    def _repair_targets(self) -> "dict[str, str]":
        """``{MO2 profile: Mosaic profile}`` for every existing profile
        installed from this same modlist (name and version). One modlist can
        have produced several, so the MO2 profile each came from -- recorded
        at install time -- is what tells them apart."""
        from Utils.exe_launch.game_helpers import _profiles_for_game
        from Utils.profile.profile_state import read_wabbajack_modlist_info
        game, modlist = self._win._gs.game, self._modlist
        found: "dict[str, str]" = {}
        if game is None or modlist is None:
            return found
        for name in _profiles_for_game(game.name):
            info = read_wabbajack_modlist_info(Path(game.get_profile_root()) / "profiles" / name)
            if (info and info.get("name") == modlist.name
                    and info.get("version") == modlist.version):
                found.setdefault(info.get("mo2_profile") or "", name)
        return found

    def _start_install(self, profiles=None, repair=None) -> None:
        """Install one or more of the modlist's MO2 profiles.

        Each becomes a Mosaic profile of its own, but they share one copy of
        the mods: every profile keeps ``profile_specific_mods`` and its own
        overwrite/filemap/index, while its ``mods`` entry is a symlink to one
        folder beside ``profiles/``. A modlist's profiles differ only in which
        of a shared pool they enable, so staging the pool once is the whole
        point -- Fashionista - Steam is 405 mods and 69 GB for all 17.
        """
        from Utils.exe_launch.game_helpers import _create_profile, _profiles_for_game
        from Utils.profile.profile_state import link_shared_mods
        from Utils.wabbajack.wabbajack_install import (
            WabbajackInstallCallbacks,
            WabbajackInstallControl,
        )
        from gui_qt.wabbajack.wabbajack_install_overlay import WabbajackInstallOverlay
        if self._running:
            return
        game = self._win._gs.game
        modlist = self._modlist
        if isinstance(profiles, str):
            profiles = [profiles]
        if repair:
            targets = {mo2: Path(game.get_profile_root()) / "profiles" / nm
                       for mo2, nm in repair.items()}
        else:
            names = _profile_names_for(modlist.name, list(profiles or []),
                                       set(_profiles_for_game(game.name)), game)
            group = _profile_base_name(modlist.name)
            # One profile needs no sharing: it keeps its own mods folder, as
            # every Wabbajack install did before.
            shared = (Path(game.get_mod_staging_path()).parent / "shared_mods" / group
                      if len(names) > 1 else None)
            targets, made = {}, []
            try:
                for mo2, nm in names.items():
                    d = Path(_create_profile(game.name, nm, profile_specific_mods=True))
                    made.append(d)
                    if shared is not None:
                        link_shared_mods(d, shared, group)
                    targets[mo2] = d
            except Exception as exc:
                for d in made:          # don't leave half a set behind
                    shutil.rmtree(d, ignore_errors=True)
                self._win._notify(self.tr("Could not create profile: {0}").format(exc), "error")
                return

        self._running = True
        self._profile_dirs = list(targets.values())
        self._profile_names = [d.name for d in targets.values()]
        name = self._profile_names[0]
        self._profile_name = name
        if self._view is not None:
            self._view.set_installing(True)
            # The install does its own download-folder scan; two watchers
            # hashing the same files helps nobody.
            self._view.stop_manual_queue()
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
            on_archives_needed=lambda n, b: safe_emit(self._needed, int(n), int(b)),
            request_manual_download=self._blocking(self._ask_manual, ("archive", "reason")),
        )
        api = self._win._ensure_nexus_api()
        threading.Thread(target=self._install_worker,
                         args=(game, api, targets, callbacks, self._control, bool(repair)),
                         daemon=True, name="wabbajack-install").start()

    def _install_worker(self, game, api, targets, callbacks, control, resume=False) -> None:
        from Nexus.nexus_download import NexusDownloader
        from Utils.config_paths import get_download_cache_dir_for_game
        from Utils.wabbajack.wabbajack_directives import path_substitutions
        from Utils.wabbajack.wabbajack_install import run_wabbajack_install
        from Utils.wine_proton.wine_paths import to_wine_path
        try:
            download_dir = get_download_cache_dir_for_game(game.name)
            profile_dir = next(iter(targets.values()))
            # Resolve the new profile's staging first: the substitution table
            # must describe where this profile's files actually live.
            game.set_active_profile_dir(profile_dir)
            game.load_paths()

            # Deleting a profile does not delete shared staging, so a re-import
            # can usually reuse most of what the last run built. Offer that
            # rather than silently rebuilding -- or silently reusing files the
            # user may have meant to discard.
            if not resume:
                from Utils.wabbajack.wabbajack_install import prebuilt_mod_count
                present, total = prebuilt_mod_count(
                    self._modlist, Path(game.get_effective_mod_staging_path()))
                if present:
                    ask = self._blocking(self._ask_resume, ("present", "total"))
                    resume = bool(ask(present, total))

            game_path = game.get_game_path()
            substitutions = path_substitutions(
                game_path=to_wine_path(game_path) if game_path else None,
                install_path=to_wine_path(Path(game.get_effective_mod_staging_path()).parent),
                download_path=to_wine_path(download_dir))
            report = run_wabbajack_install(
                wabbajack_path=self._path, modlist=self._modlist, game=game,
                profile_dirs=targets, download_dir=download_dir,
                nexus_downloader=(NexusDownloader(api, download_dir=download_dir)
                                  if api is not None else None),
                substitutions=substitutions, callbacks=callbacks, control=control,
                resume=resume)
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
        from gui_qt.wabbajack.manual_download_overlay import CANCEL_INSTALL

        def done(result):
            if result == CANCEL_INSTALL:
                if self._control is not None:
                    self._control.cancel.set()
                result = None
            self._answer(payload, result)

        ManualDownloadOverlay.show_over(
            self._win, payload["archive"], payload["reason"], done)

    def _on_ask_resume(self, payload) -> None:
        """Offer to reuse mod folders a previous run already built."""
        from PySide6.QtWidgets import QMessageBox
        if self._control is not None and self._control.cancel.is_set():
            self._answer(payload, False)
            return
        present, total = payload["present"], payload["total"]
        box = QMessageBox(self._win)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(self.tr("Reuse existing files?"))
        box.setText(self.tr("{0} of {1} mod folders from this modlist are already "
                            "in the staging folder.").format(present, total))
        box.setInformativeText(self.tr(
            "Reuse them and only build what is missing, or rebuild everything "
            "from scratch?\n\nFiles that don't match the modlist are rebuilt "
            "either way."))
        reuse = box.addButton(self.tr("Reuse"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(self.tr("Rebuild all"), QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(reuse)
        box.exec()
        self._answer(payload, box.clickedButton() is reuse)

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
        self._snapshot_as_installed(report)
        text, ok = install_summary(report, self._profile_names or [self._profile_name])
        if self._overlay is not None:
            self._overlay.finish(text, ok)
        if self._profile_name:
            self._win._select_installed_collection_profile(self._profile_name)

    def _snapshot_as_installed(self, report) -> None:
        """Keep each new profile's as-installed state as a backup it can be
        reverted to.

        A Wabbajack modlist is a fixed setup its author tested; enabling or
        removing a mod afterwards should be undoable without reinstalling
        70 GB. The backup holds the profile's modlist, load order and
        settings -- which is the whole of what the modlist decides -- and is
        marked kept so pruning never reaches it.
        """
        from Utils.profile.profile_backup import (
            create_backup, set_backup_kept, set_backup_label)
        label = self.tr("As installed — {0}").format(
            getattr(self._modlist, "name", "") or self.tr("Wabbajack modlist"))
        for d in self._profile_dirs:
            try:
                b = create_backup(d, manual=True)
                set_backup_kept(b, True)
                set_backup_label(b, label)
            except OSError as exc:
                self._win._append_log(
                    f"[wabbajack] couldn't save the as-installed state for {d.name}: {exc}")

    def _on_overlay_closed(self) -> None:
        self._overlay = None

"""The play bar's Steam build dropdown — switch build without the wizard.

The wizard (``wizards_qt/game_version_view.py``) is how a build first arrives:
it walks the user through pasting ``download_depot`` lines into Steam's
console. Once a build's files are on hand that walk is pure overhead, and it
was the only way back and forth, so going from one collection's build to
another's meant four wizard pages each time.

Mosaic keeps a complete file set for every build that isn't in the game folder
(see the store in ``Utils.steam.depot_versions``), so this dropdown can offer
the whole list and switch to anything already on disk. Picking a build that
isn't opens the wizard at its download step instead.

The classify step reads Steam's console logs, which run to several MB, so it
happens on a worker thread when something is picked -- never while building
the menu, which is on the UI thread of every profile change and deploy.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from gui_qt.safe_emit import safe_emit
from gui_qt.selector_button import SelectorButton


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


class GameVersionPicker(QObject):
    """Owns the play bar's build selector and everything it does."""

    _classified = Signal(object, object)       # (SwitchOption | None, error)
    _finished = Signal(object, object)         # (label | None, error)
    _labelled = Signal(object, object)         # (generation, list[SwitchOption])

    def __init__(self, *, host, log_fn, notify, confirm, message, open_wizard,
                 run_restore=None, on_busy=None, height=None, parent=None):
        super().__init__(parent)
        self._host = host
        self._log = log_fn
        self._notify = notify
        self._confirm = confirm
        self._message = message
        self._open_wizard = open_wizard
        self._run_restore = run_restore
        self._on_busy = on_busy or (lambda _b: None)
        self._game = None
        self._app_id = ""
        self._current = ""
        self._busy = False
        self._gen = 0
        self._label_of: "dict[str, str]" = {}   # menu text -> real build label

        self.button = SelectorButton(
            items=["—"], current="—", prefix=self.tr("Build: "), min_width=0,
            on_select=self._on_select,
            actions=[(self.tr("Game version wizard…"), self._open_wizard)])
        self.button.setToolTip(self.tr(
            "Which Steam build of the game is installed. Builds Mosaic already has the "
            "files for switch over without downloading anything."))
        if height is not None:
            self.button.setFixedHeight(height)
        self.button.hide()
        self._classified.connect(self._on_classified)
        self._finished.connect(self._on_finished)
        self._labelled.connect(self._on_labelled)

    # ---- building the menu (UI thread, so: small files only) -----------------
    def refresh(self, game) -> None:
        """Repopulate from the version table and the store, and hide the
        button for a game that has neither. Deliberately cheap: two small JSON
        files and the appmanifest, no Steam logs and no PE version read."""
        self._game = game
        self._app_id = ""
        if game is None:
            self.button.hide()
            return
        try:
            labels, current, names = self._list_builds(game)
        except Exception as exc:                 # never break the play bar
            self._log(f"Game version: couldn't list builds: {exc}")
            self.button.hide()
            return
        if len(labels) < 2:
            self.button.hide()
            return
        self._current = current
        # No set_highlighted_item: its green "✓ deployed" marker means mods in
        # this app, and the installed build is always the selected one anyway,
        # so the menu's own radio check already says which it is.
        self._label_of = {names.get(lbl, lbl): lbl for lbl in labels}
        self.button.set_items([names.get(lbl, lbl) for lbl in labels],
                              current=names.get(current, current))
        self.button.show()
        self._annotate(game)

    def _annotate(self, game) -> None:
        """Mark each build with whether switching to it would download, on a
        worker thread: saying so needs Steam's console logs, several MB of
        them, and this runs on every profile change and finished deploy."""
        self._gen += 1
        gen = self._gen
        root, app_id, name = game.get_game_path(), self._app_id, game.name

        def worker():
            opts = []
            try:
                from Utils.config_paths import get_game_config_dir
                from Utils.steam import depot_versions as dv
                state_dir = get_game_config_dir(name)
                table = dv.load_table(app_id, state_dir / dv.USER_VERSIONS_FILENAME)
                opts = dv.switch_options(root, app_id, state_dir, table)
            except Exception as exc:
                self._log(f"Game version: couldn't check what's on hand: {exc}")
            safe_emit(self._labelled, gen, opts)

        threading.Thread(target=worker, daemon=True, name="version-annotate").start()

    def _on_labelled(self, gen, opts) -> None:
        if gen != self._gen or not opts or self._busy:
            return                                # a later refresh won the race
        from Utils.steam import depot_versions as dv
        shown, self._label_of = [], {}
        for o in opts:
            if o.state == dv.SwitchOption.CURRENT:
                text = o.display
            elif o.instant:
                text = self.tr("{0}   ·  no download").format(o.display)
            else:
                text = self.tr("{0}   ·  needs downloading").format(o.display)
            shown.append(text)
            self._label_of[text] = o.label
        self.button.set_items(shown, current=self._label_of and next(
            (t for t, lbl in self._label_of.items() if lbl == self._current), shown[0]))

    def _list_builds(self, game) -> "tuple[list[str], str, dict[str, str]]":
        from Utils.config_paths import get_game_config_dir
        from Utils.steam import depot_versions as dv
        from Utils.wine_proton.steam_finder import game_steam_id
        app_id = str(game_steam_id(game) or getattr(game, "steam_id", "") or "")
        root = game.get_game_path()
        if not app_id or root is None:
            return [], "", {}
        self._app_id = app_id
        state_dir = get_game_config_dir(game.name)
        table = dv.load_table(app_id, state_dir / dv.USER_VERSIONS_FILENAME)
        index = dv.read_index(state_dir)
        # exe_file="" on purpose: the label only needs the appmanifest's build
        # id, and reading a 37 MB exe's version resource here would cost a
        # file read on every profile change.
        base = dv.capture_current(root, app_id, "")
        base_label = index.get("base") or (base.label if base else "")
        current = index.get("current") or base_label
        labels: "list[str]" = []
        for label in ([base_label] + [e.label for e in table.versions]
                      + dv.stored_labels(state_dir) + [current]):
            if label and label not in labels:
                labels.append(label)
        # Reading one version resource per build, which is an mmap and a seek:
        # worth it here so the button never flashes a raw build id first.
        return labels, current, dv.build_names(root, state_dir, table.exe_file, index)

    # ---- picking one --------------------------------------------------------
    def _on_select(self, shown: str) -> None:
        label = self._label_of.get(shown, shown)
        if self._busy or not label or label == self._current:
            return
        game = self._game
        if game is None or not self._app_id:
            return
        self._set_busy(True)
        self._log(f"Game version: checking what switching to {label} needs…")
        root, app_id, name = game.get_game_path(), self._app_id, game.name
        deployed = self._deployment_traces(game)

        def worker():
            option = err = None
            try:
                from Utils.config_paths import get_game_config_dir
                from Utils.steam import depot_versions as dv
                state_dir = get_game_config_dir(name)
                table = dv.load_table(app_id, state_dir / dv.USER_VERSIONS_FILENAME)
                opts = dv.switch_options(root, app_id, state_dir, table)
                option = next((o for o in opts if o.label == label), None)
                if option is None:
                    err = self.tr("Mosaic no longer has {0} in its version list.").format(label)
                elif deployed:
                    err = self.tr(
                        "Mods are deployed ({0}), and a build switch replaces the game's own "
                        "files. Restore first, then pick the build.").format("; ".join(deployed))
                    option = None
                elif dv.runtime_swap_blocker(root, state_dir):
                    # Checked here, not in the confirmation: it reads a file's
                    # hash, and saying no up front beats asking and refusing.
                    err = dv.runtime_swap_blocker(root, state_dir)
                    option = None
            except Exception as exc:
                err = str(exc)
            safe_emit(self._classified, option, err)

        threading.Thread(target=worker, daemon=True, name="version-classify").start()

    def _deployment_traces(self, game) -> "list[str]":
        from Utils.steam import depot_versions as dv
        try:
            active = bool(game.get_deploy_active())
        except Exception:
            active = False
        try:
            return dv.deployment_traces(game.get_game_path(), deploy_active=active)
        except Exception:
            return []

    def _on_classified(self, option, err) -> None:
        self._set_busy(False)
        self._show_current()                         # until it really changed
        if err:
            self._log(f"Game version: {err}")
            if self._run_restore is not None and "Restore first" in err:
                self._confirm(self.tr("Switch build"), err,
                              self._on_restore_first, confirm_label=self.tr("Restore now"))
            else:
                self._message(self.tr("Switch build"), err)
            return
        from Utils.steam import depot_versions as dv
        if option.state == dv.SwitchOption.NEEDS_DOWNLOAD:
            self._confirm(
                self.tr("Switch build"),
                self.tr("{0} isn't on this computer yet — {1} depot(s) have to come from "
                        "Steam first. Open the Game version wizard?")
                .format(option.display, len(option.download)),
                lambda ok: self._open_wizard() if ok else None,
                confirm_label=self.tr("Open wizard"))
            return
        if option.state == dv.SwitchOption.STORED:
            body = self.tr(
                "Switch to {0} using the {1} of its files Mosaic keeps. Nothing is "
                "downloaded. The build you're on now is kept in its place, so you can "
                "switch straight back.").format(option.display, _gb(option.size_bytes))
        else:
            body = self.tr(
                "Switch to {0} using the {1} depot(s) Steam has already downloaded. The "
                "build you're on now is kept, so you can switch straight back.") \
                .format(option.display, len(option.depot_dirs))
        self._confirm(self.tr("Switch build"), body, lambda ok: self._run(option) if ok else None,
                      confirm_label=self.tr("Switch"))

    def _on_restore_first(self, ok: bool) -> None:
        if ok and self._run_restore is not None:
            self._run_restore(lambda _ok: None)

    # ---- doing it -----------------------------------------------------------
    def _run(self, option) -> None:
        game = self._game
        if game is None:
            return
        self._set_busy(True)
        self._notify(self.tr("Switching to {0}…").format(option.display), "info")
        root, name = game.get_game_path(), game.name
        exe = getattr(game, "exe_name", "") or ""

        def worker():
            label = err = None
            try:
                from Utils.config_paths import get_game_config_dir
                from Utils.steam import depot_versions as dv
                label = dv.switch_to(root, get_game_config_dir(name), option,
                                     exe_file=exe, log_fn=self._log)
            except Exception as exc:
                err = str(exc)
            safe_emit(self._finished, label, err)

        threading.Thread(target=worker, daemon=True, name="version-switch").start()

    def _on_finished(self, label, err) -> None:
        self._set_busy(False)
        self.refresh(self._game)
        if err:
            self._log(f"Game version: {err}")
            self._message(self.tr("Switch build"), err)
            return
        self._notify(self.tr("Now on {0}.").format(self.button.current()), "success")

    def _show_current(self) -> None:
        """Put the button face back on the build that is actually installed."""
        for text, label in self._label_of.items():
            if label == self._current:
                self.button.set_current(text)
                return
        self.button.set_current(self._current)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.button.setEnabled(not busy)
        self._on_busy(busy)

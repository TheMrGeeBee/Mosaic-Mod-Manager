"""Plugin panel: load/sort/filter, ESL scan, userlist rules and groups, framework banner.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import threading
import time as _time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from gui_qt.flow_layout import FlowLayout
from gui_qt.theme.theme_qt import _c


class MainWindow:

    def _on_plugin_selection_changed(self):
        if self._suppress_xpanel():
            return
        self._xpanel_busy = True
        try:
            mv, pv = self._modlist_view, self._plugin_view
            owner = (self._conflict_data.plugin_owner
                     if self._conflict_data else {})
            mods = pv.selected_owner_mods(owner)
            # Plugin selected → orange its owning mod, clear mod conflict tint.
            # Suppressed in View Requirements mode (purple/blue tints own the
            # modlist while that tab is open).
            if not self._req_tab_active():
                mv.set_highlighted_mods(mods)
            mv.clearSelection()
            # Green-tick the selected plugin's masters in the plugin marker
            # strip (Tk parity).
            pv.set_master_highlight(self._selected_plugin_masters())
        finally:
            self._xpanel_busy = False
        # Follow the selection in the open Plugin Rules tab (Tk parity:
        # _on_plugin_row_selected_cb → set_selected_plugin).
        if self._tabs.has_key("plugin_rules"):
            rules_view = getattr(self, "_plugin_rules_view", None)
            sel = self._plugin_view.selectionModel().selectedRows()
            if rules_view is not None and sel:
                rules_view.set_selected_plugin(
                    self._plugin_model.row(sel[0].row()).name)

    def _selected_plugin_masters(self) -> set:
        """Lowercase master filenames of the currently-selected plugin(s), read
        from each plugin's resolved on-disk path. Empty if nothing selected or
        paths are unavailable."""
        pv = self._plugin_view
        rows = pv.selectionModel().selectedRows()
        if not rows:
            return set()
        paths = getattr(self, "_plugin_paths", None)
        if not paths:
            return set()
        from Utils.plugins.plugin_parser import read_masters
        masters: set = set()
        for idx in rows:
            r = self._plugin_model.row(idx.row())
            p = paths.get(r.name.lower())
            if p is None:
                continue
            try:
                masters.update(m.lower() for m in read_masters(p))
            except Exception:
                pass
        return masters

    def _plugins_footer(self) -> QWidget:
        """Colored tool buttons + search, under the plugins."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        _made = {}
        self._plugin_footer_btns: list = []
        for label, disp, key in [
            ("Sort Plugins", self.tr("Sort Plugins"), "BTN_SUCCESS"),
            ("Refresh Plugins", self.tr("Refresh Plugins"), "BTN_INFO"),
            ("Groups", self.tr("Groups"), "BTN_INFO"),
            ("Plugin Rules", self.tr("Plugin Rules"), "BTN_INFO"),
        ]:
            b = self._color_button(disp, _c(self._pal, key), compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            btns.addWidget(b)
            _made[label] = b
            self._plugin_footer_btns.append(b)
        v.addLayout(btns)
        self._plugin_sort_btn = _made["Sort Plugins"]
        self._plugin_refresh_btn = _made["Refresh Plugins"]
        self._plugin_groups_btn = _made["Groups"]
        self._plugin_rules_btn = _made["Plugin Rules"]
        self._plugin_filters_btn = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True)
        self._plugin_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._plugin_footer_btns.append(self._plugin_filters_btn)
        self._plugin_sort_btn.clicked.connect(self._on_sort_plugins)
        self._plugin_refresh_btn.clicked.connect(self._on_refresh_plugins)
        self._plugin_groups_btn.clicked.connect(self._open_plugin_groups_tab)
        self._plugin_rules_btn.clicked.connect(self._open_plugin_rules_tab)
        self._plugin_filters_btn.clicked.connect(self._toggle_plugin_filters)

        # Search row: a total / non-ESL count label (blue outline) to the left
        # of a full-width search box, mirroring the modlist footer.
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)

        count = QLabel("…")
        count.setObjectName("PluginCountLabel")
        count.setAlignment(Qt.AlignCenter)
        count.setStyleSheet(
            "QLabel#PluginCountLabel {"
            f" color: {_c(self._pal, 'TEXT_MAIN')};"
            f" border: 1px solid {_c(self._pal, 'ACCENT')};"
            " border-radius: 4px; padding: 2px 8px; }")
        self._plugin_count = count
        search_row.addWidget(count)
        search_row.addWidget(self._plugin_filters_btn)

        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search plugins…"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._on_plugin_search)
        self._plugins_search = search
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        return bar

    def _guard_missing_plugins(self, game, proceed):
        """UI thread: call *proceed()* unless enabled plugins of the deployed
        profile are missing from the game's Data folder, in which case say so and
        let the user cancel (or launch anyway). Never blocks on an internal error."""
        try:
            missing = (game.find_missing_plugin_files(self._gs.profile)
                       if hasattr(game, "find_missing_plugin_files") else [])
        except Exception as exc:                          # noqa: BLE001
            self._append_log(f"[play] missing-plugin check skipped: {exc}")
            missing = []
        if not missing:
            proceed()
            return
        self._append_log(f"[play] {len(missing)} enabled plugin(s) are missing from Data/: "
                         + ", ".join(missing[:10]))
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        shown = "\n".join(f"• {m}" for m in missing[:8])
        if len(missing) > 8:
            shown += "\n" + self.tr("…and {0} more").format(len(missing) - 8)
        ConfirmOverlay.show_over(
            self, self.tr("{0} enabled plugin(s) are missing").format(len(missing)),
            self.tr("The game would start without these files, which usually ends in "
                    "a crash on the loading screen:\n\n{0}\n\nThe deployment is out "
                    "of date (mods were renamed, moved or removed since the last "
                    "Deploy). Press Deploy, then Play again.").format(shown),
            lambda ok: proceed() if ok else None,
            confirm_label=self.tr("Launch anyway"), cancel_label=self.tr("Cancel"),
            danger=False, card_h=min(300 + 18 * min(len(missing), 8), 520))

    def _guard_bsa_memory_map(self, game, proceed):
        """UI thread: call *proceed()* unless BSA Memory Map is deployed and
        switched on. Under Proton it fills the memory SKSE plugins need for
        their hooks, so they fail with "Failed to hook …" while the game loads.
        Offers to switch it off (only on request: it edits the mod's ini),
        launch anyway, or cancel. Never blocks on an internal error."""
        try:
            finding = (game.find_bsa_memory_map(self._gs.profile)
                       if hasattr(game, "find_bsa_memory_map") else None)
        except Exception as exc:                          # noqa: BLE001
            self._append_log(f"[play] BSA Memory Map check skipped: {exc}")
            finding = None
        if finding is None:
            proceed()
            return
        self._append_log(f"[play] BSA Memory Map is enabled ({finding.dll}); under "
                         "Proton it makes SKSE plugins fail to hook.")
        can_fix = finding.can_disable

        def _done(choice):
            if choice is True and can_fix:
                from Utils.modding_tools import bsa_memory_map as bmm
                try:
                    for p in bmm.disable(finding):
                        self._append_log(f"[play] BSA Memory Map: set bEnabled=0 in {p}")
                except OSError as exc:
                    self._append_log(f"[play] BSA Memory Map: could not edit the ini: {exc}")
                    self._notify(self.tr("Couldn't switch BSA Memory Map off — see log."),
                                 "error")
                    return
                proceed()
            elif choice == "alt" or (choice is True and not can_fix):
                proceed()

        body = self.tr(
            "BSA Memory Map (BSAMemoryMap.dll, \"Faster File Copy\" in LoreRim) is "
            "switched on. Under Proton it fills the memory next to the game that "
            "other SKSE plugins need for their hooks, so they fail while the game "
            "loads with errors like \"Failed to hook …\" or \"no trampoline "
            "available\". Windows isn't affected.\n\n"
            "Switching it off (bEnabled=0 in BSAMemoryMap.ini) fixes this. The only "
            "cost is slightly slower BSA reads.")
        if can_fix:
            overlay_kw = dict(
                confirm_label=self.tr("Switch off and launch"),
                alt_label=self.tr("Launch anyway"))
        else:
            body += "\n\n" + self.tr(
                "Mosaic couldn't find a bEnabled line to change. Add bEnabled=0 to "
                "the plugin's BSAMemoryMap.ini yourself.")
            overlay_kw = dict(confirm_label=self.tr("Launch anyway"))
        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("BSA Memory Map will break SKSE hooks"), body, _done,
            cancel_label=self.tr("Cancel"), danger=False, card_h=330,
            **overlay_kw)

    def _build_plugin_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.modlist.modlist_filter import PLUGIN_STATUS_FILTERS
        items = [(key, label, True) for key, label in PLUGIN_STATUS_FILTERS]
        spec = [{"title": "By status", "type": "checks", "items": items}]
        panel = FilterSidePanel(spec, title=self.tr("Filters"))
        panel.changed.connect(self._on_plugin_filter_changed)
        panel.close_requested.connect(self._toggle_plugin_filters)
        self._plugin_filter_state: dict = {}
        return panel

    def _toggle_plugin_filters(self):
        panel = self._plugin_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._apply_plugin_filters()
        else:
            panel.setVisible(False)

    def _on_plugin_filter_changed(self, state: dict):
        self._plugin_filter_state = state
        self._apply_plugin_filters()
        active = self._plugin_filter_panel.any_active()
        b = getattr(self, "_plugin_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _quick_plugin_filter_state(self, key: str) -> int:
        """Current tri-state for a plugin status filter (for the column-menu
        Filters submenu check marks). Reads the panel so it reflects edits."""
        panel = getattr(self, "_plugin_filter_panel", None)
        if panel is not None:
            return panel.check_state(key)
        return (getattr(self, "_plugin_filter_state", {}) or {}).get(key, 0)

    def _on_quick_plugin_filter(self, key: str, state: int):
        """Apply a plugin status filter chosen from the column menu by driving
        the Filters panel checkbox, so the panel and menu stay in sync and the
        normal filter pipeline (footer-button tint included) runs."""
        panel = getattr(self, "_plugin_filter_panel", None)
        if panel is not None:
            panel.set_check(key, state)   # emits changed -> _on_plugin_filter_changed
        else:   # panel not built yet — fall back to the state dict directly
            self._plugin_filter_state[key] = state
            self._apply_plugin_filters()

    def _apply_plugin_filters(self):
        """Push the filter-hidden row set to the plugin view (composes with the
        plugin search via the view's search/filter union)."""
        if not hasattr(self, "_plugin_view"):
            return
        from gui_qt.modlist.modlist_filter import plugin_filter_hidden_rows
        state = getattr(self, "_plugin_filter_state", None) or {}
        disabled_mf = self._disabled_plugin_files()
        hide = plugin_filter_hidden_rows(self._plugin_model._rows, state,
                                         disabled_mf=disabled_mf)
        self._plugin_view.set_filter_hidden(hide)

    def _disabled_plugin_files(self) -> set:
        """Plugin filenames (lower) disabled via the Mod Files tab, for the
        Plugins-tab 'Disabled plugins' filter union."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return set()
        try:
            from Utils.plugins.disabled_plugins import disabled_plugin_files
            return disabled_plugin_files(pdir, self._gs.game)
        except Exception:
            return set()

    def _on_plugin_search(self, text: str):
        """Plugins search: hide plugins whose name (or owning mod name) doesn't
        contain the query."""
        self._plugin_search_text = text
        t = getattr(self, "_plugin_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(120)
            t.timeout.connect(self._apply_plugin_search)
            self._plugin_search_timer = t
        t.start()

    def _apply_plugin_search(self):
        from gui_qt.modlist.modlist_filter import plugin_search_hidden_rows
        text = getattr(self, "_plugin_search_text", "")
        rows = self._plugin_model._rows
        owner = (self._conflict_data.plugin_owner
                 if getattr(self, "_conflict_data", None) else {})
        self._plugin_view.set_search_hidden(
            plugin_search_hidden_rows(rows, text, owner))

    def _build_disabled_plugins_mods(self) -> set:
        """Mods that own at least one disabled plugin — union of plugins disabled
        in plugins.txt and plugins disabled via the Mod Files tab (Tk-parity
        'Mods with disabled plugins' filter)."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return set()
        out: set[str] = set()
        # Mod Files "Disable" checkbox on a plugin file.
        try:
            from Utils.plugins.disabled_plugins import mods_with_disabled_plugins
            out |= mods_with_disabled_plugins(pdir, self._gs.game)
        except Exception:
            pass
        # plugins.txt-disabled plugins → their owning mod (via filemap owner map).
        try:
            cd = getattr(self, "_conflict_data", None)
            owner = dict(getattr(cd, "plugin_owner", {}) or {}) if cd else {}
            for r in getattr(self._plugin_model, "_rows", []) or []:
                if getattr(r, "vanilla", False) or getattr(r, "enabled", True):
                    continue
                mod = owner.get(getattr(r, "name", "").lower())
                if mod:
                    out.add(mod)
        except Exception:
            pass
        return out

    def _refresh_plugin_stats(self):
        """Update the plugins footer count label: total plugins / non-ESL. All
        the data is in-memory on the plugin model, so this is instant."""
        lbl = getattr(self, "_plugin_count", None)
        if lbl is None:
            return
        from gui_qt.modlist.modlist_data import compute_plugin_stats
        s = compute_plugin_stats(self._plugin_model._rows)
        lbl.setText(self.tr("P:{0} / Non-ESL:{1}").format(
            s['total'], s['non_esl']))
        lbl.setToolTip(self.tr("{0} plugins ({1} ESL, {2} non-ESL)").format(
            s["total"], s["esl"], s["non_esl"]))

    def _reload_plugins(self):
        """Load the active game/profile's plugins into the Plugins tab.

        The disk work (per-plugin header reads for the ESL/master flags,
        master checks, filemap parse) is hundreds of file opens on a large
        load order, so it runs on a daemon worker → _plugins_loaded → the
        UI applies the rows. A generation counter drops results from a
        superseded reload (game/profile switched mid-read)."""
        self._reassert_profile_paths()
        self._plugins_gen += 1
        gen = self._plugins_gen
        game, profile = self._gs.game, self._gs.profile
        ul_path = self._userlist_path()

        def worker():
            from gui_qt.plugin.plugin_state import (
                load_plugins,
                resolve_plugin_paths_for_game,
            )
            from Utils.perftrace import span
            from Utils.userlist import read_userlist_state
            # Poll for supersession between the load's expensive phases: a
            # newer reload bumping the gen makes this one's result dead on
            # arrival (dropped in _on_plugins_loaded), so stop working on it.
            stale = lambda: gen != self._plugins_gen
            try:
                with span("plugins.load_plugins(worker)"):
                    rows = load_plugins(game, profile, cancelled=stale)
            except Exception as exc:
                print(f"[gui_qt] plugin load failed: {exc}", flush=True)
                rows = []
            if rows is None or stale():
                msg = (f"plugin load gen={gen} cancelled mid-build "
                       f"(superseded by gen={self._plugins_gen})")
                print(f"[plugin-diag] {msg}", flush=True)
                self._append_log(f"[rescan-diag] {msg}")
                return
            with span("plugins.resolve_paths(worker)"):
                paths = (resolve_plugin_paths_for_game(game)
                         if game is not None else {})
            try:
                state = read_userlist_state(ul_path)
            except Exception:
                state = None
            self._plugins_loaded.emit(gen, rows, paths, state)

        threading.Thread(target=worker, daemon=True,
                         name="plugins-reload").start()

    def _clear_plugin_panel(self):
        """Blank the plugin panel while a reload that will repopulate it is in
        flight (profile switch: the conflict rebuild's plugin reload is the
        authoritative one). Bumps the generation so any in-flight load from
        the previous profile is dropped/cancelled."""
        self._plugins_gen += 1
        self._plugin_model.set_rows([], game=self._gs.game,
                                    profile=self._gs.profile,
                                    profile_dir=self._gs.profile_dir())
        self._plugin_view.set_master_highlight(set())
        self._plugin_view.refresh_missing_marker()
        self._plugin_view.refresh_cycle_marker()
        self._refresh_plugin_stats()

    def _on_plugins_loaded(self, gen, rows, paths, state):
        """UI thread: apply a finished plugin reload (see _reload_plugins)."""
        if gen != self._plugins_gen:
            msg = (f"plugins_loaded gen={gen} SUPERSEDED "
                   f"(current={self._plugins_gen}) — {len(rows)} row(s) DROPPED")
            print(f"[plugin-diag] {msg}", flush=True)
            self._append_log(f"[rescan-diag] {msg}")
            return   # superseded — a newer reload is in flight
        # Final link in the chain: how many plugin rows actually reached the
        # panel. If this is 0 while the index/filemap looked fine above, the
        # failure is in plugin discovery, not the filemap. GUI-visible so a
        # user can see + send it without an env var.
        self._append_log(f"[rescan-diag] plugins_loaded gen={gen} → applying "
                         f"{len(rows)} row(s) to the panel")
        _apply_t0 = _time.perf_counter()
        self._plugin_model.set_rows(rows, game=self._gs.game,
                                    profile=self._gs.profile,
                                    profile_dir=self._gs.profile_dir())
        # Context menu reads these off the view (ESL path resolution + refresh).
        self._plugin_view.game = self._gs.game
        self._plugin_view.profile_dir = self._gs.profile_dir()
        self._plugin_view.on_plugins_changed = self._reload_plugins
        self._apply_plugin_search()
        # Persistent red marker-strip ticks for plugins with missing masters
        # (Tk parity) — recomputed from the freshly-loaded PF_MISSING flags.
        self._plugin_view.refresh_missing_marker()
        # Red marker-strip ticks for plugins whose userlist rules form a broken
        # cycle — recomputed from the freshly-loaded PF_UL_CYCLE flags.
        self._plugin_view.refresh_cycle_marker()
        # Resolved plugin paths (name.lower → on-disk path) power the master-
        # highlight on plugin selection; clear any stale master ticks.
        self._plugin_paths = paths
        self._plugin_view.set_master_highlight(set())
        # Row indices/flags changed — re-apply any active plugin filter.
        if getattr(self, "_plugin_filter_panel", None) is not None:
            self._apply_plugin_filters()
        self._refresh_plugin_stats()
        self._refresh_framework_banner()
        self._apply_plugins_supported()
        # Rerun-FOMOD flag: an option's fileDependency plugin may have just become
        # enabled/disabled — recompute the overlay against the fresh load order.
        self._refresh_rerun_fomod_flags()
        # Userlist state → PF_USERLIST/PF_UL_CYCLE bits were already applied by
        # load_plugins; push the membership sets (context-menu predicates), the
        # group map (flags tooltip), and the userlist action callbacks.
        if state is None:
            from Utils.userlist import read_userlist_state
            state = read_userlist_state(self._userlist_path())
        self._userlist_state = state
        self._plugin_view.userlist_plugins = state.plugins
        self._plugin_view.userlist_cycles = state.cycle_plugins
        self._plugin_model.set_userlist_groups(state.group_map)
        self._plugin_view.on_groundcover_changed = self._on_groundcover_changed
        self._plugin_view.on_userlist_add = self._on_userlist_add
        self._plugin_view.on_group_add = self._on_group_add
        self._plugin_view.on_userlist_remove = self._on_userlist_remove
        self._plugin_view.on_show_cycle = self._open_plugin_cycle_tab
        self._plugin_view.on_show_overlapping = self._on_show_overlapping_plugins
        self._plugin_view.on_quick_filter = self._on_quick_plugin_filter
        self._plugin_view.quick_filter_state = self._quick_plugin_filter_state
        print(f"[gui_qt] plugins: {len(rows)} entries")
        from Utils import perftrace
        perftrace.mark("on_plugins_loaded(apply)",
                       _time.perf_counter() - _apply_t0)
        # ESL-eligibility (filters) computes AFTER the rows are visible — a
        # cold libloot scan is seconds of GIL-hogging record parsing.
        self._start_esl_scan(gen, rows, paths)
        # Profile-switch milestones: the FIRST plugin pass runs against the old
        # filemap (fast feedback); the FINAL pass follows the conflict rebuild
        # and is the moment the switch is fully rendered.
        if getattr(self, "_switch_conflicts_done", False):
            self._mark_since_switch("switch→plugins_final_applied")  # i18n: skip — perftrace marker label
            self._switch_t0 = None
            self._switch_conflicts_done = False
        else:
            self._mark_since_switch("switch→plugins_first_applied")  # i18n: skip — perftrace marker label
        # Last render step of first load — the plugin panel is now fully
        # populated. Dismiss the startup splash here (dropped one event-loop
        # turn later so this final dataChanged pass actually paints first).
        if not self._splash_dismissed and self._splash is not None:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._dismiss_splash)

    def _start_esl_scan(self, gen, rows, resolved):
        """Compute the ESL-safe/unsafe filter bits on a worker AFTER the plugin
        rows are applied. A cold libloot eligibility scan is seconds of
        full-record parsing that does not release the GIL — inside
        load_plugins it starved every other reload worker and the UI thread.
        Results are mtime-cached (plugin_state._ESL_ELIG_CACHE), so repeat
        loads resolve instantly."""
        game = self._gs.game
        if game is None or not getattr(game, "supports_esl_flag", False):
            return
        names = [r.name for r in rows]
        if not names:
            return

        def worker():
            from gui_qt.plugin.plugin_state import compute_esl_eligibility
            from Utils.perftrace import span
            data_dir = (game.get_vanilla_plugins_path()
                        if hasattr(game, "get_vanilla_plugins_path") else None)
            with span("plugins.esl_eligibility(deferred)"):
                kinds = compute_esl_eligibility(names, resolved, data_dir, game)
            self._esl_elig_ready.emit(gen, kinds)

        threading.Thread(target=worker, daemon=True, name="esl-elig").start()

    def _on_esl_elig_ready(self, gen, kinds):
        """UI thread: merge the deferred ESL-eligibility bits into the plugin
        rows (see _start_esl_scan) and reapply any active plugin filter that
        keys off them."""
        if gen != self._plugins_gen or not kinds:
            return   # superseded — a newer plugin reload is in flight/applied
        from gui_qt.plugin.plugin_model import COL_FLAGS, PFlagsRole
        from gui_qt.plugin.plugin_state import PF_ESL_SAFE, PF_ESL_UNSAFE
        m = self._plugin_model
        changed = False
        for r in m._rows:
            bits = kinds.get(r.name.lower())
            if bits is None:
                continue
            nf = (r.flags & ~(PF_ESL_SAFE | PF_ESL_UNSAFE)) | bits
            if nf != r.flags:
                r.flags = nf
                changed = True
        if not changed:
            return
        m.dataChanged.emit(m.index(0, COL_FLAGS),
                           m.index(len(m._rows) - 1, COL_FLAGS),
                           [PFlagsRole, Qt.ToolTipRole])
        # The ESL-safe/unsafe filters read these bits — reapply if active.
        if getattr(self, "_plugin_filter_panel", None) is not None:
            self._apply_plugin_filters()

    # ---- LOOT userlist (groups / rules / cycle / flag) ---------------------
    def _userlist_path(self):
        """<active profile>/userlist.yaml, or None without a profile (matches
        the Tk _get_userlist_path and the Sort Plugins path resolution)."""
        pdir = self._gs.profile_dir()
        return (pdir / "userlist.yaml") if pdir else None

    def _refresh_userlist_flags(self):
        """Light refresh after a userlist.yaml edit: re-read the state and
        update each plugin row's PF_USERLIST/PF_UL_CYCLE bits + the menu sets +
        the tooltip group map, without a full plugin reload (Tk parity:
        _refresh_userlist_set + _predraw)."""
        from gui_qt.plugin.plugin_model import COL_FLAGS, PFlagsRole
        from gui_qt.plugin.plugin_state import PF_UL_CYCLE, PF_USERLIST
        from Utils.userlist import read_userlist_state
        state = read_userlist_state(self._userlist_path())
        self._userlist_state = state
        self._plugin_view.userlist_plugins = state.plugins
        self._plugin_view.userlist_cycles = state.cycle_plugins
        m = self._plugin_model
        m.set_userlist_groups(state.group_map)
        for r in m._rows:
            r.flags &= ~(PF_USERLIST | PF_UL_CYCLE)
            low = r.name.lower()
            if low in state.plugins:
                r.flags |= PF_USERLIST
                if low in state.cycle_plugins:
                    r.flags |= PF_UL_CYCLE
        if m._rows:
            m.dataChanged.emit(m.index(0, COL_FLAGS),
                               m.index(len(m._rows) - 1, COL_FLAGS),
                               [PFlagsRole, Qt.ToolTipRole])

    def _on_userlist_bar_saved(self, message: str):
        """An inline bar (Add to userlist / Add to group) wrote userlist.yaml."""
        self._notify(message, "success")
        self._refresh_userlist_flags()

    def _close_userlist_ui(self):
        """Close the userlist-scoped tabs + hide the inline bars — they hold
        the previous game/profile's userlist path."""
        for key in ("plugin_groups", "plugin_rules", "plugin_cycle"):
            if self._tabs.has_key(key):
                self._tabs.close_tab(key)
        self._plugin_rules_view = None
        self._plugin_cycle_view = None
        self._cycle_anchor = ""
        self._cycle_scope = frozenset()
        for bar in (getattr(self, "_ul_bar", None),
                    getattr(self, "_grp_bar", None)):
            if bar is not None:
                bar.cancel()

    def _open_plugin_groups_tab(self):
        """Footer 'Groups' button → LOOT Groups tab over the modlist panel."""
        ul_path = self._userlist_path()
        if ul_path is None:
            self._notify(self.tr("No active profile — cannot configure groups."),
                         "warning")
            return
        # Tk closes any existing overlay first so the view reflects the file.
        if self._tabs.has_key("plugin_groups"):
            self._tabs.close_tab("plugin_groups")
        from gui_qt.plugin.plugin_groups_view import PluginGroupsView
        view = PluginGroupsView(
            ul_path,
            on_close=lambda: self._tabs.close_tab("plugin_groups"),
            on_saved=self._refresh_userlist_flags,
        )
        self._tabs.open_scoped_tab(view, self.tr("LOOT Groups"),
                                   self._modlist_panel_stack,
                                   key="plugin_groups")

    def _open_plugin_rules_tab(self):
        """Footer 'Plugin Rules' button → per-plugin rules tab over the
        modlist panel. Follows the plugins-panel selection while open."""
        ul_path = self._userlist_path()
        if ul_path is None:
            self._notify(self.tr("No active profile — cannot configure plugin rules."),
                         "warning")
            return
        if self._tabs.has_key("plugin_rules"):
            self._tabs.close_tab("plugin_rules")
        plugin_names = [r.name for r in self._plugin_model._rows]
        sel_rows = self._plugin_view.selectionModel().selectedRows()
        sel_name = (self._plugin_model.row(sel_rows[0].row()).name
                    if sel_rows else "")
        from gui_qt.plugin.plugin_rules_view import PluginRulesView
        view = PluginRulesView(
            plugin_names, ul_path, selected_plugin=sel_name,
            on_close=lambda: self._tabs.close_tab("plugin_rules"),
            on_saved=self._refresh_userlist_flags,
        )
        self._plugin_rules_view = view
        self._tabs.open_scoped_tab(view, self.tr("LOOT Plugin Rules"),
                                   self._modlist_panel_stack,
                                   key="plugin_rules")

    def _open_plugin_cycle_tab(self, plugin_name: str):
        """'Show cycle…' / 'Show userlist rules…' → cycle tab over the modlist
        panel, pinned to the plugin's SCC (or its whole rule component)."""
        ul_path = self._userlist_path()
        if ul_path is None:
            return
        from Utils.userlist import parse_userlist, userlist_rule_component
        name_lower = plugin_name.lower()
        state = getattr(self, "_userlist_state", None)
        component = (state.cycle_components.get(name_lower)
                     if state is not None else None)
        if not component:
            data = (parse_userlist(ul_path) if ul_path.is_file()
                    else {"plugins": [], "groups": []})
            component = userlist_rule_component(data, name_lower)
        if not component:
            self._notify(self.tr("{0} has no userlist rules to display.").format(plugin_name),
                         "info")
            return
        if self._tabs.has_key("plugin_cycle"):
            self._tabs.close_tab("plugin_cycle")
        # Freeze the plugin set at open time. Subsequent flips keep showing
        # these plugins' rules even if the cycle is gone, so the user can
        # revert or adjust further (Tk parity).
        self._cycle_anchor = name_lower
        self._cycle_scope = component
        from gui_qt.plugin.plugin_cycle_view import PluginCycleView
        view = PluginCycleView(
            plugin_name,
            on_close=lambda: self._tabs.close_tab("plugin_cycle"),
            on_flip=self._on_flip_plugin_rule,
        )
        self._plugin_cycle_view = view
        self._tabs.open_scoped_tab(view, self.tr("Plugin Cycle"),
                                   self._modlist_panel_stack,
                                   key="plugin_cycle")
        self._refresh_cycle_tab_data()

    def _on_flip_plugin_rule(self, owner: str, field: str, target: str):
        """Cycle-tab Flip button: move target between the owner entry's
        after/before lists, save, refresh flag + tab."""
        from Utils.userlist import flip_plugin_rule, parse_userlist, write_userlist
        ul_path = self._userlist_path()
        if ul_path is None or not ul_path.is_file():
            self._notify(self.tr("userlist.yaml not found — cannot flip rule."),
                         "warning")
            return
        data = parse_userlist(ul_path)
        if not flip_plugin_rule(data, owner, field, target):
            self._notify(self.tr("Rule {0} '{1}' {2} not found in userlist.yaml.").format(owner, field, target), "warning")
            return
        write_userlist(ul_path, data)
        other = "before" if field == "after" else "after"
        self._notify(self.tr("Flipped: {0} now '{1}' {2}").format(owner, other, target), "success")
        self._refresh_userlist_flags()
        self._refresh_cycle_tab_data()

    # ---- userlist context-menu actions (plugin_menu callbacks) -------------
    def _on_groundcover_changed(self, count: int, marked: bool):
        """Groundcover marks were saved from the Plugins panel; openmw.cfg gets them
        on the next deploy."""
        if marked:
            msg = self.tr("{0} plugin(s) marked as groundcover — Deploy to apply.")
        else:
            msg = self.tr("{0} plugin(s) no longer groundcover — Deploy to apply.")
        self._notify(msg.format(count), "info")

    def _on_userlist_add(self, plugin_name: str, row: int):
        """'Add to userlist…' — open the inline bar prefilled with the
        plugin's current load-order neighbours (Tk _add_plugin_to_userlist)."""
        if self._userlist_path() is None:
            self._notify(self.tr("No active profile — cannot edit userlist."), "warning")
            return
        rows = self._plugin_model._rows
        after_plugin = rows[row - 1].name if row > 0 else ""
        before_plugin = rows[row + 1].name if row + 1 < len(rows) else ""
        self._ul_bar.open_for(plugin_name, after_plugin, before_plugin)

    def _on_userlist_remove(self, plugin_names: list):
        """'Remove from userlist' — drop the plugins' entries and refresh."""
        from Utils.userlist import parse_userlist, remove_plugins, write_userlist
        ul_path = self._userlist_path()
        if ul_path is None:
            return
        data = parse_userlist(ul_path)
        remove_plugins(data, plugin_names)
        write_userlist(ul_path, data)
        self._notify(self.tr("Removed from userlist: {0} plugin(s)").format(len(plugin_names)),
                     "success")
        self._refresh_userlist_flags()

    # ---- Sort Plugins (LOOT) ----------------------------------------------
    def _on_refresh_plugins(self):
        """Run LOOT to refresh plugin metadata (messages/dirty/tags/requirements)
        WITHOUT reordering the load order — the ready handler skips the reorder
        step when the context is flagged as a refresh."""
        self._on_sort_plugins(refresh=True)

    def _on_sort_plugins(self, _checked=False, *, refresh=False):
        """LOOT-sort the load order on a worker thread (reuses the Tk backend
        LOOT/loot_sorter.sort_plugins). Result applied on the UI thread.

        When *refresh* is True, LOOT still evaluates the plugins (yielding the
        same metadata) but the resulting order is discarded — only the metadata
        is written and the panel reloaded."""
        from LOOT.loot_sorter import is_available, unavailable_reason
        if not is_available():
            self._notify(self.tr("LOOT library not available — cannot sort."), "warning")
            reason = unavailable_reason()
            if reason:
                from Utils.app_log import app_log
                app_log(reason)
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if not getattr(game, "loot_sort_enabled", False):
            self._notify(self.tr("LOOT sorting isn't supported for this game."), "warning")
            return
        rows = list(self._plugin_model._rows)
        if not rows:
            self._notify(self.tr("No plugins to sort."), "warning")
            return
        if self._sort_running:
            self._notify(self.tr("A sort is already running."), "info")
            return

        # Locked plugins stay put; LOOT sorts the rest. (Qt model already carries
        # vanilla rows pinned at the top, so — unlike Tk — we don't inject them.)
        locked_indices = {i: r for i, r in enumerate(rows)
                          if self._plugin_model.is_locked(i)}
        unlocked = [r for i, r in enumerate(rows) if i not in locked_indices]
        plugin_names = [r.name for r in unlocked]
        enabled_set = {r.name for r in unlocked if r.enabled}

        pdir = self._gs.profile_dir()
        userlist_path = None
        try:
            prof_ul = (pdir / "userlist.yaml") if pdir else None
            if prof_ul and prof_ul.is_file():
                userlist_path = prof_ul
            else:
                from Utils.config_paths import get_loot_game_dir
                g_ul = get_loot_game_dir(game.game_id) / "userlist.yaml"
                userlist_path = g_ul if g_ul.is_file() else None
        except Exception:
            userlist_path = None

        include_vanilla = bool(getattr(game, "plugins_include_vanilla", False))
        # Snapshot what the apply step needs (no model access mid-flight).
        self._sort_ctx = {
            "rows": rows, "locked_indices": locked_indices,
            "plugin_names": plugin_names, "include_vanilla": include_vanilla,
            "profile_dir": pdir, "game_id": game.game_id,
            "game": game, "profile": self._gs.profile,
            "refresh": refresh,
        }

        kw = dict(
            plugin_names=plugin_names, enabled_set=enabled_set,
            game_name=getattr(game, "name", self._gs.game_name),
            game_path=game.get_game_path(),
            staging_root=game.get_effective_mod_staging_path(),
            game_type_attr=getattr(game, "loot_game_type", ""),
            game_id=game.game_id,
            masterlist_url=getattr(game, "loot_masterlist_url", ""),
            masterlist_repo=getattr(game, "loot_masterlist_repo", ""),
            game_data_dir=(game.get_vanilla_plugins_path()
                           if hasattr(game, "get_vanilla_plugins_path") else None),
            userlist_path=userlist_path,
        )

        self._sort_running = True
        self._plugin_sort_btn.setEnabled(False)
        if hasattr(self, "_plugin_refresh_btn"):
            self._plugin_refresh_btn.setEnabled(False)
        if refresh:
            self._notify(
                self.tr("Refreshing LOOT metadata for {0} plugins…").format(
                    len(plugin_names)), "info")
        else:
            self._notify(
                self.tr("Running LOOT on {0} plugins…").format(
                    len(plugin_names)), "info")

        from gui_qt.worker import run_in_worker

        def sort():
            from LOOT.loot_sorter import sort_plugins
            from Utils.app_log import app_log
            # Route libloot's status lines (and, via run_in_worker's exception
            # handler, cyclic-dependency / sort-failure messages) to the visible
            # log panel — Tk did this with self._log; the Qt port had dropped it
            # to a stdout print(), so users never saw why a sort failed.
            return sort_plugins(
                log_fn=lambda m: app_log(f"[loot] {m}"), **kw)

        run_in_worker(sort, self._sort_plugins_ready, name="loot-sort")

    def _on_sort_plugins_ready(self, result):
        self._sort_running = False
        if hasattr(self, "_plugin_sort_btn"):
            self._plugin_sort_btn.setEnabled(True)
        if hasattr(self, "_plugin_refresh_btn"):
            self._plugin_refresh_btn.setEnabled(True)
        ctx = getattr(self, "_sort_ctx", None) or {}
        self._sort_ctx = None
        is_refresh = bool(ctx.get("refresh"))
        if result is None:
            self._notify(
                (self.tr("LOOT refresh failed — see log.") if is_refresh
                 else self.tr("LOOT sort failed — see log.")), "error")
            return
        for w in getattr(result, "warnings", []) or []:
            self._append_log(f"[loot] warning: {w}")

        # Persist evaluated LOOT metadata so PF_LOOT/DIRTY/TAGS light up on reload.
        try:
            from LOOT.loot_sorter import write_loot_info
            write_loot_info(ctx.get("profile_dir"), result.plugin_info,
                            result.general_messages, game_id=ctx.get("game_id", ""))
        except Exception as exc:
            self._append_log(f"[loot] could not write loot.json: {exc}")

        # Refresh mode: metadata is written, load order untouched. Just reload
        # the panel so the new flags light up, then stop.
        if is_refresh:
            self._reload_plugins()
            self._notify(self.tr("Plugin metadata refreshed."), "success")
            return

        from gui_qt.plugin.plugin_state import apply_loot_sort, save_plugins
        new_rows, moved = apply_loot_sort(
            ctx.get("rows", []), ctx.get("locked_indices", {}),
            list(result.sorted_names), ctx.get("include_vanilla", False))

        game, profile = ctx.get("game"), ctx.get("profile")
        if game is not None and profile:
            self._plugin_model._rows = new_rows
            try:
                save_plugins(game, profile, new_rows)
            except Exception as exc:
                self._notify(self.tr("Failed to write load order: {0}").format(exc), "error")
                return
        # Reload (rebuilds rows + flags) and recompute BSA conflicts (a sort
        # changes plugin order → BSA winners follow plugin load order).
        self._reload_plugins()
        self._rebuild_conflicts_async()
        if moved == 0 and not ctx.get("locked_indices"):
            self._notify(self.tr("Load order is already sorted."), "info")
        else:
            self._notify(
                (self.tr("Sorted — 1 plugin moved.") if moved == 1
                 else self.tr("Sorted — {0} plugins moved.").format(moved)),
                "success")

    # ---- Show overlapping plugins (LOOT record overlap) -------------------
    def _on_show_overlapping_plugins(self, plugin_name: str):
        """Find plugins whose records overlap with *plugin_name* and highlight
        them in the plugins list (Tk parity: plugin_panel_loot
        _show_overlapping_plugins). A full libloot load is expensive, so it
        runs on a worker thread; the result is applied on the UI thread."""
        from LOOT.loot_sorter import is_available, unavailable_reason
        if not is_available():
            self._notify(self.tr("LOOT library not available — cannot check overlap."),
                         "warning")
            reason = unavailable_reason()
            if reason:
                from Utils.app_log import app_log
                app_log(reason)
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if not getattr(game, "loot_sort_enabled", False):
            self._notify(self.tr("LOOT sorting isn't supported for this game."), "warning")
            return
        rows = list(self._plugin_model._rows)
        if not rows:
            return
        if getattr(self, "_overlap_running", False):
            self._notify(self.tr("An overlap check is already running."), "info")
            return

        # Snapshot everything the worker needs — no model access mid-flight.
        plugin_names = [r.name for r in rows]
        kw = dict(
            target_plugin=plugin_name,
            plugin_names=plugin_names,
            game_name=getattr(game, "name", self._gs.game_name),
            game_path=game.get_game_path(),
            staging_root=game.get_effective_mod_staging_path(),
            game_type_attr=getattr(game, "loot_game_type", ""),
            game_data_dir=(game.get_vanilla_plugins_path()
                           if hasattr(game, "get_vanilla_plugins_path") else None),
        )

        self._overlap_running = True
        self._notify(self.tr("Checking record overlap for {0}…").format(plugin_name), "info")

        from gui_qt.worker import run_in_worker

        def check():
            from LOOT.loot_sorter import find_overlapping_plugins
            from Utils.app_log import app_log
            overlaps = find_overlapping_plugins(
                log_fn=lambda m: app_log(f"[loot] {m}"), **kw)
            return (plugin_name, overlaps)

        # error_result carries the target name with a None payload so the apply
        # step can report the failure against the right plugin.
        run_in_worker(check, self._overlap_ready, name="loot-overlap",
                      unpack=True, error_result=(plugin_name, None))

    def _plugins_supported(self) -> bool:
        """Whether the active game uses plugins (has plugin extensions). Games
        that return an empty ``plugin_extensions`` (e.g. non-Bethesda) don't
        track plugins, so the Plugins tab shows no column header/footer."""
        game = self._gs.game
        return bool(getattr(game, "plugin_extensions", None)) if game else True

    def _apply_plugins_supported(self):
        """Hide the plugins column header + footer for games without plugins.
        Called on game/profile change (via _reload_plugins)."""
        supported = self._plugins_supported()
        # Column header: hide for plugin-less games.
        self._plugin_view.setHeaderHidden(not supported)
        # Sub-tab strip: plugin-less games hide the Plugins tab entirely and
        # land on Downloads instead. Switching back to a plugin game restores
        # the tab (and returns to it if we auto-moved off it).
        was_hidden = getattr(self, "_plugins_tab_hidden", False)
        self._plugins_tab_hidden = not supported
        self._plugin_tab_labels[0].setVisible(supported)
        if not supported and self._plugin_stack.currentIndex() == 0:
            self._select_plugin_tab(self._DOWNLOADS_TAB_IDX)
        elif (supported and was_hidden
                and self._plugin_stack.currentIndex() == self._DOWNLOADS_TAB_IDX):
            self._select_plugin_tab(0)
        # Footer (page 0 of the swap stack = the plugin tools) is hidden only
        # while the Plugins sub-tab is active; other tabs keep their own footer.
        self._refresh_plugin_footer_visibility()

    def _refresh_plugin_footer_visibility(self):
        """Show the footer stack unless we're on the Plugins tab of a game that
        doesn't use plugins."""
        stack = getattr(self, "_plugin_footer_stack", None)
        if stack is None:
            return
        on_plugins_tab = self._plugin_stack.currentIndex() == 0
        stack.setVisible(self._plugins_supported() or not on_plugins_tab)

    def _refresh_framework_banner(self):
        """Re-detect modding frameworks and update the Plugins-tab banner. Called
        on game/profile change + after each filemap rebuild (same as Tk).

        The detect reads filemap.txt (+ mod index) — ~200 ms on a 100k-file
        modlist — so it runs on a worker thread and lands via
        _framework_statuses_ready. A generation counter drops results that a
        game/profile switch (or a newer refresh) has superseded."""
        if not hasattr(self, "_framework_banner"):
            return
        self._framework_gen += 1
        gen = self._framework_gen
        # Snapshot inputs on the UI thread — GameState can be swapped under a
        # worker (see the profile-desync incident).
        game = self._gs.game
        staging = self._gs.staging_dir()
        filemap_path = (staging.parent / "filemap.txt") if staging is not None else None
        modlist_path = self._gs.modlist_path()

        from gui_qt.worker import run_in_worker

        def detect():
            from Utils.wine_proton.framework_detect import detect_frameworks
            return gen, detect_frameworks(game, filemap_path, modlist_path,
                                          rf_toggle_enabled=True)

        run_in_worker(detect, self._framework_statuses_ready,
                      name="framework-detect", unpack=True,
                      error_result=(gen, []))

    def _on_framework_statuses(self, gen: int, statuses):
        if gen != self._framework_gen or not hasattr(self, "_framework_banner"):
            return
        self._framework_banner.set_statuses(statuses)
        # Cache {label: state} for the play-bar dropdown (staged-but-not-
        # deployed script extenders are listed and deploy on Run). Keyed by
        # game+profile so a stale map from before a switch is never applied;
        # the gen guard above means the result matches the CURRENT selection.
        key = (self._gs.game.name if self._gs.game else None, self._gs.profile)
        states = {s.label: s.state for s in (statuses or [])}
        if getattr(self, "_framework_states", None) != (key, states):
            self._framework_states = (key, states)
            self._refresh_play_selector()

    # ----------------------------------------------------------------- right
    def _build_plugins(self) -> QWidget:
        return self._plugins_placeholder()

    def _plugins_placeholder(self) -> QWidget:
        from PySide6.QtWidgets import QStackedWidget

        from gui_qt.plugin.plugin_model import PluginModel
        from gui_qt.plugin.plugin_view import PluginView

        frame = QFrame()
        frame.setObjectName("PlaceholderPane")
        v = QVBoxLayout(frame)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Sub-tab strip — switches the stacked pages below.
        self._plugin_tab_names = [self.tr("Plugins"), self.tr("Mod Files"),
                                  self.tr("Text Files"), self.tr("Data"),
                                  self.tr("Downloads")]
        self._plugin_stack = QStackedWidget()

        # Page 0: the real Plugins view, with a framework-status banner above the
        # columns (one colored row per framework the game declares).
        self._plugin_model = PluginModel()
        # A plugin reorder/toggle changes BSA load order (BSAs load at their
        # plugin's position), so recompute BSA conflicts when the order is saved.
        # BSA-ONLY: a plugin toggle doesn't touch the deployed file set, so the
        # filemap/modindex, loose conflicts and the Plugins panel itself are all
        # unaffected — recompute just the BSA winners (re-reads the freshly-
        # written loadorder.txt) instead of a full filemap rebuild + modlist/
        # plugins reload. Tk parity: recompute_bsa_conflicts.
        self._plugin_model.order_changed.connect(
            self._recompute_bsa_conflicts_async)
        self._plugin_model.save_failed.connect(
            lambda msg: self._notify(msg, "error"))
        self._plugin_view = PluginView(self._plugin_model)
        from gui_qt.framework_banner import FrameworkBanner
        self._framework_banner = FrameworkBanner()
        self._plugin_stack.addWidget(self._plugin_view)
        # Page 1: the real Mod Files view.
        from gui_qt.mod_files.mod_files_view import ModFilesView
        self._mod_files_view = ModFilesView()
        self._mod_files_view.changed.connect(self._on_mod_files_changed)
        self._mod_files_view.on_open_image = self._open_image_preview_tab
        self._mod_files_view.on_open_archive = self._open_bsa_preview_tab
        self._mod_files_view.on_open_text = self._open_text_editor_tab
        self._plugin_stack.addWidget(self._mod_files_view)
        # Page 2: the real Text Files view.
        from gui_qt.text_files.text_files_view import TextFilesView
        self._text_files_view = TextFilesView()
        self._text_files_view.on_open_file = self._open_text_editor_tab
        self._plugin_stack.addWidget(self._text_files_view)
        # Page 3: the real Data view.
        from gui_qt.data_tab.data_view import DataView
        self._data_view = DataView()
        self._data_view.on_select_mod = self._on_data_select_mod
        self._plugin_stack.addWidget(self._data_view)
        # Page 4: the real Downloads view.
        from gui_qt.downloads.downloads_view import DownloadsView
        self._downloads_view = DownloadsView()
        self._downloads_view.on_install = \
            lambda paths: self._install_paths(paths, clear_archives=False)
        self._downloads_view.selection_changed.connect(
            self._update_downloads_footer)
        self._plugin_stack.addWidget(self._downloads_view)
        self._TEXT_FILES_TAB_IDX = 2
        self._DATA_TAB_IDX = 3
        self._DOWNLOADS_TAB_IDX = 4

        tabs = QHBoxLayout()
        tabs.setSpacing(2)
        # Centre the tab strip within the panel (stretch on both sides).
        tabs.addStretch(1)
        self._plugin_tab_labels = []
        for i, t in enumerate(self._plugin_tab_names):
            lbl = QLabel(t)
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.mousePressEvent = lambda _e, idx=i: self._select_plugin_tab(idx)
            tabs.addWidget(lbl)
            self._plugin_tab_labels.append(lbl)
        tabs.addStretch(1)
        # Framework-status banner ABOVE the tabs so it's visible on every
        # sub-tab (one colored row per framework the game declares).
        v.addWidget(self._framework_banner)
        v.addLayout(tabs)
        v.addWidget(self._plugin_stack, 1)
        self._select_plugin_tab(0)
        return frame

    def _select_plugin_tab(self, idx: int):
        # Plugin-less games have no Plugins tab — route to Downloads instead.
        if idx == 0 and getattr(self, "_plugins_tab_hidden", False):
            idx = getattr(self, "_DOWNLOADS_TAB_IDX", 4)
        self._plugin_stack.setCurrentIndex(idx)
        # Swap the column footer to match the active sub-tab. Footer pages:
        # 0 plugins / 1 Mod Files / 2 Data / 3 Downloads / 4 Text Files.
        fstack = getattr(self, "_plugin_footer_stack", None)
        tf_idx = getattr(self, "_TEXT_FILES_TAB_IDX", 2)
        data_idx = getattr(self, "_DATA_TAB_IDX", 3)
        dl_idx = getattr(self, "_DOWNLOADS_TAB_IDX", 4)
        if fstack is not None:
            fstack.setCurrentIndex(
                1 if idx == 1 else 2 if idx == data_idx
                else 3 if idx == dl_idx else 4 if idx == tf_idx else 0)
            # Plugin-less games hide the plugin tools footer on the Plugins tab.
            self._refresh_plugin_footer_visibility()
        # Deferred build: only (re)build a tab's contents when it's shown.
        dv = getattr(self, "_data_view", None)
        if dv is not None:
            dv.set_visible_tab(idx == data_idx)
        dlv = getattr(self, "_downloads_view", None)
        if dlv is not None:
            dlv.set_visible_tab(idx == dl_idx)
            if idx == dl_idx:
                self._update_downloads_footer()
        tfv = getattr(self, "_text_files_view", None)
        if tfv is not None:
            tfv.set_visible_tab(idx == tf_idx)
        for i, lbl in enumerate(self._plugin_tab_labels):
            sel = i == idx
            # Theme foreground (dim when unselected) so the tab strip reads on
            # the light panel too; selected tab gets the accent underline.
            col = _c(self._pal, "TEXT_MAIN" if sel else "TEXT_DIM")
            lbl.setStyleSheet(
                f"padding:4px 8px; color:{col};" + (
                    f"border-bottom:2px solid {_c(self._pal,'ACCENT')};"
                    if sel else ""))

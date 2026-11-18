"""Modlist panel: build, filters and search, reload, flags/sizes/stats, filemap and conflict rebuilds.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from gui_qt.modlist.modlist_model import COL_SIZE, ModListModel
from gui_qt.modlist.modlist_view import ModListView
from Utils.wine_proton.framework_detect import game_provides_requirement


class MainWindow:

    def _build_modlist(self) -> QWidget:
        self._modlist_model = ModListModel([])
        self._modlist_model.enabled_changed.connect(self._on_mods_enabled_changed)
        self._modlist_model.save_failed.connect(
            lambda msg: self._notify(msg, "error"))
        self._modlist_view = ModListView(self._modlist_model)
        # Search/filter hidden sets are row-indexed, so any structural change
        # (reorder/insert/remove) leaves them misaligned and must be recomputed
        # from the current entries — the view's own handler only re-applies the
        # STALE indices, so search would skip/mis-show rows until a full reload.
        # Connected AFTER the view so its cache-drop + re-span run first.
        # modelReset is handled by _reload_modlist's explicit reapply.
        for sig in (self._modlist_model.layoutChanged,
                    self._modlist_model.rowsMoved,
                    self._modlist_model.rowsInserted,
                    self._modlist_model.rowsRemoved):
            sig.connect(self._on_modlist_layout_changed)
        return self._modlist_view

    def _on_modlist_layout_changed(self, *_a):
        self._apply_modlist_search()
        self._apply_modlist_filters()

    def _on_mods_enabled_changed(self, changes):
        """Mods were toggled (checkbox / Enable-Disable all / context menu) —
        mirror the change into plugins.txt + loadorder.txt (Tk parity:
        _sync_plugins_for_toggle) and refresh the Plugins tab."""
        from Utils.plugins.plugin_sync import sync_plugins_for_mods
        try:
            wrote = sync_plugins_for_mods(
                self._gs.game, self._gs.profile_dir(), self._gs.staging_dir(),
                list(changes or []), log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] plugin sync failed: {exc}", flush=True)
            return
        # NOTE: the plugin panel is reloaded from _on_conflicts_ready (after the
        # filemap rebuild that save()→on_saved() kicked off finishes), NOT here.
        # Reloading now would read a STALE filemap — the rebuild is still in
        # flight — so a just-enabled patcher mod's light copies wouldn't be the
        # resolved winners yet (ESL flags missing), and a just-disabled mod's
        # plugins couldn't be recovered from the fresh filemap. Tk parity:
        # gui.py _on_filemap_rebuilt refreshes the plugins tab after the rebuild.
        _ = wrote

    def _build_modlist_area(self) -> QWidget:
        """Modlist column: the list + its tool footer (buttons + search) stacked
        vertically, with a collapsible filter side panel docked on the left (Tk
        parity — the filter panel pushes the column right, not an overlay)."""
        # The list + footer share one vertical column. A hidden 'new profile'
        # bar sits at the very top (row 0, Tk parity) and appears when the user
        # picks 'Add new profile…'.
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        from gui_qt.new_profile_bar import NewProfileBar
        self._new_profile_bar = NewProfileBar(
            on_create=self._on_new_profile_create,
            on_cancel=lambda: None)
        cv.addWidget(self._new_profile_bar)
        cv.addWidget(self._build_modlist(), 1)
        cv.addWidget(self._modlist_footer())

        area = QWidget()
        h = QHBoxLayout(area)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self._modlist_filter_panel = self._build_modlist_filter_panel()
        self._modlist_filter_panel.setVisible(False)
        h.addWidget(self._modlist_filter_panel)
        # The Mod Files filter panel shares this window-left slot so it opens in
        # the same place as the modlist filters (just a different filter set).
        self._mod_files_filter_panel = self._build_mod_files_filter_panel()
        self._mod_files_filter_panel.setVisible(False)
        h.addWidget(self._mod_files_filter_panel)
        # The Data filter panel shares the same window-left slot.
        self._data_filter_panel = self._build_data_filter_panel()
        self._data_filter_panel.setVisible(False)
        h.addWidget(self._data_filter_panel)
        # The Downloads filter panel shares the slot too.
        self._downloads_filter_panel = self._build_downloads_filter_panel()
        self._downloads_filter_panel.setVisible(False)
        h.addWidget(self._downloads_filter_panel)
        # The Text Files filter panel shares the slot too.
        self._text_files_filter_panel = self._build_text_files_filter_panel()
        self._text_files_filter_panel.setVisible(False)
        h.addWidget(self._text_files_filter_panel)
        # The Plugins filter panel shares the slot too.
        self._plugin_filter_panel = self._build_plugin_filter_panel()
        self._plugin_filter_panel.setVisible(False)
        h.addWidget(self._plugin_filter_panel)
        h.addWidget(col, 1)
        return area

    def _build_text_files_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(self._text_files_view.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_text_files_filter_changed)
        panel.close_requested.connect(self._toggle_text_files_filters)
        self._text_files_view.filetypes_changed.connect(
            self._sync_text_files_filter_list)
        return panel

    def _toggle_text_files_filters(self):
        panel = self._text_files_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_text_files_filter_list()
        else:
            panel.setVisible(False)

    def _on_text_files_filter_changed(self, state: dict):
        self._text_files_view.apply_filter_state(state)
        active = self._text_files_filter_panel.any_active()
        b = getattr(self, "_tf_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_text_files_filter_list(self):
        if not self._text_files_filter_panel.isVisible():
            return
        self._text_files_filter_panel.set_dynamic_items(
            "filetypes", self._text_files_view.filetype_items())

    def _build_downloads_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(self._downloads_view.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_downloads_filter_changed)
        panel.close_requested.connect(self._toggle_downloads_filters)
        self._downloads_view.filetypes_changed.connect(
            self._sync_downloads_filter_list)
        return panel

    def _toggle_downloads_filters(self):
        panel = self._downloads_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_downloads_filter_list()
        else:
            panel.setVisible(False)

    def _on_downloads_filter_changed(self, state: dict):
        self._downloads_view.apply_filter_state(state)
        active = self._downloads_filter_panel.any_active()
        b = getattr(self, "_dl_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_downloads_filter_list(self):
        if not self._downloads_filter_panel.isVisible():
            return
        self._downloads_filter_panel.set_dynamic_items(
            "filetypes", self._downloads_view.filetype_items())
        self._downloads_filter_panel.set_dynamic_items(
            "locations", self._downloads_view.location_items())

    def _build_data_filter_panel(self):
        from gui_qt.data_tab.data_view import DataView
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(DataView.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_data_filter_changed)
        panel.close_requested.connect(self._toggle_data_filters)
        self._data_view.filetypes_changed.connect(self._sync_data_filter_list)
        return panel

    def _toggle_data_filters(self):
        """Open/close the Data filter panel in the window-left slot (does NOT hide
        the plugins column — the Data tree lives there)."""
        panel = self._data_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_data_filter_list()
        else:
            panel.setVisible(False)

    def _on_data_filter_changed(self, state: dict):
        self._data_view.apply_filter_state(state)
        active = self._data_filter_panel.any_active()
        b = getattr(self, "_data_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_data_filter_list(self):
        if not self._data_filter_panel.isVisible():
            return
        self._data_filter_panel.set_dynamic_items(
            "filetypes", self._data_view.filetype_items())

    def _build_mod_files_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.mod_files.mod_files_view import ModFilesView
        panel = FilterSidePanel(ModFilesView.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_mod_files_filter_changed)
        panel.close_requested.connect(self._toggle_mod_files_filters)
        # Keep the panel's file-type list + Pack/Unpack enablement in sync.
        self._mod_files_view.filetypes_changed.connect(
            self._sync_mod_files_filter_list)
        self._mod_files_view.mod_changed.connect(
            lambda _n: self._update_mf_footer_buttons())
        return panel

    def _toggle_mod_files_filters(self):
        """Open/close the Mod Files filter panel in the window-left slot. Unlike
        the modlist filter, this does NOT hide the plugins column — the Mod Files
        tree lives there, so hiding it would hide what you're filtering."""
        panel = self._mod_files_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)  # share the slot
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_mod_files_filter_list()
        else:
            panel.setVisible(False)

    def _on_mod_files_filter_changed(self, state: dict):
        self._mod_files_view.apply_filter_state(state)
        active = self._mod_files_filter_panel.any_active()
        b = getattr(self, "_mf_filters_btn", None)
        if b is not None:
            b.setProperty("active", active)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_mod_files_filter_list(self):
        if not self._mod_files_filter_panel.isVisible():
            return
        self._mod_files_filter_panel.set_dynamic_items(
            "filetypes", self._mod_files_view.filetype_items())

    def _update_mf_footer_buttons(self):
        import Utils.archives.bsa_pack_ops as ops
        pack_btn = getattr(self, "_mf_pack_btn", None)
        unpack_btn = getattr(self, "_mf_unpack_btn", None)
        if pack_btn is None or unpack_btn is None:
            return
        mv = self._mod_files_view
        kind = ops.archive_kind_for_game(getattr(mv, "game", None))
        # Hide both buttons entirely on games we can't pack for (Tk parity).
        if kind is None:
            pack_btn.setVisible(False)
            unpack_btn.setVisible(False)
            return
        upper = kind.upper()
        pack_btn.setVisible(True)
        unpack_btn.setVisible(True)
        pack_btn.setText(self.tr("Pack {0}").format(upper))
        unpack_btn.setText(self.tr("Unpack {0}").format(upper))
        # Pack: any normal mod. Unpack: also needs a matching archive on disk.
        is_normal = mv.has_mod() and ops.is_packable_mod(getattr(mv, "_mod_name", None))
        pack_btn.setEnabled(is_normal)
        has_archive = False
        if is_normal:
            mod_dir = self._bsa_mod_dir()
            has_archive = mod_dir is not None and ops.mod_has_archive(mod_dir, kind)
        unpack_btn.setEnabled(has_archive)

    def _on_mf_expand_clicked(self):
        expanded = self._mod_files_view._toggle_expand_all()
        self._mf_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                    else self.tr("⊞ Expand all"))

    def _build_modlist_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.modlist.modlist_filter import STATUS_FILTERS

        # Filters whose backing data the Qt side doesn't build yet — shown but
        # disabled (greyed) so the panel is complete and they light up later.
        # (FOMOD/BAIN come from meta.is_fomod/is_bain; conflicts, plugins, BSA,
        #  PBR, updates, categories, file types are all wired.)
        _UNWIRED = set()
        items = [(key, label, key not in _UNWIRED)
                 for key, label in STATUS_FILTERS]
        spec = [
            {"title": "By status", "type": "checks", "items": items},
            {"title": "By category", "type": "dynamic", "id": "categories"},
            {"title": "By file type", "type": "dynamic", "id": "filetypes"},
            {"title": "By author", "type": "dynamic", "id": "authors"},
        ]
        panel = FilterSidePanel(spec, title=self.tr("Filters"))
        panel.changed.connect(self._on_modlist_filter_changed)
        panel.close_requested.connect(self._toggle_modlist_filters)
        self._modlist_filter_state: dict = {}
        self._modlist_filter_data = None
        return panel

    def _toggle_modlist_filters(self):
        """Show/hide the modlist filter side panel (the Filters footer button).

        Mirrors Tk: opening the modlist filter auto-hides the plugins panel so
        the filter takes its space; closing restores the plugins panel only if
        it was visible when we opened."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        show = not panel.isVisible()
        right = getattr(self, "_right_col", None)
        if show:
            # The Mod Files / Data filters share this slot — close them first.
            mfp = getattr(self, "_mod_files_filter_panel", None)
            if mfp is not None and mfp.isVisible():
                self._toggle_mod_files_filters()
            dfp = getattr(self, "_data_filter_panel", None)
            if dfp is not None and dfp.isVisible():
                self._toggle_data_filters()
            dlfp = getattr(self, "_downloads_filter_panel", None)
            if dlfp is not None and dlfp.isVisible():
                self._toggle_downloads_filters()
            tffp = getattr(self, "_text_files_filter_panel", None)
            if tffp is not None and tffp.isVisible():
                self._toggle_text_files_filters()
            self._filter_plugins_was_visible = bool(
                right is not None and right.isVisible())
            panel.setVisible(True)
            if right is not None and self._filter_plugins_was_visible:
                right.setVisible(False)
            self._rebuild_filter_data()
        else:
            panel.setVisible(False)
            if right is not None and getattr(
                    self, "_filter_plugins_was_visible", False):
                right.setVisible(True)
            self._filter_plugins_was_visible = False

    # ---- footer button handlers -----------------------------------------
    def _on_toggle_collapse_all(self):
        """Expand all / Collapse all separators (toggles based on current state)."""
        m = self._modlist_model
        if not m.collapsible_separator_names():
            return
        collapse = not m.any_collapsed()   # if any expanded → collapse all
        self._modlist_view.set_all_collapsed(collapse)
        self._refresh_footer_toggle_labels()

    def _on_toggle_enable_all(self):
        """Enable all / Disable all toggleable mods."""
        m = self._modlist_model
        enable = not m.all_mods_enabled()
        m.set_all_enabled(enable)
        self._refresh_footer_toggle_labels()
        self._notify(self.tr("All mods enabled") if enable else self.tr("All mods disabled"),
                     "info")

    def _refresh_footer_toggle_labels(self):
        """Keep the Expand/Collapse all + Enable/Disable all button text in sync
        with the list state (Tk parity)."""
        m = self._modlist_model
        eb = getattr(self, "_expand_all_btn", None)
        if eb is not None:
            has_seps = bool(m.collapsible_separator_names())
            eb.setText(self.tr("Expand all") if (not has_seps or m.any_collapsed())
                       else self.tr("Collapse all"))
        nb = getattr(self, "_enable_all_btn", None)
        if nb is not None:
            nb.setText(self.tr("Disable all") if m.all_mods_enabled() else self.tr("Enable all"))

    def _on_refresh_modlist(self):
        """Refresh: re-sync the mods folder, reload the modlist + plugins, and
        force a full index rescan (picks up files added/removed inside mods)."""
        from Utils.mods.modlist import sync_modlist_with_mods_folder
        self._reassert_profile_paths()
        ml = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        if ml is not None and staging is not None:
            try:
                sync_modlist_with_mods_folder(ml, staging)
            except Exception as exc:
                print(f"[gui_qt] modlist sync failed: {exc}", flush=True)
        self._reload_modlist(rescan_index=True, preserve_overlays=True)
        self._reload_plugins()
        self._refresh_footer_toggle_labels()
        self._notify(self.tr("Modlist refreshed"), "info")

    # ---- search boxes ----------------------------------------------------
    def _on_modlist_search(self, text: str):
        """Modlist search: hide rows whose mod name (or owning separator block)
        doesn't contain the query. Debounced to coalesce fast typing."""
        self._modlist_search_text = text
        t = getattr(self, "_modlist_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(120)
            t.timeout.connect(self._apply_modlist_search)
            self._modlist_search_timer = t
        t.start()

    def _modlist_search_tooltip(self) -> str:
        """Rich-text tooltip for the search icon: the available `!` filter tags
        plus the filetype/category forms. Grouped for readability."""
        rows = [
            (self.tr("Updates"),        "!update"),
            (self.tr("Winning conflicts"),         "!winning"),
            (self.tr("Losing conflicts"),          "!losing"),
            (self.tr("Winning & losing"),          "!partial"),
            (self.tr("Fully conflicted"),          "!full"),
            (self.tr("FOMOD installs"),            "!fomod"),
            (self.tr("BAIN installs"),             "!bain"),
            (self.tr("Missing requirements"),      "!missing"),
            (self.tr("Has notes"),                 "!notes"),
            (self.tr("Has plugins"),               "!plugins"),
            (self.tr("Has BSA/BA2 archives"),      "!bsa"),
            (self.tr("PGPatcher textures"),        "!pbr"),
            (self.tr("Enabled / disabled"),        "!enabled · !disabled"),
            (self.tr("By file type"),              "!.dds"),
            (self.tr("By category"),               "!patches"),
        ]
        items = "".join(
            f"<tr><td style='padding-right:10px'><code>{tag}</code></td>"
            f"<td>{label}</td></tr>"
            for label, tag in rows)
        head = self.tr("Filter the modlist with search tags "
                       "(combine them, and with text):")
        return f"<b>{head}</b><table>{items}</table>"

    def _modlist_token_search_active(self) -> bool:
        """True when the search box holds a `!token` filter — those key off the
        live FilterData, so callers must rebuild/reapply it like a panel filter."""
        return "!" in (getattr(self, "_modlist_search_text", "") or "")

    def _apply_modlist_search(self):
        from gui_qt.modlist.modlist_filter import search_hidden_rows
        text = getattr(self, "_modlist_search_text", "")
        entries = self._modlist_model._entries
        active = bool((text or "").strip())
        data = getattr(self, "_modlist_filter_data", None)
        self._modlist_view.set_search_hidden(
            search_hidden_rows(entries, text, data), active=active)

    def _on_modlist_filter_changed(self, state: dict):
        self._modlist_filter_state = state
        self._apply_modlist_filters()
        self._update_filters_btn_active()

    def _quick_modlist_filter_state(self, key: str) -> int:
        """Read the current tri-state for a status filter (for the column-menu
        quick-filter check marks). Reads the panel so it reflects panel edits."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is not None:
            return panel.check_state(key)
        return (getattr(self, "_modlist_filter_state", {}) or {}).get(key, 0)

    def _on_quick_modlist_filter(self, key: str, state: int):
        """Apply a quick filter chosen from the column menu by driving the
        Filters panel checkbox, so the panel and menu stay in sync and the
        normal filter pipeline (footer-button tint included) runs."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is not None:
            panel.set_check(key, state)   # emits changed -> _on_modlist_filter_changed
        else:   # panel not built yet — fall back to the state dict directly
            self._modlist_filter_state[key] = state
            self._apply_modlist_filters()
            self._update_filters_btn_active()

    def _apply_modlist_filters(self):
        from gui_qt.modlist.modlist_filter import compute_hidden_rows
        state = getattr(self, "_modlist_filter_state", {}) or {}
        # Flatten the column sort across separator groups while separators are
        # hidden (must run before reading _entries — it may re-derive the sort).
        self._modlist_model.set_separators_hidden(
            state.get("filter_hide_separators") == 1)
        data = getattr(self, "_modlist_filter_data", None)
        if data is None:
            self._modlist_view.set_filter_hidden(set())
            return
        entries = self._modlist_model._entries
        hide = compute_hidden_rows(entries, state, data)
        self._modlist_view.set_filter_hidden(hide)

    def _update_filters_btn_active(self):
        """Tint the Filters footer button when any filter is active (Tk parity)."""
        btn = getattr(self, "_modlist_filters_btn", None)
        panel = getattr(self, "_modlist_filter_panel", None)
        if btn is None or panel is None:
            return
        btn.setProperty("active", panel.any_active())
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _rebuild_filter_data(self):
        """(Re)build FilterData: the disk-derived parts (modindex read + BSA /
        plugin folder walks) run on a worker; _on_filter_data_ready assembles
        the FilterData and updates the panel on the UI thread. Runs after every
        conflict rebuild, so the scans must not block the UI."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        staging = self._gs.staging_dir()
        staging_parent = staging.parent if staging is not None else None
        plugin_exts = getattr(self._gs.game, "plugin_extensions", None)
        self._filter_data_gen += 1
        gen = self._filter_data_gen

        def worker():
            payload = None
            if staging_parent is not None:
                from gui_qt.modlist.modlist_filter import (
                    build_index_data,
                    build_mods_with_bsa,
                    build_mods_with_plugins,
                )
                try:
                    counts, mod_ft, pbr = build_index_data(staging_parent)
                    payload = {
                        "filetype_counts": counts,
                        "mod_filetypes": mod_ft,
                        "mods_with_pbr": pbr,
                        "mods_with_bsa": build_mods_with_bsa(staging_parent),
                        "mods_with_plugins": build_mods_with_plugins(
                            staging_parent, plugin_exts),
                    }
                except Exception as exc:
                    print(f"[gui_qt] filter data scan failed: {exc}", flush=True)
            self._filter_data_ready.emit(gen, payload)

        threading.Thread(target=worker, daemon=True,
                         name="filter-data").start()

    def _on_filter_data_ready(self, gen: int, payload):
        """UI thread: merge the worker's disk-derived sets with the in-memory
        meta/conflict state into FilterData, repopulate the panel, reapply."""
        if gen != self._filter_data_gen:
            return   # superseded by a newer rebuild
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        from gui_qt.modlist.modlist_filter import FilterData
        cd = getattr(self, "_conflict_data", None)

        data = FilterData()
        if cd is not None:
            data.conflict_codes = self._loose_backend_codes(cd)
            data.bsa_conflict_codes = self._bsa_backend_codes(cd)
        data.mods_with_updates = set(getattr(self, "_mod_updates", set()))
        data.missing_reqs = set(getattr(self, "_mod_missing_reqs", set()))
        data.ignored_missing_reqs = set(getattr(self, "_ignored_missing_reqs", frozenset()))
        data.category_names = dict(getattr(self, "_mod_categories", {}))
        data.author_names = dict(getattr(self, "_mod_authors", {}))
        data.fomod_mods = set(getattr(self, "_mod_fomod", set()))
        data.bain_mods = set(getattr(self, "_mod_bain", set()))
        data.modified_mf_mods = self._build_modified_mf_mods()
        data.disabled_plugins_mods = self._build_disabled_plugins_mods()
        data.notes_mods = {m for m, note in self._read_mod_notes().items()
                           if (note or "").strip()}
        # Overlay the "modified in Mod Files" eye flag in the modlist Flags column.
        self._modlist_model.set_modified_mf(data.modified_mf_mods)
        if payload:
            data.filetype_counts = payload["filetype_counts"]
            data.mod_filetypes = payload["mod_filetypes"]
            data.mods_with_pbr = payload["mods_with_pbr"]
            data.mods_with_bsa = payload["mods_with_bsa"]
            data.mods_with_plugins = payload["mods_with_plugins"]
        self._modlist_filter_data = data

        # Repopulate dynamic lists.
        cats = sorted({(c or "") for c in data.category_names.values()} | {""},
                      key=lambda c: ("(Uncategorized)" if c == "" else c).lower())
        panel.set_dynamic_items("categories", [
            (c, "(Uncategorized)" if c == "" else c, None) for c in cats])
        # Authors (Nexus uploader). Only offer names we actually know — an
        # "(Unknown)" bucket for un-stamped mods would just match everything not
        # yet looked up, which isn't a useful filter.
        auths = sorted({a for a in data.author_names.values() if a},
                       key=str.lower)
        panel.set_dynamic_items("authors", [(a, a, None) for a in auths])
        fts = sorted(data.filetype_counts.items(), key=lambda kv: kv[0])
        panel.set_dynamic_items("filetypes", [
            (ext, ext, count) for ext, count in fts])

        # Relabel / re-enable game-specific filters, then reapply.
        self._refresh_filter_game_specific()
        self._apply_modlist_filters()
        # Token searches (e.g. "!update") depend on the FilterData we just
        # (re)built — re-run so results reflect fresh conflict/update data.
        if self._modlist_token_search_active():
            self._apply_modlist_search()

    def _build_modified_mf_mods(self) -> set:
        """Mods with Mod Files tab modifications — any excluded file OR a strip
        prefix (Tk `_mod_is_modified_in_mf`)."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return set()
        out: set[str] = set()
        try:
            from Utils.profile.profile_state import (
                read_excluded_mod_files,
                read_mod_strip_prefixes,
            )
            for mod, keys in (read_excluded_mod_files(pdir, None) or {}).items():
                if keys:
                    out.add(mod)
            for mod, prefixes in (read_mod_strip_prefixes(pdir, None) or {}).items():
                if any(p for p in prefixes):
                    out.add(mod)
        except Exception:
            pass
        return out

    def _build_rerun_fomod_mods(self) -> set:
        """Mods that should show the rerun-FOMOD flag. Two conditions, both read
        from meta.ini for FOMOD mods only (read_meta is mtime-cached, so cheap)
        and evaluated against the currently-enabled plugins:

          • `fomodPendingDeps` (UNSELECTED options' deps): flag when an AND-clause
            becomes FULLY present — a patch you skipped is now relevant.
          • `fomodActiveDeps` (SELECTED options' deps): flag when an AND-clause is
            NO LONGER fully present — a patch you installed is now orphaned (its
            required mod was removed/disabled).

        See FLAG_RERUN_FOMOD."""
        if not hasattr(self, "_modlist_model") or not hasattr(self, "_plugin_model"):
            return set()
        staging = self._gs.staging_dir()
        if staging is None:
            return set()
        try:
            enabled = self._plugin_model.enabled_lower()
        except Exception:
            return set()
        # Narrow to FOMOD-installed mods when we know them (avoids reading meta for
        # every mod); fall back to all mods if the set isn't populated yet.
        candidates = getattr(self, "_mod_fomod", None) or self._modlist_model.mod_names()
        # A DISABLED mod isn't deployed, so "rerun to fix it" is meaningless — and
        # its own plugins leave the load order when disabled, which would break its
        # self-referential fomodActiveDeps and falsely raise the flag. Skip them.
        try:
            enabled_mods = self._modlist_model.enabled_mod_names()
            candidates = [m for m in candidates if m in enabled_mods]
        except Exception:
            pass
        out: set[str] = set()
        try:
            from Nexus.nexus_meta import read_meta
        except Exception:
            return set()

        # Clause delimiters — imported so the encode (fomod_installer) and decode
        # (here) can never drift. They're all filename-illegal chars, so plugin
        # names containing "+", "!", "&" etc. don't collide.
        from Utils.installers.fomod_installer import (
            FLAG_ABSENT,
            FLAG_AND_SEP,
            FLAG_OPT_SEP,
            FLAG_OR_SEP,
        )

        def _member_holds(m: str) -> bool:
            # ">name" = the plugin must be ABSENT (a state="Missing" gate);
            # "name" = the plugin must be present + enabled.
            if m.startswith(FLAG_ABSENT):
                return m[len(FLAG_ABSENT):] not in enabled
            return m in enabled

        def _and_clause(clause: str):
            # An AND-clause → its lowercase members.
            return [m.strip().lower() for m in clause.split(FLAG_AND_SEP)
                    if m.strip()]

        def _clause_holds(members) -> bool:
            return bool(members) and all(_member_holds(m) for m in members)

        def _option_conditions(raw: str):
            # Each option-group is ONE option's condition, an OR-of-ANDs. Yield the
            # list of member-lists (the OR alternatives).
            for cond in (raw or "").split(FLAG_OPT_SEP):
                alts = [_and_clause(c) for c in cond.split(FLAG_OR_SEP)]
                alts = [a for a in alts if a]
                if alts:
                    yield alts

        def _option_met(alts) -> bool:
            # OR: the option's condition holds if ANY alternative clause holds.
            return any(_clause_holds(a) for a in alts)

        def _has_present_member(alts) -> bool:
            return any(not m.startswith(FLAG_ABSENT) for a in alts for m in a)

        for name in candidates:
            try:
                meta = read_meta(staging / name / "meta.ini")
            except Exception:
                continue
            # Pending: an UNSELECTED option's condition is now satisfiable → rerun
            # to pick up the newly-relevant patch. Require at least one PRESENT
            # member across the option (a non-"!" literal) so a condition that only
            # needs something ABSENT — true almost always — doesn't fire constantly.
            if any(_option_met(alts) and _has_present_member(alts)
                   for alts in _option_conditions(
                       getattr(meta, "fomod_pending_deps", ""))):
                out.add(name)
                continue
            # Active: a SELECTED option's condition is no longer satisfied (NONE of
            # its OR alternatives hold) → rerun to drop the now-orphaned patch. For
            # an OR option, having ONE of its plugins is still valid — don't fire.
            if any(not _option_met(alts)
                   for alts in _option_conditions(
                       getattr(meta, "fomod_active_deps", ""))):
                out.add(name)
        return out

    def _refresh_rerun_fomod_flags(self) -> None:
        """Recompute + push the rerun-FOMOD overlay onto the modlist model."""
        if hasattr(self, "_modlist_model"):
            self._modlist_model.set_rerun_fomod_mods(self._build_rerun_fomod_mods())

    def _refresh_filter_game_specific(self):
        """Relabel BSA→BA2 + enable/disable PGPatcher per the active game."""
        panel = getattr(self, "_modlist_filter_panel", None)
        g = self._gs.game
        if panel is None:
            return
        archive_exts = getattr(g, "archive_extensions", None) if g else None
        if archive_exts and ".ba2" in archive_exts:
            panel.set_check_label("filter_has_bsa", self.tr("Mods with BA2 archives"))
        elif archive_exts and ".pak" in archive_exts:
            panel.set_check_label("filter_has_bsa", self.tr("Mods with PAK archives"))
        else:
            panel.set_check_label("filter_has_bsa", self.tr("Mods with BSA archives"))
        # PGPatcher (PBR) is Skyrim SE only.
        is_sse = bool(g and getattr(g, "nexus_game_domain", "")
                      == "skyrimspecialedition")
        panel.set_check_enabled("filter_has_pbr", is_sse)

    def _loose_backend_codes(self, cd) -> dict:
        """Map the display loose_codes back to backend CONFLICT_* codes the
        engine filters on (DISP 1/-1/2/3 → WINS/LOSES/PARTIAL/FULL)."""
        from Utils.filemap import (
            CONFLICT_FULL,
            CONFLICT_LOSES,
            CONFLICT_PARTIAL,
            CONFLICT_WINS,
        )
        m = {1: CONFLICT_WINS, -1: CONFLICT_LOSES,
             2: CONFLICT_PARTIAL, 3: CONFLICT_FULL}
        return {n: m[c] for n, c in (cd.loose_codes or {}).items() if c in m}

    def _reassert_profile_paths(self):
        """Force game._active_profile_dir back in sync with GameState before
        resolving profile-derived paths (modlist/plugins/staging). Background
        workers (deploy pipeline, collection install/cancel-cleanup) swap the
        game object's active dir and can leave it stale or None — after which
        get_effective_mod_staging_path() silently falls back to the SHARED
        mods folder for profile-specific-mods profiles, and reload/sync/prune
        paths read (and destructively write) against the wrong profile.
        Skipped while a worker that legitimately owns that state is running —
        it re-establishes the right dir itself when it finishes. (getattr:
        the first _reload_modlist runs before the collection state is set.)"""
        if (getattr(self, "_deploy_running", False)
                or getattr(self, "_col_install_running", False)):
            return
        self._gs.reassert_active_profile()

    def _reload_modlist(self, rescan_index: bool = False,
                        preserve_overlays: bool = False):
        """Load the active game/profile's modlist + metadata into the model.
        rescan_index=True forces the conflict rebuild to rescan the index from
        disk (Refresh button).

        preserve_overlays=True keeps the current flag/conflict icons on screen
        until the async re-read lands (Refresh of the SAME game/profile — the
        overlays are keyed by mod name so they still render correctly). Leave it
        False for a game/profile switch, where the old overlays are stale and
        must be cleared immediately."""
        from gui_qt.modlist.modlist_data import read_meta_for_entries
        from Utils.mods.modlist import read_modlist
        from Utils.perftrace import span

        self._reassert_profile_paths()

        ml_path = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        with span("reload_modlist.read_modlist"):
            entries = (read_modlist(ml_path)
                       if (ml_path and ml_path.is_file()) else [])
        # Non-empty ⇒ this reload ends in a conflict rebuild (see bottom),
        # whose completion reloads the plugin panel. The profile-switch path
        # reads this to skip its own (redundant) immediate plugin reload.
        self._reload_had_entries = bool(entries)

        # Preserve the Mod Files tab's shown mod across a same-context refresh
        # (install/toggle/deploy/Refresh of the SAME game+profile): set_entries
        # clears the modlist selection, which would otherwise blank the tab. On a
        # game/profile SWITCH the shown mod may not exist, so we drop it.
        prev_context = (self._gs.game_name, self._gs.profile_dir())
        keep_mod_files = None
        if prev_context == getattr(self, "_modlist_context", None):
            mfv = getattr(self, "_mod_files_view", None)
            if mfv is not None and mfv.has_mod():
                keep_mod_files = mfv._mod_name
        self._modlist_context = prev_context

        self._mod_categories: dict[str, str] = {}
        self._mod_authors: dict[str, str] = {}
        self._mod_updates: set[str] = set()
        self._mod_fomod: set[str] = set()
        self._mod_bain: set[str] = set()
        self._mod_missing_reqs: set[str] = set()
        self._ignored_missing_reqs: frozenset[str] = frozenset()
        if entries and staging is not None:
            pdir = self._gs.profile_dir()
            if pdir is not None:
                try:
                    from Utils.profile.profile_state import (
                        read_ignored_missing_requirements,
                    )
                    self._ignored_missing_reqs = frozenset(
                        read_ignored_missing_requirements(pdir))
                except Exception:
                    self._ignored_missing_reqs = frozenset()

        # Entries go up immediately with blank meta so a game/profile switch
        # never blocks on disk; the per-mod meta.ini reads (one file per mod)
        # run on a worker → _modlist_meta_ready fills the columns/flags in.
        # The gen bump also drops a stale in-flight read on switch.
        self._modlist_meta_gen += 1
        meta_gen = self._modlist_meta_gen
        if not preserve_overlays:
            self._modlist_model._versions = {}
            self._modlist_model._installed = {}
            self._modlist_model._categories = {}
        with span("reload_modlist.set_entries"):
            self._modlist_model.set_entries(entries)
        if not preserve_overlays:
            self._modlist_model.set_flags({})
        with span("reload_modlist.read_notes"):
            self._modlist_model.set_notes(self._read_mod_notes())
        # (The meta worker itself starts at the END of this reload — see
        # below — so its disk work doesn't contend with the sync UI section.)
        if not preserve_overlays:
            self._modlist_model.set_conflicts({}, {})   # clear stale; recomputed async
        # Persist edits back to this modlist; rebuild conflicts after each save
        # (pure reorders that can't change the filemap skip the rebuild).
        self._modlist_model.modlist_path = ml_path
        self._modlist_model.on_saved = self._on_modlist_saved
        self._modlist_view.staging_dir = staging
        self._modlist_view.profile_dir = self._gs.profile_dir()
        self._modlist_view.game = self._gs.game
        # Right-click "Check Updates" reaches the window through this callback.
        self._modlist_view.on_check_updates = self._on_check_updates
        # Change Version: right-click item + clicking the update flag icon.
        self._modlist_view.on_change_version = self._open_change_version_tab
        # Change Version (mod.io): right-click item + clicking the mod.io
        # update flag icon.
        self._modlist_view.on_change_version_modio = self._open_change_version_modio_tab
        self._modlist_view.on_bundle_options = self._open_bundle_tab
        self._modlist_view.on_flag_clicked = self._on_modlist_flag_clicked
        # Missing Requirements: right-click item + clicking the ⚠ flag icon.
        self._modlist_view.on_missing_reqs = self._open_missing_reqs_tab
        self._modlist_view.ignored_missing_reqs = self._ignored_missing_reqs
        # View Requirements: right-click item (offline, reads meta.ini data).
        self._modlist_view.on_view_requirements = self._open_view_requirements_tab
        # Quick Update: right-click on update-flagged mods (premium direct DL).
        self._modlist_view.on_quick_update = self._quick_update_mods
        # Quick Update (mod.io): right-click on mod.io-update-flagged mods.
        self._modlist_view.on_quick_update_modio = self._quick_update_modio_mods
        # Reinstall: right-click item(s) whose install archive is still on disk.
        self._modlist_view.on_reinstall = self._reinstall_mods
        # Show Conflicts: right-click item.
        self._modlist_view.on_show_conflicts = self._open_show_conflicts_tab
        # Mod removal strips plugins from plugins.txt/loadorder.txt — reload the
        # plugin panel so the removed plugins disappear without a manual refresh.
        self._modlist_view.on_mods_removed = self._on_mods_removed
        # Root-Folder toggle wrote meta.ini → refresh the root flag immediately,
        # then rescan those mods + rebuild filemap (the index caches strip-applied
        # vs verbatim paths, so it's now stale; the rebuild also refreshes the
        # filemap-derived root-rule/pre-RTX overlays).
        self._modlist_view.on_root_folder_changed = \
            lambda names: (self._refresh_modlist_flags(names),
                           self._rebuild_conflicts_async(rescan_index=True))
        # Endorse/Abstain: needs the shared Nexus API + a flag refresh.
        self._modlist_view.on_endorse = self._on_modlist_endorse
        # Like/Unlike (mod.io): needs the mod.io OAuth token + a flag refresh.
        self._modlist_view.on_modio_like = self._on_modlist_modio_like
        # Track: needs the shared Nexus API (no meta.ini flag to persist).
        self._modlist_view.on_track = self._on_modlist_track
        # Collapsing from the context menu (or a separator click) changes what
        # the footer's Expand all / Collapse all toggle should say next.
        self._modlist_view.on_collapse_changed = self._refresh_footer_toggle_labels
        # A saved note may change the note flag — light flag-only refresh.
        self._modlist_view.on_notes_changed = self._refresh_modlist_flags
        # Copy/Move to profile: worker copy + collision overlay + (move) remove.
        self._modlist_view.on_copy_to_profile = self._copy_mods_to_profile
        self._modlist_view.on_copy_separators_to_profile = self._copy_separators_to_profile
        # Rename (context menu): folder + modindex + per-mod state migration.
        self._modlist_view.on_rename_mod = self._rename_mod_on_disk
        # "Fetch name from Nexus" button inside the rename dialog.
        self._modlist_view.mod_has_nexus_id = self._mod_has_nexus_id
        self._modlist_view.on_fetch_nexus_name = self._fetch_nexus_name
        # Separator settings (colour + deploy override): open the scoped tab;
        # rename/remove migrate/drop the stored colour + deploy entries.
        self._modlist_view.on_separator_settings = self._open_sep_settings_tab
        self._modlist_view.on_separator_renamed = self._on_separator_renamed
        self._modlist_view.on_separators_removed = self._on_separators_removed
        # Enabling the Size column scans mod folder sizes on demand (Tk parity:
        # only walk the disk when Size is actually shown).
        self._modlist_view.on_sizes_requested = self._apply_modlist_sizes
        self._modlist_view.on_quick_filter = self._on_quick_modlist_filter
        self._modlist_view.quick_filter_state = self._quick_modlist_filter_state
        self._modlist_model._sizes = {}
        if not self._modlist_view.isColumnHidden(COL_SIZE):
            self._apply_modlist_sizes()
        with span("reload_modlist.load_separator_state"):
            self._modlist_view.load_separator_state()
        self._modlist_view.load_mod_lock_state()
        # Point the Mod Files tab at this game/profile (index next to filemap).
        if hasattr(self, "_mod_files_view"):
            idx = (staging.parent / "modindex.bin") if staging is not None else None
            self._mod_files_view.configure(
                self._gs.game, self._gs.profile_dir(), idx)
            # Re-show the previously selected mod on a same-context refresh (it
            # updates against the freshly-scanned files); blank otherwise. The
            # Overwrite / Root Folder boundary rows are synthetic (not in the raw
            # modlist) but always present, so they survive too.
            from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME
            still_present = keep_mod_files is not None and (
                keep_mod_files in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
                or any(e.name == keep_mod_files for e in entries))
            self._mod_files_view.show_mod(
                keep_mod_files if still_present else None)
        # Point the Data tab at this game/profile (filemap.txt + modindex.bin).
        if hasattr(self, "_data_view"):
            fm = (staging.parent / "filemap.txt") if staging is not None else None
            idx2 = (staging.parent / "modindex.bin") if staging is not None else None
            self._data_view.configure(
                self._gs.game, self._gs.profile_dir(), fm, idx2)
        # Point the Downloads tab at this game (game-name getter for cache dir).
        if hasattr(self, "_downloads_view"):
            self._downloads_view.configure(
                self._gs.game, lambda: self._gs.game_name)
        # Point the Text Files tab at this game/profile.
        if hasattr(self, "_text_files_view"):
            fm3 = (staging.parent / "filemap.txt") if staging is not None else None
            self._text_files_view.configure(
                self._gs.game, self._gs.profile_dir(), fm3, staging)
        self._refresh_footer_toggle_labels()
        # Re-apply an active search + filters against the fresh row indices.
        # set_entries fired modelReset, which dropped the view's applied-hidden
        # cache but left the OLD filter-hidden row indices in place — without
        # this reapply they'd hide the wrong (or all) rows after an install,
        # blanking the list until a manual Refresh. compute_hidden_rows re-indexes
        # off the current entry list; the async conflict/filter-data rebuild below
        # then reapplies once more with the freshly-scanned per-mod data.
        with span("reload_modlist.search+filters"):
            self._apply_modlist_search()
            self._apply_modlist_filters()
        self._refresh_modlist_stats()
        print(f"[gui_qt] modlist: {ml_path} ({len(entries)} entries)")

        # The Downloads tab reflects the active profile's STAGING folder (which
        # changes with the profile), so refresh it on every reload — even for an
        # empty profile, where the conflict rebuild below is skipped.
        if hasattr(self, "_data_view"):
            self._data_view.mark_dirty()
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        if hasattr(self, "_text_files_view"):
            self._text_files_view.mark_dirty()
        # The Nexus browser (if open) shows Install/Reinstall per the active
        # profile's installed mods — refresh on every modlist reload (covers
        # profile/game change AND post-install).
        nv = getattr(self, "_nexus_view", None)
        if nv is not None:
            nv._game = self._gs.game
            nv.refresh_installed()

        # Meta read + conflict rebuild, SEQUENCED. Both cold-read the same
        # per-mod meta.ini files (the meta columns here, the filemap's
        # root-flag collect inside the conflict build); run concurrently they
        # each parse ~the whole set and stretch each other under the GIL
        # (measured 4.0s + 3.1s concurrent vs ~0.4s + ~0.05s sequenced — the
        # second reader hits the read_meta mtime cache). So: meta worker
        # first, and _on_modlist_meta_ready chains the conflict rebuild.
        meta_started = False
        if entries and staging is not None:
            ignored = self._ignored_missing_reqs
            pdir_meta = self._gs.profile_dir()
            is_bg3 = (getattr(self._gs.game, "game_id", "") == "baldurs_gate_3")
            is_tw3 = (getattr(self._gs.game, "game_id", "") == "witcher_3")
            game_meta = self._gs.game
            meta_entries = list(entries)

            def meta_worker():
                try:
                    collection_order_mods = frozenset()
                    if is_tw3 and pdir_meta is not None:
                        try:
                            from Utils.mods.tw3_load_index import (
                                collection_governed_mod_names,
                            )
                            collection_order_mods = frozenset(
                                collection_governed_mod_names(game_meta, pdir_meta))
                        except Exception as exc:
                            print(f"[gui_qt] TW3 collection-order scan failed: {exc}", flush=True)
                    with span("modlist.meta_worker(read_meta)"):
                        payload = read_meta_for_entries(
                            meta_entries, staging, ignored,
                            profile_dir=pdir_meta, is_bg3=is_bg3,
                            collection_order_mods=collection_order_mods,
                            provided=game_provides_requirement(game_meta))
                except Exception as exc:
                    print(f"[gui_qt] meta read failed: {exc}", flush=True)
                    payload = None   # still emit — the conflict rebuild chains
                self._modlist_meta_ready.emit(meta_gen, payload)

            self._conflicts_after_meta = (meta_gen, rescan_index)
            threading.Thread(target=meta_worker, daemon=True,
                             name="modlist-meta").start()
            meta_started = True

        if entries and not meta_started:
            # No meta worker (staging unresolved) → kick the rebuild directly.
            self._rebuild_conflicts_async(rescan_index=rescan_index)
        elif not entries and rescan_index:
            # Empty modlist but an explicit rescan was requested (Refresh /
            # install / toggle). The conflict rebuild is normally skipped when
            # there are no mods (nothing to conflict), but the [Overwrite]
            # pseudo-mod is INDEPENDENT of the real modlist: files can sit in
            # overwrite/ (and in modindex.bin's [Overwrite] entry) with zero
            # enabled mods. Skipping the rebuild here left a stale [Overwrite]
            # index entry + stale filemap.txt that no Refresh could clear once
            # the modlist was empty — the file kept showing in the Data tab and
            # kept deploying (as a dangling symlink). Run the rebuild so the
            # index and filemap are reconciled against the folder; it also
            # writes an empty filemap when the last mod is removed.
            # _on_conflicts_ready sets _switch_conflicts_done when the build
            # lands, so the switch-marker bookkeeping below is still covered.
            self._rebuild_conflicts_async(rescan_index=rescan_index)
        elif not entries and getattr(self, "_switch_t0", None) is not None:
            # Empty profile, no rescan (plain profile switch) → no conflict
            # rebuild will follow, so the first plugin pass IS the final one;
            # without this the switch marks would leak into the next unrelated
            # plugin reload.
            self._switch_conflicts_done = True

    def _on_modlist_meta_ready(self, gen, payload):
        """UI thread: apply the worker-read per-mod meta (see _reload_modlist).
        *payload* is None when the read failed — the apply is skipped but the
        chained conflict rebuild below still runs (it must not be lost)."""
        if gen != self._modlist_meta_gen:
            return   # superseded — the game/profile switched mid-read
        self._mark_since_switch("switch→modlist_meta_applied")  # i18n: skip — perftrace marker label
        from Utils.perftrace import span
        # Chained conflict/filemap rebuild (see _reload_modlist): kicked here,
        # AFTER the meta read, so its root-flag collect hits the warm read_meta
        # cache instead of racing the worker over the same 400+ ini files.
        pend = getattr(self, "_conflicts_after_meta", None)
        if pend is not None and pend[0] == gen:
            self._conflicts_after_meta = None
            self._rebuild_conflicts_async(rescan_index=pend[1])
        if payload is None:
            return
        (versions, installed, flags, categories, updates,
         fomod, bain, missing_reqs, descriptions, authors) = payload
        self._mod_categories = categories
        self._mod_authors = authors
        self._mod_updates = updates
        self._mod_fomod = fomod
        self._mod_bain = bain
        self._mod_missing_reqs = missing_reqs
        with span("on_modlist_meta_ready(apply)"):
            self._modlist_model.set_meta(versions, installed, categories,
                                         descriptions, authors)
            self._modlist_model.set_flags(flags)
        # Now that _mod_fomod + meta are current, refresh the rerun-FOMOD overlay
        # (picks up a just-installed/updated mod's pending deps without waiting for
        # the next plugin reload).
        self._refresh_rerun_fomod_flags()
        # Prune any installed requirements from an open Missing Requirements panel
        # (this is the path the panel's own Install button lands on).
        view = getattr(self, "_missing_reqs_view", None)
        if view is not None:
            try:
                view.prune_installed(self._installed_mod_ids())
            except Exception:
                pass
        # The meta-derived filter inputs (updates / categories / fomod / bain /
        # missing-reqs) were just refreshed on this thread. _reload_modlist
        # cleared them to empty sets before the worker ran, so any active filter
        # that keys off them (e.g. "mods with updates") was reapplied against the
        # stale FilterData and blanked the list until Refresh. Rebuild the filter
        # data now so _on_filter_data_ready merges the fresh sets and reapplies —
        # only when a filter is actually active (avoids needless disk scans).
        # A token search (e.g. "!updates") keys off the SAME meta-derived sets,
        # so it must rebuild too — otherwise updating a mod that a token matched
        # reapplies "!updates" against the just-cleared sets and blanks the list.
        panel = getattr(self, "_modlist_filter_panel", None)
        panel_active = panel is not None and panel.any_active()
        if panel_active or self._modlist_token_search_active():
            self._rebuild_filter_data()

    def _apply_modlist_sizes(self):
        """Scan mod folder sizes and push them to the model. Called on reload
        when the Size column is visible, and when the user enables Size from the
        column menu (the disk walk is skipped while Size stays hidden).

        The walk covers every mod folder, so it runs on a daemon worker —
        a generation counter drops a stale result (profile switched or a
        newer scan started mid-walk)."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        self._sizes_gen = getattr(self, "_sizes_gen", 0) + 1
        gen = self._sizes_gen
        # Snapshot the entry list — the user can reorder/rename while the
        # worker walks the disk.
        entries = list(self._modlist_model.natural_entries())

        from gui_qt.worker import NO_EMIT, run_in_worker

        def scan():
            from gui_qt.modlist.modlist_data import compute_sizes
            sizes, size_bytes = compute_sizes(entries, staging)
            return gen, sizes, size_bytes

        run_in_worker(scan, self._sizes_ready, name="modlist-sizes",
                      unpack=True, error_result=NO_EMIT)

    def _on_sizes_ready(self, gen, sizes, size_bytes):
        if gen != getattr(self, "_sizes_gen", 0):
            return   # superseded — a newer scan is in flight
        self._modlist_model.set_sizes(sizes, size_bytes)

    def _refresh_modlist_stats(self):
        """Update the modlist footer count label: enabled / total mod counts.
        Both come straight from the model, so this is instant."""
        lbl = getattr(self, "_modlist_count", None)
        if lbl is None:
            return
        entries = [self._modlist_model.entry(r)
                   for r in range(self._modlist_model.rowCount())]
        mods = [e for e in entries if not e.is_separator]
        enabled = sum(1 for e in mods if e.enabled)
        lbl.setText(f"{enabled} / {len(mods)}")
        lbl.setToolTip(self.tr("{0} enabled of {1} mods").format(enabled, len(mods)))

    def _installed_mod_ids(self) -> set[int]:
        """The set of Nexus mod ids currently installed in the active profile's
        staging folder (read from each mod's meta.ini). Used to prune the
        Missing Requirements panel once a requirement is installed."""
        ids: set[int] = set()
        staging = self._gs.staging_dir()
        if staging is None:
            return ids
        from Nexus.nexus_meta import read_meta
        for r in range(self._modlist_model.rowCount()):
            e = self._modlist_model.entry(r)
            if e is None or e.is_separator:
                continue
            meta_path = staging / e.name / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                mid = int(getattr(read_meta(meta_path), "mod_id", 0) or 0)
            except Exception:
                continue
            if mid > 0:
                ids.add(mid)
        return ids

    def _on_mods_removed(self):
        """A mod was fully removed (single or multi right-click Remove): reload
        the plugins panel AND re-run the light flag pass. Removing a mod must
        resurface the ⚠ missing-requirement flag on mods that depended on it —
        the seeded list stays in meta.ini for exactly this, and the flag
        two-pass recomputes installed ids from the remaining rows. Must be a
        FULL flag pass (no names subset): a subset would compute installed ids
        from the subset alone."""
        self._reload_plugins()
        self._refresh_modlist_flags()

    def _refresh_modlist_flags(self, names=None):
        """Re-read the meta/profile-derived flags and push them into the model
        WITHOUT the full modlist reset (keeps selection + scroll). Call after any
        action that changes a flag (endorse, root toggle, note edit). When
        *names* is given, only those mods' metas are re-read and merged into the
        existing state (endorsing one mod shouldn't re-read 500 meta files).
        The filemap-derived overlays (pre-RTX / root-rule) refresh via the
        conflict-ready path instead."""
        from gui_qt.modlist.modlist_data import read_meta_for_entries
        # Reached from ten call sites, several of them right after a background
        # worker finished — never trust the game object's current active-profile
        # state here. Without this, a stale _active_profile_dir sends
        # staging_dir() to the shared (empty) mods/ folder and the read below
        # comes back with no flags for anything.
        self._gs.reassert_active_profile()
        staging = self._gs.staging_dir()
        if staging is None:
            return
        subset = set(names) if names else None
        entries = [e for r in range(self._modlist_model.rowCount())
                   if (e := self._modlist_model.entry(r)) is not None
                   and (subset is None or e.name in subset)]
        try:
            (_v, _i, flags, categories, updates, fomod, bain,
             missing_reqs, _desc, authors) = read_meta_for_entries(
                entries, staging, self._ignored_missing_reqs,
                profile_dir=self._gs.profile_dir(),
                is_bg3=(getattr(self._gs.game, "game_id", "") == "baldurs_gate_3"),
                provided=game_provides_requirement(self._gs.game))
        except Exception:
            return
        # Safety net, independent of the guard above. A full refresh returning
        # nothing is legitimate (clearing the last note on a short list), but
        # not when the staging folder holds no meta.ini for ANY known mod —
        # that only happens when the read went to the wrong folder, and
        # blanking the whole Flags column on the strength of it is never right.
        # The probe only runs in that already-suspicious case, and stops at the
        # first hit.
        if (subset is None and not flags and entries
                and getattr(self._modlist_model, "_flags", None)
                and not any((staging / e.name / "meta.ini").is_file()
                            for e in entries)):
            self._append_log(
                f"Flag refresh skipped: no mod metadata found under {staging} "
                f"({len(entries)} mod(s) expected) — keeping the current flags.")
            return
        if subset is None:
            (self._mod_categories, self._mod_authors, self._mod_updates,
             self._mod_fomod, self._mod_bain, self._mod_missing_reqs) = (
                categories, authors, updates, fomod, bain, missing_reqs)
        else:
            # Merge: clear the requested names first (a cleared flag won't
            # appear in the subset result), then overlay the fresh values.
            flags = {**{n: b for n, b in self._modlist_model._flags.items()
                        if n not in subset}, **flags}
            self._mod_categories = {**{n: c for n, c in self._mod_categories.items()
                                       if n not in subset}, **categories}
            self._mod_authors = {**{n: a for n, a in
                                    getattr(self, "_mod_authors", {}).items()
                                    if n not in subset}, **authors}
            for cur, fresh in ((self._mod_updates, updates),
                               (self._mod_fomod, fomod),
                               (self._mod_bain, bain),
                               (self._mod_missing_reqs, missing_reqs)):
                cur -= subset
                cur |= fresh
        # FLAG_COLLECTION_ORDER_LOCKED (TW3) is NOT recomputed on this path --
        # it's a cheap targeted refresh after things like endorse/note-edit,
        # not a full profile rescan (which would need a real file-content
        # scan, too costly to run on every small action). Preserve whatever
        # it already was so this can't silently clear it; the full reload
        # path (_on_modlist_meta_ready) recomputes it properly.
        from gui_qt.modlist.modlist_data import FLAG_COLLECTION_ORDER_LOCKED
        prev_flags = getattr(self._modlist_model, "_flags", None) or {}
        for name, prev_bits in prev_flags.items():
            if prev_bits & FLAG_COLLECTION_ORDER_LOCKED:
                flags[name] = flags.get(name, 0) | FLAG_COLLECTION_ORDER_LOCKED
        self._modlist_model.set_flags(flags)
        # Re-point the model at the (possibly rebuilt) categories dict and
        # re-sort if the Category column drives the current sort.
        self._modlist_model._categories = self._mod_categories
        self._modlist_model._resort_if_key("category")
        self._modlist_model.set_notes(self._read_mod_notes())
        # If the Missing Requirements panel is open, drop cards for any
        # requirement that's now installed (works for any install path).
        view = getattr(self, "_missing_reqs_view", None)
        if view is not None:
            try:
                view.prune_installed(self._installed_mod_ids())
            except Exception:
                pass
        # This light path just mutated the meta-derived sets (updates / fomod /
        # bain / missing-reqs) in place. An active token search or panel filter
        # keys off the FilterData COPY of those sets, so refresh it and reapply —
        # otherwise a mod that just lost its !update flag lingers (or the list
        # goes stale) until a full Refresh.
        data = getattr(self, "_modlist_filter_data", None)
        if data is not None:
            data.mods_with_updates = set(self._mod_updates)
            data.fomod_mods = set(self._mod_fomod)
            data.bain_mods = set(self._mod_bain)
            data.missing_reqs = set(self._mod_missing_reqs)
            self._apply_modlist_filters()
        if self._modlist_token_search_active():
            self._apply_modlist_search()

    def _read_mod_notes(self) -> dict:
        """Per-mod note text for the active profile (Note flag tooltip). {} if
        none / no profile."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return {}
        try:
            from Utils.profile.profile_state import read_mod_notes
            return read_mod_notes(pdir)
        except Exception:
            return {}

    def _refresh_cycle_tab_data(self):
        """Push the pinned scope's current rules to the open cycle tab."""
        if not self._tabs.has_key("plugin_cycle"):
            return
        view = getattr(self, "_plugin_cycle_view", None)
        anchor = getattr(self, "_cycle_anchor", "")
        scope = getattr(self, "_cycle_scope", frozenset())
        if view is None or not anchor or not scope:
            return
        from Utils.userlist import build_cycle_scope_data, parse_userlist
        ul_path = self._userlist_path()
        data = (parse_userlist(ul_path) if (ul_path and ul_path.is_file())
                else {"plugins": [], "groups": []})
        display: dict[str, str] = {}
        for r in self._plugin_model._rows:
            display[r.name.lower()] = r.name
        for entry in data.get("plugins", []):
            raw = entry.get("name") or ""
            if raw:
                display.setdefault(raw.lower(), raw)
        info = build_cycle_scope_data(data, scope, display)
        view.update_cycle(
            starting_plugin=display.get(anchor, anchor),
            scope_plugins=scope,
            display_names=display,
            **info,
        )

    def _on_group_add(self, plugin_names: list):
        """'Add to group…' — open the inline group-assignment bar."""
        if self._userlist_path() is None:
            self._notify(self.tr("No active profile — cannot assign group."), "warning")
            return
        self._grp_bar.open_for(plugin_names)

    def _on_overlap_ready(self, plugin_name: str, overlaps):
        self._overlap_running = False
        if overlaps is None:
            self._notify(self.tr("Overlap check failed — see log."), "error")
            return
        if not overlaps:
            self._notify(self.tr("{0}: no record overlap with other plugins.").format(plugin_name),
                         "info")
            self._plugin_view.set_master_highlight(set())
            return

        # Highlight the target + overlapping plugins (green tint + marker strip),
        # then scroll the first highlighted row into view (Tk parity).
        names_lower = {plugin_name.lower(), *(o.lower() for o in overlaps)}
        self._plugin_view.set_master_highlight(names_lower)
        m = self._plugin_model
        first = next((i for i in range(m.rowCount())
                      if m.row(i).name.lower() in names_lower), None)
        if first is not None:
            from PySide6.QtWidgets import QAbstractItemView

            from gui_qt.plugin.plugin_model import COL_FLAGS
            self._plugin_view.scrollTo(m.index(first, COL_FLAGS),
                                       QAbstractItemView.PositionAtCenter)
        self._append_log(
            f"[loot] {plugin_name} overlaps with {len(overlaps)} plugin(s): "
            + ", ".join(overlaps))
        self._notify(
            self.tr("{0} overlaps {1} plugin(s) — highlighted in list").format(
                plugin_name, len(overlaps)),
            "warning")

    def _on_modlist_saved(self, edit_ctx=None):
        """modlist.txt was rewritten (every structural edit funnels here via
        model.save()). edit_ctx classifies the edit (see model.save):
          ("move", moved, crossed) — when the move provably can't have changed
            the filemap, skip EVERYTHING (worker + plugins reload + auto-deploy
            are all no-ops).
          ("toggle", changes) — when a disable provably can't change any
            conflict product, rebuild ONLY the filemap (the toggled mod's
            entries must still leave it) and auto-deploy; skip the scan.
          None/unknown → full rebuild."""
        if edit_ctx is not None:
            kind = edit_ctx[0]
            if kind == "move" and self._move_skips_rebuild(edit_ctx[1:]):
                return
            if kind == "toggle" and self._toggle_skips_conflict_scan(edit_ctx[1]):
                self._rebuild_filemap_light_async()
                return
        self._rebuild_conflicts_async()

    def _move_skips_rebuild(self, move_ctx) -> bool:
        """True when a reorder left every moved mod on the same side of all
        its conflict partners (loose AND archive), so no filemap winner,
        conflict code or override pair can have changed. Archive partners
        matter because orphan BSAs and UE paks rank by MOD priority — a move
        across an archive partner flips that winner (plugin-tied BSAs follow
        plugin order and are unaffected, but they're in the same maps, so
        including them just makes the check more conservative). Disabled mods
        aren't in the maps (empty partners). Sufficiency of the direct-pair
        maps: provider stacks are priority-ordered, so crossing ANY
        co-provider means first crossing a direct neighbour in that stack —
        which IS in the override maps. Conservative: any doubt (stale/missing
        conflict data, unknown mod) → full rebuild. AMM_MOVE_SKIP_REBUILD=0
        kills the fast path."""
        if os.environ.get("AMM_MOVE_SKIP_REBUILD") == "0":
            return False
        # Maps must describe the CURRENT modlist: _conflict_maps_current is
        # armed only when a build's result is accepted and dropped whenever a
        # rebuild is queued (a drag racing an in-flight toggle rebuild would
        # otherwise test against the pre-toggle maps). Skipped moves keep the
        # maps valid — that's exactly what this predicate proves.
        if not getattr(self, "_conflict_maps_current", False):
            return False
        data = self._conflict_data
        if data is None:
            return False
        moved, crossed = move_ctx
        crossed_set = set(crossed)
        if moved and crossed_set:
            for m in moved:
                partners = ((data.overrides.get(m) or set())
                            | (data.overridden_by.get(m) or set())
                            | (data.bsa_overrides.get(m) or set())
                            | (data.bsa_overridden_by.get(m) or set()))
                if partners & crossed_set:
                    return False
        self._append_log(
            f"[filemap] move: no conflict crossing — rebuild skipped "
            f"(moved={len(moved)}, crossed={len(crossed_set)})")
        return True

    def _toggle_skips_conflict_scan(self, changes) -> bool:
        """True when a toggle batch is disable-only and every disabled mod has
        no loose-conflict partners, no plugin files, no BSA/BA2 archives and
        no framework-exe files — then no ConflictData product changes: codes/
        override maps lose nothing (the mod's sets were empty), plugin_owner
        and the plugins panel keep the same winners, BSA conflicts follow
        plugin order + bsa_index (untouched), framework statuses can't flip
        (matching is by exe basename — the mod ships none). Only the filemap
        file set shrinks → _rebuild_filemap_light_async. Enables are never
        skipped: the maps can't prove a disabled mod won't CREATE conflicts.
        Capability sets come from the same accepted build as the maps (see
        ConflictData), so the _conflict_maps_current guard covers them too.
        AMM_TOGGLE_LIGHT=0 kills the fast path."""
        if os.environ.get("AMM_TOGGLE_LIGHT") == "0":
            return False
        if not getattr(self, "_conflict_maps_current", False):
            return False
        data = self._conflict_data
        if data is None or not changes:
            return False
        for name, enabled in changes:
            if enabled:
                return False
            if name not in data.overrides and name not in data.overridden_by:
                return False   # unknown to the maps → treat as stale
            if data.overrides.get(name) or data.overridden_by.get(name):
                return False
            if (name in data.plugin_mods or name in data.bsa_mods
                    or name in data.framework_file_mods):
                return False
        self._append_log(
            f"[filemap] toggle: disable-only, no conflicts/plugins/BSAs — "
            f"conflict scan skipped ({len(changes)} mod(s), filemap-only)")
        return True

    def _rebuild_filemap_light_async(self):
        """Disable fast path: rebuild ONLY the filemap (the incremental delta
        removes the toggled mods' entries) — _toggle_skips_conflict_scan proved
        every conflict product unchanged, so skip the scan, the panel reloads
        and the conflict-map repaints. Shares the build lock + generation with
        _rebuild_conflicts_async so full rebuilds serialize/supersede normally;
        _conflict_maps_current deliberately stays armed (the maps still
        describe reality — that is exactly what the predicate proved)."""
        self._reassert_profile_paths()
        gen = getattr(self, "_conflict_gen", 0) + 1
        self._conflict_gen = gen
        if not hasattr(self, "_conflict_build_lock"):
            self._conflict_build_lock = threading.Lock()
        g = self._gs.game
        profile = self._gs.profile
        if g is None or not profile:
            return

        def worker():
            from Utils.perftrace import span
            with self._conflict_build_lock:
                if gen != self._conflict_gen:
                    return   # superseded while waiting — the newer build covers us
                from Utils.deploy.deploy_pipeline import _build_filemap_for_game

                def _fm_log(m):
                    m = str(m)
                    try:
                        m.encode("utf-8")
                    except UnicodeEncodeError:
                        m = m.encode("utf-8", "backslashreplace").decode(
                            "utf-8", "replace")
                    print(f"[filemap] {m}", flush=True)
                    self._append_log(f"[filemap] {m}")

                try:
                    with span("build_filemap(light)"):
                        _build_filemap_for_game(
                            g, profile, log_fn=_fm_log, rescan_index=False)
                except Exception as exc:
                    print(f"[gui_qt] light filemap rebuild failed: {exc}",
                          flush=True)
            self._filemap_light_done.emit(gen)

        threading.Thread(target=worker, daemon=True).start()

    def _on_filemap_light_done(self, gen: int):
        """UI thread: a filemap-only rebuild finished. Refresh only what the
        deployed file SET touches — conflict maps, plugins panel, filter data
        and framework banner are untouched by construction."""
        if gen != self._conflict_gen:
            return   # superseded by a full rebuild — its handler covers this
        from Utils.perftrace import span
        with span("on_filemap_light_done"):
            if hasattr(self, "_data_view"):
                self._data_view.mark_dirty()
            if hasattr(self, "_text_files_view"):
                self._text_files_view.mark_dirty()
            self._refresh_modlist_stats()
            # Active filters may key on the enabled state — reapply from the
            # in-memory FilterData (no disk rescan needed).
            self._apply_modlist_filters()
        self._maybe_auto_deploy()

    def _rebuild_conflicts_async(self, rescan_index: bool = False):
        """Build the filemap off-thread; the worker emits _conflicts_ready
        (queued → UI thread). A generation counter drops results from a
        superseded reload (user switched game before the build finished).
        rescan_index=True forces a full disk rescan (Refresh button)."""
        # The maps the move fast-path tests are about to be superseded.
        self._conflict_maps_current = False
        self._reassert_profile_paths()
        gen = getattr(self, "_conflict_gen", 0) + 1
        self._conflict_gen = gen
        # A requested full rescan must SURVIVE supersession. Previously
        # rescan_index lived only in this call's worker closure: if a Refresh's
        # rescan build (rescan_index=True) was superseded — e.g. by the enable
        # toggle the user does immediately after Refresh, which triggers a plain
        # rescan_index=False rebuild — the Refresh worker returned early WITHOUT
        # rescanning, and the superseding build reused the STALE index. The
        # just-added mod then never entered modindex.bin, so it dropped out of
        # filemap.txt (no conflicts / plugins / Data-tab) until some later full
        # rescan happened to win the race (or a different tool — e.g. the Tk
        # build, which always rescans — rewrote the index on disk). Latch the
        # request on the window so whichever build actually runs consumes it.
        if rescan_index:
            self._pending_rescan_index = True
        # Serialize the actual build: rapid triggers (e.g. a Mod Files edit while
        # a previous rescan is still running) otherwise run two rebuild_mod_index
        # writes concurrently and collide on modindex.bin.tmp. The generation
        # check still drops superseded RESULTS.
        if not hasattr(self, "_conflict_build_lock"):
            self._conflict_build_lock = threading.Lock()

        from Utils.perftrace import span

        def worker():
            # Diagnostics below go to BOTH stderr and the GUI log panel
            # (_append_log, thread-safe) so a normal user can see + send them
            # without relaunching with an env var. They fire once per conflict
            # build / supersession (not per-frame), so they're low-noise.
            with self._conflict_build_lock:
                if gen != self._conflict_gen:
                    msg = (f"conflict build gen={gen} SUPERSEDED "
                           f"(current={self._conflict_gen}) — skipped before "
                           f"build; requested rescan={rescan_index}, "
                           f"latch pending={getattr(self, '_pending_rescan_index', False)}")
                    print(f"[plugin-diag] {msg}", flush=True)
                    self._append_log(f"[rescan-diag] {msg}")
                    return   # a newer build was queued while we waited — skip
                # Honour a latched rescan request even if THIS build was queued
                # as rescan_index=False (it superseded a pending Refresh). The
                # flag is NOT cleared here — it's cleared in _on_conflicts_ready
                # only when this build's result is actually ACCEPTED. That way,
                # if this rescan is itself superseded mid-build (its result
                # dropped by the gen check), the request stays latched for the
                # next build instead of being lost.
                _latched = getattr(self, "_pending_rescan_index", False)
                do_rescan = rescan_index or _latched
                # Record what this gen's build did, so _on_conflicts_ready can
                # clear the latch only when a rescanning build is accepted.
                self._conflict_gen_did_rescan = (gen, do_rescan)
                msg = (f"build gen={gen}: requested rescan={rescan_index}, "
                       f"latched={_latched} → doing rescan={do_rescan}")
                print(f"[plugin-diag] {msg}", flush=True)
                self._append_log(f"[rescan-diag] {msg}")
                def _fm_log(m):
                    # Console for our own tracing + the GUI log panel so a user
                    # actually SEES filemap warnings (enabled-mod-not-indexed,
                    # symlink skips, name mismatches). _append_log is thread-safe
                    # (marshals to the UI thread) and classifies WARN severity.
                    # Escape surrogate bytes from on-disk names FIRST — printing
                    # them raw raises UnicodeEncodeError on strict-UTF-8 stdout,
                    # which aborted the index rescan this log_fn serves.
                    m = str(m)
                    try:
                        m.encode("utf-8")
                    except UnicodeEncodeError:
                        m = m.encode("utf-8", "backslashreplace").decode(
                            "utf-8", "replace")
                    print(f"[filemap] {m}", flush=True)
                    self._append_log(f"[filemap] {m}")
                with span(f"build_conflicts(rescan={do_rescan})"):
                    data = self._gs.build_conflicts(
                        log_fn=_fm_log,
                        rescan_index=do_rescan)
                # Decisive check: after the build, is every ENABLED modlist mod
                # actually present in the index the filemap was built from? An
                # enabled mod missing here is THE failure (no conflicts/plugins).
                self._log_enabled_not_indexed(gen)
            self._conflicts_ready.emit(gen, data)

        threading.Thread(target=worker, daemon=True).start()

    def _log_enabled_not_indexed(self, gen: int) -> None:
        """Diagnostic (off-thread): report any ENABLED modlist mod that is
        absent from modindex.bin after a build. An enabled mod missing from the
        index is the exact failure behind "no conflicts / no plugins / not in
        Data tab" — it isolates whether the cause is a stale index (rescan race:
        mod on disk + in modlist but not indexed) vs. a name/scan issue
        (near-match key) vs. all-good. Reads the index once per build."""
        try:
            from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME, read_mod_index
            from Utils.mods.modlist import read_modlist
            staging = self._gs.staging_dir()
            ml = self._gs.modlist_path()
            if staging is None or ml is None or not ml.is_file():
                return
            index_path = staging.parent / "modindex.bin"
            index = read_mod_index(index_path) or {}
            # Surface the RESOLVED index path + any RIVAL modindex.bin at the
            # OTHER convention (shared <profile_root>/ vs profile-specific
            # <profile_dir>/). Two files means the staging-path resolution
            # flip-flopped (stale _active_profile_dir) and reads/writes are
            # hitting different indexes — the "two modindex.bin" bug.
            self._append_log(
                f"[rescan-diag] gen={gen}: index path = {index_path} "
                f"(exists={index_path.is_file()})")
            try:
                canon = index_path.resolve()
                pd = self._gs.profile_dir()
                stray = (pd / "modindex.bin") if pd is not None else None
                if (stray is not None and stray.is_file()
                        and stray.resolve() != canon):
                    self._append_log(
                        f"[rescan-diag] gen={gen}: WARNING — stray modindex.bin "
                        f"at {stray} describes the same shared mods folder as the "
                        f"canonical index ({index_path}); it is a legacy leftover "
                        f"and should be removed (a Refresh sweep removes it).")
            except Exception:
                pass
            enabled = [e.name for e in read_modlist(ml)
                       if e.enabled and not e.is_separator
                       and e.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)]
            missing = []
            for name in enabled:
                if name in index:
                    continue
                on_disk = (staging / name).is_dir()
                near = [k for k in index if k.strip().casefold() == name.strip().casefold()]
                missing.append((name, on_disk, near))
            if not missing:
                self._append_log(
                    f"[rescan-diag] gen={gen}: OK — all {len(enabled)} enabled "
                    f"mod(s) present in modindex.bin ({len(index)} indexed)")
                return
            self._append_log(
                f"[rescan-diag] gen={gen}: {len(missing)} ENABLED mod(s) MISSING "
                f"from modindex.bin (index has {len(index)} mod(s)):")
            for name, on_disk, near in missing[:20]:
                if near:
                    reason = f"NAME MISMATCH — index has {near!r}"
                elif on_disk:
                    reason = ("folder EXISTS on disk but NOT indexed → STALE INDEX "
                              "(rescan race / rescan skipped)")
                else:
                    reason = "folder not found on disk"
                self._append_log(f"[rescan-diag]   {name!r}: {reason}")
        except Exception as exc:
            print(f"[plugin-diag] _log_enabled_not_indexed error: {exc}", flush=True)

    def _on_conflicts_ready(self, gen: int, data):
        if gen != self._conflict_gen:
            msg = (f"conflicts_ready gen={gen} SUPERSEDED "
                   f"(current={self._conflict_gen}) — result dropped, plugins "
                   f"NOT reloaded from this build")
            print(f"[plugin-diag] {msg}", flush=True)
            self._append_log(f"[rescan-diag] {msg}")
            return
        # This build's result is accepted. If it performed the latched full
        # rescan, clear the latch now (a fresh, correct modindex.bin is on disk).
        # A rescan that was superseded mid-build never reaches here, so its
        # latch survives for the next build — the request can't be lost.
        _did = getattr(self, "_conflict_gen_did_rescan", None)
        if _did is not None and _did[0] == gen and _did[1]:
            self._pending_rescan_index = False
        from Utils.perftrace import span
        # Profile-switch milestone: the conflict/filemap build (usually the
        # long pole) has landed; the reload_plugins below is the final pass.
        if getattr(self, "_switch_t0", None) is not None:
            self._mark_since_switch("switch→conflicts_applied")  # i18n: skip — perftrace marker label
            self._switch_conflicts_done = True
        with span("on_conflicts_ready"):
            self._conflict_data = data
            # These maps now describe the on-disk modlist — arm the move
            # fast-path (see _move_skips_rebuild). Only valid if no newer
            # rebuild was queued meanwhile; the gen check above ensures that.
            self._conflict_maps_current = True
            with span("model.set_filemap_results"):
                # Conflicts + the filemap-derived flag overlays (info=pre-RTX,
                # root=custom root rule) in one dataChanged pass.
                self._modlist_model.set_filemap_results(
                    data.loose_codes, data.bsa_codes,
                    getattr(data, "prertx_mods", set()),
                    getattr(data, "root_rule_mods", set()))
            # Cross-panel highlighting needs the override + owner maps.
            with span("view.set_conflict_maps"):
                self._modlist_view.set_conflict_maps(
                    data.overrides, data.overridden_by,
                    data.bsa_overrides, data.bsa_overridden_by)
            self._plugin_view.set_plugin_owner(data.plugin_owner)
            # Rebuild the filter data + repopulate the filter panel's dynamic lists,
            # then reapply whatever filters are currently active.
            with span("rebuild_filter_data"):
                self._rebuild_filter_data()
            # The deployed filemap changed → the Data tab is stale (rebuilds lazily).
            if hasattr(self, "_data_view"):
                self._data_view.mark_dirty()
            # A mod may have been added/removed → re-evaluate Install vs Reinstall.
            if hasattr(self, "_downloads_view"):
                self._downloads_view.mark_dirty()
            # The deployed file set changed → the Text Files list is stale.
            if hasattr(self, "_text_files_view"):
                self._text_files_view.mark_dirty()
            # A mod may have been removed/added → re-evaluate the Nexus browser's
            # Install/Reinstall buttons (remove goes through save()→conflict rebuild,
            # which is the only signal the Nexus tab gets for a removal).
            nv = getattr(self, "_nexus_view", None)
            if nv is not None:
                nv.refresh_installed()
            # The filemap (staged/deployed file set) changed → framework states may
            # have flipped (e.g. a framework mod toggled, deployed, or removed).
            # Precomputed on the conflict worker (detect_frameworks re-reads
            # filemap.txt — too slow here); bump the gen so an in-flight async
            # detect can't overwrite this fresher result. set_statuses is a
            # no-op when the rows are unchanged, so the repeated refreshes a
            # deploy/restore fires don't rebuild (and flicker) the banner.
            if hasattr(self, "_framework_banner"):
                self._framework_gen += 1
                self._framework_banner.set_statuses(data.framework_statuses)
            # A mod was toggled / added / removed → the footer counts + size are
            # stale (token-guarded, so overlapping walks coalesce).
            with span("refresh_modlist_stats"):
                self._refresh_modlist_stats()
            # The filemap is now fresh → reload the Plugins tab so ESL/master flags
            # resolve against the winning (e.g. patcher-provided light) copies and
            # plugins still deployed by another enabled mod are recovered. Tk parity:
            # gui.py _on_filemap_rebuilt calls _refresh_plugins_tab() here, AFTER the
            # rebuild — reloading earlier (on the toggle) races the stale filemap.
            with span("reload_plugins"):
                self._reload_plugins()
        self._maybe_auto_deploy()

    def _maybe_auto_deploy(self):
        """Auto deploy: if the game has auto_deploy enabled, deploy after every
        successful conflict/filemap rebuild (enable/disable/reorder/install) —
        but NOT when the rebuild was itself triggered by that auto-deploy
        (deploy → _reload_modlist → rebuild → here again), or we'd loop.
        Tk parity: gui.py _on_filemap_rebuilt. Called from _on_conflicts_ready
        AND _on_filemap_light_done (a disabled mod's files must still leave
        the game folder)."""
        if self._auto_deploy_in_progress:
            self._auto_deploy_in_progress = False
        else:
            game = self._gs.game
            if (game is not None and game.is_configured()
                    and getattr(game, "auto_deploy", False)
                    and hasattr(game, "deploy")
                    and not self._deploy_running
                    # Never auto-deploy while an install batch is mid-flight —
                    # the deploy swaps the shared game object's active profile
                    # and races the install worker's path resolution. The
                    # install's own _on_install_done reload re-triggers this.
                    and not getattr(self, "_install_running", False)
                    # Same hazard for a detached-wizard staging job in flight —
                    # _on_wizard_finish_done's reload re-triggers this.
                    and not getattr(self, "_staged_finish_running", False)
                    and not self._staged_finish_queue):
                self._auto_deploy_in_progress = True
                self._on_deploy(silent=True)

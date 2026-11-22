"""Panel footers and left header: modlist, mod files, data, downloads and text-files footers, plus downloads actions and content search.

Methods of ``MainWindow``; see ``gui_qt/app_parts/__init__.py`` for why the
class is named ``MainWindow``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui_qt.flow_layout import FlowLayout
from gui_qt.icons import icon
from gui_qt.selector_button import SelectorButton
from gui_qt.theme.theme_qt import _c
from Utils.wine_proton.proton_tools import DOTNET_VERSIONS


class MainWindow:

    # ---------------------------------------------------------- panel footers
    @staticmethod
    def _enable_height_for_width(w: QWidget) -> None:
        """Make a footer widget report its height via heightForWidth (the wrapped
        FlowLayout height at the *actual* width) rather than its inflated
        sizeHint (which Qt evaluates at the widget's MINIMUM width, i.e. every
        button on its own row). Without this, a footer whose buttons only wrap
        at narrow widths still reserves the fully-wrapped tall height, leaving a
        blank gap above the search box."""
        pol = w.sizePolicy()
        pol.setHeightForWidth(True)
        w.setSizePolicy(pol)

    def _modlist_footer(self) -> QWidget:
        """Buttons row + search box — lives at the bottom of the modlist panel."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # FlowLayout so longer translated labels wrap to a second row instead of
        # overflowing the panel (see gui_qt/flow_layout.py). Centred so the row
        # stays aligned with the middle of the modlist panel at any width.
        btns = FlowLayout(spacing=4, center=True)
        # label -> handler ("" = no-op stub, needs a dialog/auth — wired later).
        _handlers = {
            "Expand all": self._on_toggle_collapse_all,
            "Enable all": self._on_toggle_enable_all,
            "Filters": self._toggle_modlist_filters,
            "Refresh Modlist": self._on_refresh_modlist,
            "Check Updates": self._on_check_updates,
        }
        # (canonical key, translated display). Key drives handler lookup + the
        # == comparisons below; display is the visible button text.
        # "Backups…"/"Backup now…" moved to the profile dropdown menu (see
        # _profile_actions) — they belong next to Export/Import, not buried
        # in this row, per user feedback.
        self._modlist_footer_btns: list[QToolButton] = []
        for label, disp in [("Expand all", self.tr("Expand all")),
                            ("Enable all", self.tr("Enable all")),
                            ("Check Updates", self.tr("Check Updates")),
                            ("Filters", self.tr("Filters")),
                            ("Refresh Modlist", self.tr("Refresh Modlist"))]:
            b = self._text_button(disp, compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            if label in _handlers:
                b.clicked.connect(_handlers[label])
            if label == "Filters":
                b.setProperty("active", False)
                self._modlist_filters_btn = b
            elif label == "Expand all":
                self._expand_all_btn = b
            elif label == "Enable all":
                self._enable_all_btn = b
            elif label == "Check Updates":
                self._check_updates_btn = b
            btns.addWidget(b)
            self._modlist_footer_btns.append(b)
        v.addLayout(btns)

        # Search row: an enabled/total count label (blue outline) to the left of
        # a full-width search box. The count label replaces the old Enabled /
        # Disabled stat pill row.
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)

        count = QLabel("…")
        count.setObjectName("ModlistCountLabel")
        count.setAlignment(Qt.AlignCenter)
        count.setStyleSheet(
            "QLabel#ModlistCountLabel {"
            f" color: {_c(self._pal, 'TEXT_MAIN')};"
            f" border: 1px solid {_c(self._pal, 'ACCENT')};"
            " border-radius: 4px; padding: 2px 8px; }")
        self._modlist_count = count
        search_row.addWidget(count)

        # Search box spans the remaining footer width. (It used to be pinned to
        # the summed button-row width so its edge lined up with the last button;
        # that assumed a single, non-wrapping row — with the FlowLayout the
        # buttons can wrap, so a fixed cap would over/under-shoot. Full width is
        # simpler and robust across languages.)
        search_icon = QLabel()
        search_icon.setPixmap(icon("search.png", 18).pixmap(18, 18))
        search_icon.setToolTip(self._modlist_search_tooltip())
        search_icon.setAlignment(Qt.AlignCenter)
        search_row.addWidget(search_icon)

        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search mods… (try !update, !fomod, !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._on_modlist_search)
        self._modlist_search = search
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._enable_height_for_width(bar)
        return bar

    def _mod_files_footer(self) -> QWidget:
        """Pack/Unpack BSA + Filters buttons + search, shown under the plugins
        column when the Mod Files sub-tab is active (replaces the plugin tools)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._mf_pack_btn = self._color_button(
            self.tr("Pack BSA"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        self._mf_pack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_pack_btn.setEnabled(False)
        self._mf_pack_btn.clicked.connect(self._on_pack_bsa)
        self._mf_unpack_btn = self._color_button(
            self.tr("Unpack BSA"), _c(self._pal, "BTN_DANGER"), compact=True)
        self._mf_unpack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_unpack_btn.setEnabled(False)
        self._mf_unpack_btn.clicked.connect(self._on_unpack_bsa)
        self._mf_filters_btn = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True)
        self._mf_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_filters_btn.clicked.connect(self._toggle_mod_files_filters)
        self._mf_expand_btn = self._text_button(self.tr("⊞ Expand all"), compact=True)
        self._mf_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_expand_btn.clicked.connect(self._on_mf_expand_clicked)
        self._equalize_button_widths(self._mf_pack_btn, self._mf_unpack_btn)
        btns.addWidget(self._mf_expand_btn)
        btns.addWidget(self._mf_pack_btn)
        btns.addWidget(self._mf_unpack_btn)
        v.addLayout(btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._mf_filters_btn)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(
            lambda t: self._mod_files_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._mod_files_search = search
        return bar

    def _data_footer(self) -> QWidget:
        """Filters + Expand-all + search, shown under the plugins column when the
        Data sub-tab is active (no Pack/Unpack — Data is read-only)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._data_filters_btn = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True)
        self._data_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_filters_btn.clicked.connect(self._toggle_data_filters)
        self._data_expand_btn = self._text_button(self.tr("⊞ Expand all"), compact=True)
        self._data_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_expand_btn.clicked.connect(self._on_data_expand_clicked)
        btns.addWidget(self._data_expand_btn)
        v.addLayout(btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._data_filters_btn)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._data_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._data_search = search
        return bar

    def _on_data_expand_clicked(self):
        expanded = self._data_view._toggle_expand_all()
        self._data_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                      else self.tr("⊞ Expand all"))

    def _downloads_footer(self) -> QWidget:
        """Install Selected / Remove Selected / Locations / Filters + search,
        shown under the plugins column when the Downloads sub-tab is active."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._dl_install_btn = self._color_button(
            self.tr("Install Selected"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        self._dl_install_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_install_btn.setEnabled(False)
        self._dl_install_btn.clicked.connect(
            lambda: self._downloads_view.install_selected())
        self._dl_move_btn = self._color_button(
            self.tr("Move Selected"), _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_move_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_move_btn.setEnabled(False)
        self._dl_move_btn.clicked.connect(self._on_downloads_move)
        self._dl_remove_btn = self._color_button(
            self.tr("Remove Selected"), _c(self._pal, "BTN_DANGER"), compact=True)
        self._dl_remove_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_remove_btn.setEnabled(False)
        self._dl_remove_btn.clicked.connect(self._on_downloads_remove)
        self._dl_locations_btn = self._color_button(
            self.tr("Locations"), _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_locations_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_locations_btn.clicked.connect(self._on_downloads_locations)
        self._dl_filters_btn = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_filters_btn.clicked.connect(self._toggle_downloads_filters)
        self._equalize_button_widths(
            self._dl_install_btn, self._dl_move_btn, self._dl_remove_btn)
        btns.addWidget(self._dl_install_btn)
        btns.addWidget(self._dl_move_btn)
        btns.addWidget(self._dl_remove_btn)
        btns.addWidget(self._dl_locations_btn)
        v.addLayout(btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._dl_filters_btn)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search downloads…"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._downloads_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._dl_search = search
        return bar

    def _update_downloads_footer(self):
        n = self._downloads_view.checked_count()
        for attr, label in (("_dl_install_btn", self.tr("Install Selected")),
                            ("_dl_move_btn", self.tr("Move Selected")),
                            ("_dl_remove_btn", self.tr("Remove Selected"))):
            b = getattr(self, attr, None)
            if b is not None:
                b.setEnabled(n > 0)
                b.setText(self.tr("{0} ({1})").format(label, n) if n else label)

    def _on_downloads_locations(self):
        from gui_qt.downloads.download_locations_overlay import DownloadLocationsOverlay
        DownloadLocationsOverlay.show_over(
            self,
            lambda saved: self._downloads_view.refresh() if saved else None)

    def _on_downloads_remove(self):
        paths = self._downloads_view.checked_paths()
        if not paths:
            return
        names = "\n".join(Path(p).name for p in paths[:20])
        more = f"\n… and {len(paths) - 20} more" if len(paths) > 20 else ""

        def _confirmed(ok):
            if not ok:
                return
            removed = 0
            for p in paths:
                try:
                    Path(p).unlink()
                    removed += 1
                except OSError as exc:
                    print(f"[gui_qt] remove failed: {p}: {exc}", flush=True)
            self._notify(self.tr("Removed {0} archive(s)").format(removed), "info")
            self._downloads_view.clear_checks()
            self._downloads_view.refresh()

        from gui_qt.overlays.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Remove archives"),
            self.tr("Permanently delete {0} archive(s) from disk?\n\n").format(len(paths))
            + names + more,
            _confirmed, confirm_label=self.tr("Delete"))

    def _on_downloads_move(self):
        """Move the checked archives between the *configured* download locations.
        Opens a borderless overlay listing those locations (Downloads / Mod
        Manager cache / extras) rather than a native folder browser."""
        paths = self._downloads_view.checked_paths()
        if not paths:
            return
        game_name = getattr(self._gs, "game_name", None)
        from gui_qt.downloads.move_downloads_overlay import MoveDownloadsOverlay
        MoveDownloadsOverlay.show_over(
            self, len(paths), game_name,
            lambda dest: self._move_downloads_to(paths, dest))

    def _move_downloads_to(self, paths, dest):
        if not dest or not paths:
            return
        dest_dir = Path(dest)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._notify(self.tr("Cannot use that folder: {0}").format(exc), "error")
            return

        # Anything that would overwrite an existing file at the destination needs
        # confirmation before we clobber it.
        clashes = [p for p in paths
                   if (dest_dir / Path(p).name).exists()
                   and Path(dest_dir / Path(p).name).resolve() != Path(p).resolve()]

        def _do_move():
            moved = 0
            for p in paths:
                src = Path(p)
                target = dest_dir / src.name
                try:
                    if src.resolve() == target.resolve():
                        continue  # already there
                except OSError:
                    pass
                try:
                    if target.exists():
                        target.unlink()
                    shutil.move(str(src), str(target))
                    moved += 1
                except OSError as exc:
                    print(f"[gui_qt] move failed: {p} -> {target}: {exc}", flush=True)
            self._notify(self.tr("Moved {0} archive(s)").format(moved), "info")
            self._downloads_view.clear_checks()
            self._downloads_view.refresh()

        if clashes:
            names = "\n".join(Path(p).name for p in clashes[:20])
            more = f"\n… and {len(clashes) - 20} more" if len(clashes) > 20 else ""
            from gui_qt.overlays.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self, self.tr("Overwrite archives"),
                self.tr("{0} file(s) already exist in that folder and will be "
                        "overwritten:\n\n").format(len(clashes)) + names + more,
                lambda ok: _do_move() if ok else None,
                confirm_label=self.tr("Overwrite"))
        else:
            _do_move()

    def _text_files_footer(self) -> QWidget:
        """Search Content / Filters + search, shown under the plugins column when
        the Text Files sub-tab is active (read-only — no Pack/Install)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Inline content-search popup (hidden until "Search Content" is clicked).
        self._tf_content_bar = QWidget()
        cbl = QHBoxLayout(self._tf_content_bar)
        cbl.setContentsMargins(0, 0, 0, 0)
        cbl.setSpacing(4)
        lbl = QLabel(self.tr("Find in files:"))
        lbl.setStyleSheet(f"color:{_c(self._pal,'TEXT_DIM')};")
        cbl.addWidget(lbl)
        self._tf_content_input = QLineEdit()
        self._tf_content_input.setPlaceholderText(self.tr("Text to search for…"))
        self._tf_content_input.setClearButtonEnabled(True)
        self._tf_content_input.returnPressed.connect(self._run_tf_content_search)
        cbl.addWidget(self._tf_content_input, 1)
        go = self._color_button(self.tr("Search"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        go.setFixedHeight(self._FOOT_BTN_H)
        go.clicked.connect(self._run_tf_content_search)
        cbl.addWidget(go)
        close = self._text_button("✕", compact=True)
        close.setFixedHeight(self._FOOT_BTN_H)
        close.clicked.connect(self._close_tf_content_bar)
        cbl.addWidget(close)
        self._tf_content_bar.setVisible(False)
        v.addWidget(self._tf_content_bar)

        btns = FlowLayout(spacing=4)
        self._tf_content_btn = self._color_button(
            self.tr("Search Content"), _c(self._pal, "BTN_INFO"), compact=True)
        self._tf_content_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_content_btn.clicked.connect(self._on_text_files_content_search)
        self._tf_filters_btn = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True)
        self._tf_filters_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_filters_btn.clicked.connect(self._toggle_text_files_filters)
        self._tf_expand_btn = self._text_button(
            self.tr("⊞ Expand all"), compact=True)
        self._tf_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_expand_btn.clicked.connect(self._on_tf_expand_clicked)
        btns.addWidget(self._tf_expand_btn)
        btns.addWidget(self._tf_content_btn)
        # A dim status label showing the active content-search keyword.
        self._tf_content_status = QLabel("")
        self._tf_content_status.setStyleSheet(
            f"color:{_c(self._pal,'TEXT_DIM')};")
        btns.addWidget(self._tf_content_status)
        v.addLayout(btns)
        self._text_files_view.content_status_changed.connect(
            self._on_tf_content_status)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._tf_filters_btn)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._text_files_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._tf_search = search
        return bar

    def _set_tf_content_bar_visible(self, visible: bool):
        """Show/hide the inline find-in-files bar, then re-clamp the footer
        stack: its max height is pinned to the current page's height (see
        _CurrentPageStack), so the page growing a row would otherwise be
        squashed into the old clamped height."""
        self._tf_content_bar.setVisible(visible)
        self._plugin_footer_stack.clamp_to_current()

    def _on_text_files_content_search(self):
        """Toggle the inline content-search bar. When a search is active, this
        clears it; otherwise it opens the input bar above the footer + focuses it."""
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()
            self._tf_content_input.clear()
            self._set_tf_content_bar_visible(False)
            return
        showing = not self._tf_content_bar.isVisible()
        self._set_tf_content_bar_visible(showing)
        if showing:
            self._tf_content_input.setFocus()
            self._tf_content_input.selectAll()

    def _on_tf_expand_clicked(self):
        expanded = self._text_files_view._toggle_expand_all()
        self._tf_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                    else self.tr("⊞ Expand all"))

    def _run_tf_content_search(self):
        kw = self._tf_content_input.text().strip()
        if kw:
            self._text_files_view.run_content_search(kw)
        else:
            self._text_files_view.clear_content_search()

    def _close_tf_content_bar(self):
        self._set_tf_content_bar_visible(False)
        self._tf_content_input.clear()
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()

    def _on_tf_content_status(self, keyword):
        if keyword:
            self._tf_content_status.setText(self.tr('Content: "{0}"').format(keyword))
            self._tf_content_btn.setText(self.tr("Clear Content"))
        else:
            self._tf_content_status.setText("")
            self._tf_content_btn.setText(self.tr("Search Content"))

    def _left_header(self) -> QWidget:
        # Single row: game/profile selectors, then the mod-action buttons.
        header = QWidget()
        header.setObjectName("HeaderBar")
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        # Game selector — no label; the game names make it self-evident. Items
        # are populated from the configured games (_populate_selectors); with
        # none configured the button reads "Add game".
        self._game_selector = SelectorButton(
            items=[],
            current=self.tr("Add game"),
            actions=self._game_actions(),
            on_select=self._confirm_game_change,
        )
        self._game_selector.setFixedHeight(self._BTN_H)
        h.addWidget(self._game_selector)

        # Profile selector — "Profile:" prefix baked into the button text.
        self._profile_selector = SelectorButton(
            items=["default"],
            current="default",
            prefix=self.tr("Profile: "),
            min_width=150,
            actions=self._profile_actions(),
            on_select=self._confirm_profile_change,
        )
        self._profile_selector.setFixedHeight(self._BTN_H)
        # Rebuild the pinned actions on every open (like the Wizard menu): the
        # "Open" submenu and Quick-configure entries are gated on live game
        # settings, so toggling e.g. profile-specific INI files via Quick
        # configure must make the Open submenu appear without a profile swap.
        self._profile_selector._menu.aboutToShow.connect(
            self._refresh_profile_actions)
        h.addWidget(self._profile_selector)

        h.addWidget(self._group_sep())

        # Plain mod-action buttons.
        self._action_buttons = []
        _handlers = {"Install Mod": self._on_install_mod,
                     # drop QPushButton.clicked's `bool checked` arg — it would
                     # otherwise land in _on_deploy's `silent` parameter.
                     "Deploy": lambda: self._on_deploy(), "Restore": self._on_restore}
        for label, disp, ico in [
            ("Install Mod", self.tr("Install Mod"), "install.png"),
            ("Deploy",      self.tr("Deploy"),      "deploy.png"),
            ("Restore",     self.tr("Restore"),     "restore.png"),
        ]:
            # `label` stays the canonical key (handler lookup + _full_label);
            # `disp` is the translated text shown on the button + tooltip.
            b = self._action_button(disp, ico)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(disp)
            b._full_label = disp
            if label in _handlers:
                b.clicked.connect(_handlers[label])
                if label == "Deploy":
                    self._deploy_btn = b
                elif label == "Restore":
                    self._restore_btn = b
                elif label == "Install Mod":
                    self._install_btn = b
            self._action_buttons.append(b)
            h.addWidget(b)

        # Split menu buttons (placeholder menus — wired up in a later phase).
        # `label` stays the canonical key (used in == comparisons + as the
        # button's persistent id); `disp` is the translated button text. Menu
        # item labels are user-visible and translated directly.
        for label, disp, ico, items in [
            ("Proton", self.tr("Proton"), "proton.png", [
                (self.tr("Run winecfg"), self._proton_winecfg),
                (self.tr("Run winetricks"), self._proton_winetricks),
                (self.tr("Run an .exe in this prefix…"), self._proton_run_exe),
                None,
                (self.tr("Open wine registry"), self._proton_regedit),
                (self.tr("Wine DLL overrides"), self._proton_dll_overrides),
                None,
                (self.tr("Install VC++ Redistributable"), self._proton_install_vcredist),
                (self.tr("Install d3dcompiler_47"), self._proton_install_d3dcompiler),
                (self.tr("Install XACT audio (XAudio2)"), self._proton_install_xact),
                (self.tr(".NET runtime"), [
                    (self.tr(".NET {0}").format(v), (lambda v=v: self._proton_install_dotnet(v)))
                    for v in DOTNET_VERSIONS
                ]),
            ]),
            # Wizard's menu is dynamic — rebuilt per game on aboutToShow.
            ("Wizard", self.tr("Wizard"), "wizard.png", []),
            ("Nexus", self.tr("Nexus"), "nexus.png", [
                (self.tr("Open Nexus Mods"), self._open_nexus_browser_tab),
                (self.tr("Open game on nexus"), self._open_game_on_nexus),
                None,
                (self.tr("Login to Nexus"), [
                    (self.tr("Login via SSO"), self._nexus_login_sso),
                    (self.tr("Paste login code…"), self._nexus_paste_code),
                    (self.tr("Clear credentials"), self._nexus_clear_credentials),
                    (self._nxm_menu_label(), self._nexus_toggle_nxm),
                ]),
                (self.tr("Collections"), [
                    (self.tr("Browse collections…"), self._open_collections_tab),
                    (self.tr("Open current collection"), self._open_current_collection),
                    (self.tr("Reset load order"), self._reset_collection_load_order),
                ]),
            ]),
            ("Modhold", self.tr("Modhold"), "modhold.png", [
                (self.tr("Browse Modhold…"), self._open_modhold_browser_tab),
                (self.tr("Browse loadouts…"), self._open_modhold_loadouts_tab),
                None,
                (self.tr("Account…"), self._open_modhold_account),
            ]),
            ("Wabbajack", self.tr("Wabbajack"), "wabbajack.png", [
                (self.tr("Browse modlists…"),
                 lambda: self._wabbajack_controller().open_browser()),
                (self.tr("Import .wabbajack file…"),
                 lambda: self._wabbajack_controller().start_import()),
                None,
                (self.tr("LoversLab"), [
                    (self.tr("Log in…"),
                     lambda: self._wabbajack_controller().loverslab_login()),
                    (self.tr("Clear credentials"),
                     lambda: self._wabbajack_controller().loverslab_logout()),
                ]),
                (self.tr("Open Wabbajack gallery website"), self._open_wabbajack_website),
            ]),
        ]:
            # Proton's logo is a mono glyph — tint it white like the Settings
            # icon so it stays visible. The others are full-colour logos.
            tint = _c(self._pal, "TEXT_MAIN") if label == "Proton" else None
            b = self._menu_action_button(disp, ico, items, tint=tint)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(disp)
            b._full_label = disp
            if label == "Wizard":
                self._wizard_btn = b
                b._menu.aboutToShow.connect(self._rebuild_wizard_menu)
            self._action_buttons.append(b)
            h.addWidget(b)

        h.addStretch(1)

        # Settings — icon-only square button on the far right. Opens a Settings
        # tab scoped over the Plugins panel.
        self._settings_button = self._icon_square_button(
            "settings.png", tooltip=self.tr("Settings"), tint=_c(self._pal, "TEXT_MAIN"))
        self._settings_button.clicked.connect(self._open_settings_tab)
        h.addWidget(self._settings_button)

        self._left_header_widget = header
        return header

    def _icon_square_button(self, icon_name: str, tooltip: str = "",
                            tint: str | None = None) -> QToolButton:
        """A compact square icon-only button (e.g. Settings) for the toolbar.

        *tint* recolours a mono glyph to a theme colour so it stays visible in
        both light and dark modes (the raw PNGs are white)."""
        b = QToolButton()
        b.setIcon(icon(icon_name, self._ICON_PX, color=tint))
        b.setIconSize(QSize(self._ICON_PX, self._ICON_PX))
        b.setToolButtonStyle(Qt.ToolButtonIconOnly)
        b.setObjectName("IconButton")
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(self._BTN_H, self._BTN_H)
        if tooltip:
            b.setToolTip(tooltip)
        return b

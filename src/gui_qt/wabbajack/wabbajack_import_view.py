"""The tab shown after picking a ``.wabbajack`` file: what the modlist is,
where its downloads come from, and the preflight result. A modlist that
ships several MO2 profiles (e.g. GOG and Steam variants, or the 17 versions
the VtMB list "Fashionista - Steam" ships) shows a checkable profile list
with nothing preselected: tick as many as you want and each becomes its own
Mosaic profile, sharing one copy of the mods. Install is only enabled when no
check blocks and, if there's a list, a profile is ticked; a LoversLab login is offered in place when
preflight asks for one (the controller re-runs preflight afterwards via
:meth:`set_checks`).
"""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui_qt.theme.theme_qt import _c, active_palette
from Utils.collections.collection_manifest import fmt_size
from Utils.collections.collection_preflight import blocking_failures
from Utils.wabbajack.wabbajack_manifest import ModList, UnknownState, mosaic_game_for
from Utils.wabbajack.wabbajack_preflight import FIX_LOVERSLAB_LOGIN

_SOURCE_LABELS = {
    "NexusState": "Nexus Mods", "HttpState": "Direct link", "WabbajackCDNState": "Wabbajack CDN",
    "GoogleDriveState": "Google Drive", "MediaFireState": "MediaFire", "MegaState": "Mega",
    "LoversLabState": "LoversLab", "ManualState": "Manual download",
}


def source_breakdown(modlist: ModList) -> str:
    counts = Counter(
        (a.state.type_name.split(",")[0] or "Unknown") if isinstance(a.state, UnknownState)
        else _SOURCE_LABELS.get(type(a.state).__name__, type(a.state).__name__)
        for a in modlist.archives)
    return " · ".join(f"{label}: {n}" for label, n in counts.most_common())


class WabbajackImportView(QWidget):
    def __init__(self, modlist: ModList, checks, *, on_install, on_login,
                 profiles=(), repair_profiles=(), preselect=(), on_repair=None,
                 parent=None):
        """``on_install(profiles)`` gets the ticked profile names, or ``[]``
        when the modlist has at most one profile (``profiles``).
        ``repair_profiles`` names existing Mosaic profiles installed from this
        same modlist and ``preselect`` the MO2 profiles they came from;
        ``on_repair()`` then builds only their missing files."""
        super().__init__(parent)
        self._blocked = False
        self._installing = False
        self._on_repair = on_repair
        self._repair_btn = None
        self._profile_list = None
        self._modlist = modlist
        self._on_install = on_install
        self._on_login = on_login
        p = active_palette()
        self._p = p

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(10)

        title = QLabel(modlist.name or self.tr("Untitled modlist"))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:20px;")
        title.setWordWrap(True)
        v.addWidget(title)

        game = mosaic_game_for(modlist.game_type) or modlist.game_type or self.tr("unknown game")
        facts = [self.tr("by {0}").format(modlist.author) if modlist.author else "",
                 self.tr("version {0}").format(modlist.version) if modlist.version else "",
                 game]
        sub = QLabel(" · ".join(f for f in facts if f))
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        v.addWidget(sub)

        sizes = QLabel(self.tr(
            "{0} archives, {1} to download · about {2} installed").format(
                len(modlist.archives), fmt_size(modlist.total_archive_size),
                fmt_size(modlist.total_install_size)))
        sizes.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        v.addWidget(sizes)
        sources = QLabel(source_breakdown(modlist))
        sources.setWordWrap(True)
        sources.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        v.addWidget(sources)

        if modlist.description:
            desc = QLabel(modlist.description)
            desc.setWordWrap(True)
            desc.setTextFormat(Qt.PlainText)
            desc.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px; margin-top:6px;")
            v.addWidget(desc)

        if len(profiles) > 1:
            pick_title = QLabel(self.tr("Profile"))
            pick_title.setStyleSheet(
                f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:14px; margin-top:10px;")
            v.addWidget(pick_title)
            pick_note = QLabel(self.tr(
                "This modlist comes in several versions with different mods and load "
                "orders. Tick the ones you want: each becomes its own profile in "
                "Mosaic, and they share one copy of the mods."))
            pick_note.setWordWrap(True)
            pick_note.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
            v.addWidget(pick_note)
            self._profile_list = QListWidget()
            self._profile_list.setMaximumHeight(220)
            self._profile_list.setSelectionMode(QListWidget.NoSelection)
            preselected = {str(s) for s in preselect}
            for name in profiles:
                item = QListWidgetItem(name, self._profile_list)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if name in preselected else Qt.Unchecked)
            self._profile_list.itemChanged.connect(lambda _i: self._refresh_install())
            v.addWidget(self._profile_list)
            picks = QHBoxLayout()
            for label, state in ((self.tr("Select all"), Qt.Checked),
                                 (self.tr("Select none"), Qt.Unchecked)):
                btn = QPushButton(label)
                btn.setObjectName("FormButton")
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(lambda _c=False, s=state: self._set_all(s))
                picks.addWidget(btn)
            picks.addStretch(1)
            v.addLayout(picks)

        checks_title = QLabel(self.tr("Before installing"))
        checks_title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:14px; margin-top:10px;")
        v.addWidget(checks_title)
        self._checks_box = QVBoxLayout()
        self._checks_box.setSpacing(6)
        v.addLayout(self._checks_box)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.setContentsMargins(24, 8, 24, 16)
        self._login_btn = QPushButton(self.tr("Log in to LoversLab…"))
        self._login_btn.setObjectName("FormButton")
        self._login_btn.setCursor(Qt.PointingHandCursor)
        self._login_btn.clicked.connect(lambda: self._on_login())
        bar.addWidget(self._login_btn)
        bar.addStretch(1)
        if repair_profiles and on_repair is not None:
            self._repair_btn = QPushButton(
                self.tr("Repair '{0}'").format(repair_profiles[0]) if len(repair_profiles) == 1
                else self.tr("Repair {0} profiles").format(len(repair_profiles)))
            self._repair_btn.setObjectName("FormButton")
            self._repair_btn.setCursor(Qt.PointingHandCursor)
            self._repair_btn.setToolTip(self.tr(
                "Keep the files already built in this profile and build only what is missing."))
            self._repair_btn.clicked.connect(
                lambda: self._on_repair())
            bar.addWidget(self._repair_btn)
        self._install_btn = QPushButton(self.tr("Install as new profile"))
        self._install_btn.setObjectName("PrimaryButton")
        self._install_btn.setCursor(Qt.PointingHandCursor)
        self._install_btn.clicked.connect(lambda: self._on_install(self.selected_profiles()))
        bar.addWidget(self._install_btn)
        outer.addLayout(bar)

        self.set_checks(checks)

    def set_checks(self, checks) -> None:
        while self._checks_box.count():
            old = self._checks_box.takeAt(0).widget()
            if old is not None:
                # Detach now: deleteLater alone leaves the old row painting
                # (overlapping the new one) until the deferred delete runs.
                old.hide()
                old.setParent(None)
                old.deleteLater()
        for check in checks:
            self._checks_box.addWidget(self._check_row(check))
        self._login_btn.setVisible(any(c.fix == FIX_LOVERSLAB_LOGIN and not c.ok for c in checks))
        self._blocked = bool(blocking_failures(checks))
        self._refresh_install()

    def selected_profiles(self) -> "list[str]":
        if self._profile_list is None:
            return []
        return [self._profile_list.item(i).text()
                for i in range(self._profile_list.count())
                if self._profile_list.item(i).checkState() == Qt.Checked]

    def _set_all(self, state) -> None:
        lst = self._profile_list
        if lst is None:
            return
        lst.blockSignals(True)
        for i in range(lst.count()):
            lst.item(i).setCheckState(state)
        lst.blockSignals(False)
        self._refresh_install()

    def set_installing(self, installing: bool) -> None:
        self._installing = installing
        self._refresh_install()

    def _refresh_install(self) -> None:
        chosen = self.selected_profiles()
        needs_profile = self._profile_list is not None and not chosen
        enabled = not (self._blocked or needs_profile or self._installing)
        self._install_btn.setText(
            self.tr("Install as {0} new profiles").format(len(chosen)) if len(chosen) > 1
            else self.tr("Install as new profile"))
        self._install_btn.setEnabled(enabled)
        if self._repair_btn is not None:
            self._repair_btn.setEnabled(not (self._blocked or self._installing))
        if self._blocked:
            tip = self.tr("Resolve the problems above first.")
        elif needs_profile:
            tip = self.tr("Tick at least one profile first.")
        else:
            tip = ""
        self._install_btn.setToolTip(tip)

    def _check_row(self, check) -> QWidget:
        p = self._p
        if check.ok:
            mark, key = "✓", "TEXT_OK_BRIGHT"
        elif check.blocking:
            mark, key = "✗", "TEXT_ERR_BRIGHT"
        else:
            mark, key = "!", "TEXT_WARN_BRIGHT"
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setAlignment(Qt.AlignTop)
        icon = QLabel(mark)
        icon.setFixedWidth(18)
        icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        icon.setStyleSheet(f"color:{_c(p, key)}; font-weight:700; font-size:14px;")
        h.addWidget(icon)
        text = QLabel(f"<b>{_escape(check.title)}</b>"
                      + (f"<br><span style='color:{_c(p,'TEXT_DIM')}'>{_escape(check.detail)}</span>"
                         if check.detail else ""))
        text.setWordWrap(True)
        text.setTextFormat(Qt.RichText)
        text.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        h.addWidget(text, 1)
        return row


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

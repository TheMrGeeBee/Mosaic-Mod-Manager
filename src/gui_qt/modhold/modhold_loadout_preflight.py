"""The question asked before a Modhold loadout installs: what to include, where to put it."""

from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from Modhold.browse_util import format_size
from Modhold.loadout_manifest import Loadout, LoadoutPlan, plan_install
from Modhold.nexus_links import NexusPick


def default_profile_name(loadout: Loadout) -> str:
    """A profile-safe name from the loadout's own (same recipe as Nexus collections)."""
    base = re.sub(r"[^\w\s\-]", "", loadout.name or "").strip().replace(" ", "_")
    return (base or f"Loadout_{loadout.id}")[:64]


def free_profile_name(base: str, existing: set[str]) -> str:
    """*base*, or *base*_2, _3 ... when a profile of that name already exists."""
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}"[:64] in existing:
        n += 1
    return f"{base}_{n}"[:64]


class ModholdLoadoutPreflight(QDialog):
    """Summary of a loadout plus the choices that shape the install.

    After ``accept()``, read :meth:`plan`, :meth:`profile_name` and
    :meth:`continue_existing`."""

    def __init__(self, loadout: Loadout, existing_profiles: "set[str] | list[str]",
                 parent: QWidget | None = None,
                 nexus_picks: "dict[int, NexusPick] | None" = None,
                 nexus_note: str = ""):
        """*nexus_picks* is the file chosen for each Nexus-linked item (see
        Modhold/nexus_links.resolve_nexus_picks), or None when Nexus can't be used
        (not connected, or no Premium); *nexus_note* then says why."""
        super().__init__(parent)
        self._loadout = loadout
        self._existing = set(existing_profiles)
        self._picks = nexus_picks
        self._nexus_note = nexus_note
        self.setWindowTitle(self.tr("Install loadout"))
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        head = QLabel(f"<b>{_esc(loadout.name)}</b><br>"
                      + self.tr("by {0}").format(_esc(loadout.by)))
        head.setTextFormat(head.textFormat().RichText)
        lay.addWidget(head)
        if loadout.summary:
            s = QLabel(loadout.summary)
            s.setWordWrap(True)
            lay.addWidget(s)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        lay.addWidget(self._summary)

        # Optional items: ticked by default, so the loadout installs as published.
        self._optional: list[tuple[QCheckBox, int]] = []
        optional = [i for i in loadout.items if i.optional]
        if optional:
            lay.addWidget(QLabel(self.tr("Optional items (untick to leave out):")))
            host = QWidget()
            hl = QVBoxLayout(host)
            hl.setContentsMargins(4, 0, 4, 0)
            for item in optional:
                where = "" if item.hosted else self.tr(" (hosted elsewhere)")
                box = QCheckBox(f"{item.name}{where}")
                box.setChecked(True)
                if item.note:
                    box.setToolTip(item.note)
                box.toggled.connect(lambda *_: self._refresh())
                hl.addWidget(box)
                self._optional.append((box, item.order_index))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setMaximumHeight(160)
            scroll.setWidget(host)
            lay.addWidget(scroll)

        # Items hosted on Nexus: shown with the file chosen for each, ticked by default.
        self._nexus: list[tuple[QCheckBox, int]] = []
        if self._picks is not None:
            usable = [p for p in self._picks.values() if p.usable]
            unusable = [p for p in self._picks.values() if not p.usable]
            if usable or unusable:
                lay.addWidget(QLabel(self.tr(
                    "Download from Nexus (untick any you would rather install yourself):")))
                host = QWidget()
                hl = QVBoxLayout(host)
                hl.setContentsMargins(4, 0, 4, 0)
                for pick in usable:
                    f = pick.file
                    text = "{0} → {1}".format(
                        pick.name, getattr(f, "file_name", "") or getattr(f, "name", ""))
                    version = getattr(f, "version", "")
                    if version:
                        text += f" ({version})"
                    if pick.ambiguous:
                        text += self.tr(" — newest of several main files")
                    box = QCheckBox(text)
                    box.setChecked(True)
                    box.toggled.connect(lambda *_: self._refresh())
                    hl.addWidget(box)
                    self._nexus.append((box, pick.order_index))
                for pick in unusable:
                    lbl = QLabel(self.tr("{0} — can't be chosen automatically: {1}").format(
                        pick.name, pick.reason))
                    lbl.setWordWrap(True)
                    hl.addWidget(lbl)
                nscroll = QScrollArea()
                nscroll.setWidgetResizable(True)
                nscroll.setMaximumHeight(200)
                nscroll.setWidget(host)
                lay.addWidget(nscroll)

        # Where it goes: a new profile, or on top of what an earlier run left.
        base = default_profile_name(loadout)
        self._base = base
        self._new_name = free_profile_name(base, self._existing)
        self._continue_radio: QRadioButton | None = None
        self._new_radio: QRadioButton | None = None
        self._name_edit: QLineEdit | None = None
        if base in self._existing:
            self._continue_radio = QRadioButton(self.tr(
                "Continue in the existing profile “{0}” (keeps what is installed)").format(base))
            self._continue_radio.setChecked(True)
            lay.addWidget(self._continue_radio)
            self._new_radio = QRadioButton(
                self.tr("Create a new profile “{0}”").format(self._new_name))
            lay.addWidget(self._new_radio)
        else:
            lay.addWidget(QLabel(self.tr("Profile name:")))
            self._name_edit = QLineEdit(self._new_name)
            lay.addWidget(self._name_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(self.tr("Install"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._refresh()

    # ---- state -------------------------------------------------------------
    def plan(self) -> LoadoutPlan:
        chosen = {idx for box, idx in self._optional if box.isChecked()}
        nexus = ({idx for box, idx in self._nexus if box.isChecked()}
                 if self._picks is not None else None)
        return plan_install(self._loadout, include_optional=chosen, nexus_items=nexus)

    def nexus_picks(self) -> "dict[int, NexusPick]":
        """The chosen Nexus files, for the items that stay in the plan."""
        return dict(self._picks or {})

    def continue_existing(self) -> bool:
        return bool(self._continue_radio is not None and self._continue_radio.isChecked())

    def profile_name(self) -> str:
        if self.continue_existing():
            return self._base
        if self._name_edit is not None:
            typed = re.sub(r"[^\w\s\-]", "", self._name_edit.text()).strip().replace(" ", "_")
            return free_profile_name((typed or self._base)[:64], self._existing)
        return self._new_name

    def _refresh(self):
        p = self.plan()
        lines = [self.tr("{0} mod(s) from Modhold — {1} file(s), {2} to download.").format(
            len(p.installable), p.file_count, format_size(p.download_bytes))]
        if p.nexus:
            lines.append(self.tr(
                "{0} item(s) will be downloaded from Nexus (Premium).").format(len(p.nexus)))
        if p.external:
            lines.append(self.tr(
                "{0} item(s) are hosted elsewhere and can't be downloaded here. "
                "They are listed when the install finishes, with their links.").format(
                    len(p.external)))
        if self._picks is None and self._nexus_note and any(
                i.nexus_ref for i in self._loadout.items):
            lines.append(self._nexus_note)
        if p.gated:
            lines.append(self.tr(
                "{0} item(s) are beta releases. They download only if your Modhold "
                "account has access.").format(len(p.gated)))
        if p.skipped_optional:
            lines.append(self.tr("{0} optional item(s) left out.").format(
                len(p.skipped_optional)))
        if not self._loadout.signed_in:
            lines.append(self.tr("Connect your Modhold account before installing."))
        self._summary.setText("\n".join(lines))


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

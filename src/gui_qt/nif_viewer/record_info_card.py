"""
record_info_card.py
RecordInfoCard -- a flat, app-styled overlay card showing the ARMO record
data (Utils.plugins.armor_record_details.ArmorInfo) for the mesh currently
selected in the NIF Viewer or Character tab.

Non-modal, parented on the viewport's own QStackedWidget (not on MeshViewport
itself) so it floats above whichever page is current without being one of
the stack's pages -- QOpenGLWidget explicitly supports ordinary widget
siblings overlapping it, so no changes are needed in gl_viewport.py.

Only fields derivable from a static plugin record are shown; a live
placed-reference's own Ref Form ID / world Position have no equivalent here
and are intentionally not part of this card.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QFrame, QLabel, QSizePolicy, QWidget

from gui_qt.nif_viewer.armor_variant_picker import ArmorVariantPickerDialog
from gui_qt.theme.theme_qt import _c, active_palette

_MARGIN = 12
_DASH = "—"


class ClickableLabel(QLabel):
    """A QLabel that reports clicks -- used for the Character tab's equipment
    slot rows (pick one as "current" for the record info card) and for this
    card's own "+N more" line (open the variant picker)."""

    def __init__(self, text: str = "", on_click=None):
        super().__init__(text)
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if self._on_click is not None:
            self._on_click()
        super().mousePressEvent(event)


class RecordInfoCard(QFrame):
    """set_data([], ...) hides the card; a non-empty ArmorInfo list shows it,
    with the first entry (the load-order winner) active by default -- click
    the "+N more" line to browse the rest and pick a different one."""

    def __init__(self, parent: "QWidget | None" = None):
        super().__init__(parent)
        self.setObjectName("RecordInfoCard")
        self._apply_style()
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        self._infos: list = []
        self._active_index = 0
        self._textures_found = 0
        self._textures_missing = 0
        self._slot_labels: "dict[int, str] | None" = None

        form = QFormLayout(self)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(4)
        form.setLabelAlignment(Qt.AlignLeft)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)

        self._name = QLabel()
        self._name.setStyleSheet("font-weight:600;")
        form.addRow(self._name)
        self._extra = ClickableLabel(on_click=self._open_picker)
        p = active_palette()
        self._extra.setStyleSheet(f"color:{_c(p, 'ACCENT')}; text-decoration:underline;")
        form.addRow(self._extra)

        self._rows: dict[str, QLabel] = {}
        for key, label in (
            ("editor_id", self.tr("Editor ID")),
            ("formid", self.tr("Base Form ID")),
            ("base_type", self.tr("Base Type")),
            ("textures", self.tr("Textures")),
            ("keywords", self.tr("Keywords")),
            ("armor_type", self.tr("Armor Type")),
            ("value", self.tr("Value")),
            ("weight", self.tr("Weight")),
            ("slots", self.tr("Equip Slots")),
            ("armature", self.tr("Armor Addon")),
            ("enabled", self.tr("Is Enabled")),
        ):
            value = QLabel()
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(f"{label}:", value)
            self._rows[key] = value

        self.hide()

    def _apply_style(self):
        p = active_palette()
        self.setStyleSheet(
            f"#RecordInfoCard {{ background:{_c(p, 'BG_PANEL')};"
            f" border:1px solid {_c(p, 'BORDER')}; border-radius:8px; }}"
            f" #RecordInfoCard QLabel {{ color:{_c(p, 'TEXT_MAIN')}; background:transparent; }}")

    # -- anchoring ------------------------------------------------------------------------
    def attach(self, container: QWidget):
        """Anchor to *container*'s bottom-right corner, tracking its resizes."""
        container.installEventFilter(self)
        self._reposition()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

    def _reposition(self):
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        self.move(max(0, parent.width() - self.width() - _MARGIN),
                  max(0, parent.height() - self.height() - _MARGIN))
        self.raise_()

    # -- content ----------------------------------------------------------------------------
    def set_data(self, infos: list, textures_found: int = 0, textures_missing: int = 0,
                 slot_labels: "dict[int, str] | None" = None):
        """*infos* is every ArmorInfo sharing the selected mesh, in load-order
        priority order (winner first); an empty list hides the card. The
        first entry is shown by default -- click the "+N more" line to pick
        a different one from the full set (Utils.plugins.armor_record_details
        can't tell which one you actually meant, since they're genuinely
        distinct records that happen to share a model). *slot_labels*
        optionally maps a slot number to a game-specific friendly name
        (Utils.nif.character's GameProfile.slot_groups, reversed)."""
        self._infos = infos
        self._active_index = 0
        self._textures_found = textures_found
        self._textures_missing = textures_missing
        self._slot_labels = slot_labels
        if not infos:
            self.hide()
            return
        self._render()
        self.show()
        self._reposition()

    def _open_picker(self):
        if len(self._infos) <= 1:
            return
        dlg = ArmorVariantPickerDialog(self._infos, self)
        if dlg.exec() == QDialog.Accepted:
            chosen = dlg.chosen_index()
            if chosen is not None:
                self._active_index = chosen
                self._render()

    def _render(self):
        info = self._infos[self._active_index]
        extra_count = len(self._infos) - 1

        self._name.setText(info.name or info.editor_id or _DASH)
        self._extra.setText(
            self.tr("+{0} more item(s) share this mesh").format(extra_count)
            if extra_count > 0 else "")
        self._extra.setVisible(extra_count > 0)

        self._rows["editor_id"].setText(info.editor_id or _DASH)
        self._rows["formid"].setText(info.display_formid or _DASH)
        self._rows["base_type"].setText(self.tr("Armor (ARMO)"))
        if self._textures_found or self._textures_missing:
            text = self.tr("{0} found").format(self._textures_found)
            if self._textures_missing:
                text += self.tr(", {0} missing").format(self._textures_missing)
        else:
            text = _DASH
        self._rows["textures"].setText(text)
        kw_count = len(info.keyword_labels)
        kw_label = self.tr("{0}").format(kw_count) if kw_count else "0"
        self._rows["keywords"].setText(kw_label)
        self._rows["keywords"].setToolTip(", ".join(info.keyword_labels))
        self._rows["armor_type"].setText(info.armor_type or _DASH)
        self._rows["value"].setText(str(info.value))
        self._rows["weight"].setText(f"{info.weight:g}")
        self._rows["slots"].setText(_format_slots(info.slots, self._slot_labels))
        self._rows["armature"].setText(str(len(info.arma_keys)))
        self._rows["enabled"].setText(self.tr("Yes") if info.enabled else self.tr("No"))

        self._reposition()


def _format_slots(slots: "frozenset[int]", slot_labels: "dict[int, str] | None") -> str:
    if not slots:
        return _DASH
    labels = slot_labels or {}
    parts = []
    for s in sorted(slots):
        name = labels.get(s)
        parts.append(f"{s} ({name})" if name else str(s))
    return ", ".join(parts)

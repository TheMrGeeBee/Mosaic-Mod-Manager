"""
record_info_card.py
RecordInfoCard -- a flat, app-styled overlay card showing the record data
(Utils.plugins.record_info.RecordInfo, any handled type -- Armor, Weapon,
Potion, a generic Static, ...) for the mesh currently selected in the NIF
Viewer or Character tab.

Non-modal, parented on the viewport's own QStackedWidget (not on MeshViewport
itself) so it floats above whichever page is current without being one of
the stack's pages -- QOpenGLWidget explicitly supports ordinary widget
siblings overlapping it, so no changes are needed in gl_viewport.py.

Only fields derivable from a static plugin record are shown; a live
placed-reference's own Ref Form ID / world Position have no equivalent here
and are intentionally not part of this card.

Fixed rows (Editor ID/Base Form ID/Base Type/Textures/Keywords/Value/Weight/
Is Enabled) are common to every type; everything type-specific (Armor Type/
Equip Slots/Armor Addon for ARMO, Weapon Type/Damage for WEAP, Soul Size/
Capacity for SLGM, ...) comes from RecordInfo.extra_fields and is rendered
into a small nested QFormLayout rebuilt on every _render() call, rather than
every type's fields being hardcoded rows here.
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
    """set_data([], ...) hides the card; a non-empty RecordInfo list shows
    it, with the first entry (the load-order winner) active by default --
    click the "+N more" line to browse the rest and pick a different one."""

    def __init__(self, parent: "QWidget | None" = None):
        super().__init__(parent)
        self.setObjectName("RecordInfoCard")
        self._apply_style()
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        self._infos: list = []
        self._active_index = 0
        self._textures_found = 0
        self._textures_missing = 0

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
            ("value", self.tr("Value")),
            ("weight", self.tr("Weight")),
        ):
            value = QLabel()
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(f"{label}:", value)
            self._rows[key] = value

        # Type-specific rows (Armor Type/Equip Slots/Armor Addon, Weapon
        # Type/Damage, Soul Size/Capacity, ...) -- rebuilt per record from
        # RecordInfo.extra_fields, since different types need different rows.
        self._extra_container = QWidget()
        self._extra_container.setStyleSheet("background:transparent;")
        self._extra_layout = QFormLayout(self._extra_container)
        self._extra_layout.setContentsMargins(0, 0, 0, 0)
        self._extra_layout.setSpacing(4)
        self._extra_layout.setLabelAlignment(Qt.AlignLeft)
        self._extra_layout.setRowWrapPolicy(QFormLayout.DontWrapRows)
        form.addRow(self._extra_container)

        enabled_value = QLabel()
        enabled_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(f"{self.tr('Is Enabled')}:", enabled_value)
        self._rows["enabled"] = enabled_value

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
    def set_data(self, infos: list, textures_found: int = 0, textures_missing: int = 0):
        """*infos* is every RecordInfo sharing the selected mesh, in
        load-order priority order (winner first); an empty list hides the
        card. The first entry is shown by default -- click the "+N more"
        line to pick a different one from the full set (a mesh shared by
        several genuinely distinct records has no single "right" one to
        show)."""
        self._infos = infos
        self._active_index = 0
        self._textures_found = textures_found
        self._textures_missing = textures_missing
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
        self._rows["base_type"].setText(info.base_type_label or _DASH)
        if self._textures_found or self._textures_missing:
            text = self.tr("{0} found").format(self._textures_found)
            if self._textures_missing:
                text += self.tr(", {0} missing").format(self._textures_missing)
        else:
            text = _DASH
        self._rows["textures"].setText(text)
        kw_count = len(info.keyword_labels)
        self._rows["keywords"].setText(str(kw_count))
        self._rows["keywords"].setToolTip(", ".join(info.keyword_labels))
        self._rows["value"].setText(str(info.value) if info.value is not None else _DASH)
        self._rows["weight"].setText(f"{info.weight:g}" if info.weight is not None else _DASH)
        self._rows["enabled"].setText(self.tr("Yes") if info.enabled else self.tr("No"))

        while self._extra_layout.rowCount():
            self._extra_layout.removeRow(0)
        for label, text in info.extra_fields:
            value = QLabel(text or _DASH)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._extra_layout.addRow(f"{label}:", value)
        self._extra_container.setVisible(bool(info.extra_fields))

        self._reposition()

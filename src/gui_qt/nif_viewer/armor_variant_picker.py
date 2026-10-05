"""
armor_variant_picker.py
ArmorVariantPickerDialog -- pick one ArmorInfo (Utils.plugins.armor_record_
details) out of a list that all share the same mesh, e.g. the 100+ enchanted
variants of a base armor piece that all reuse one model. Mirrors
character_view.PickMeshDialog's established "search + list + Ok/Cancel"
picker convention in this app, minus the background-worker scan (the data
here is already fully resolved in memory, nothing to compute).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

_COLUMNS = ("Name", "Editor ID", "Base Form ID")


class ArmorVariantPickerDialog(QDialog):
    def __init__(self, infos: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Choose a variant"))
        self.resize(640, 440)             # resizable -- no setFixedSize, same as PickMeshDialog
        self._infos = infos
        self._chosen_index: "int | None" = None

        v = QVBoxLayout(self)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search by name, editor ID, or form ID…"))
        self._search.setClearButtonEnabled(True)
        v.addWidget(self._search)

        self._table = QTableWidget(len(infos), len(_COLUMNS))
        self._table.setHorizontalHeaderLabels([self.tr(c) for c in _COLUMNS])
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for row, info in enumerate(infos):
            name_item = QTableWidgetItem(info.name or info.editor_id or "—")
            name_item.setData(Qt.UserRole, row)       # index into *infos* -- stable across sort/filter
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, QTableWidgetItem(info.editor_id or "—"))
            self._table.setItem(row, 2, QTableWidgetItem(info.display_formid or "—"))
        self._table.setSortingEnabled(True)           # enabled AFTER population, standard Qt convention
        v.addWidget(self._table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self._ok = buttons.button(QDialogButtonBox.Ok)
        self._ok.setEnabled(False)

        self._search.textChanged.connect(self._apply_filter)
        self._table.itemDoubleClicked.connect(lambda _i: self._accept())
        self._table.itemSelectionChanged.connect(
            lambda: self._ok.setEnabled(bool(self._table.selectedItems())))

        if infos:
            self._table.selectRow(0)

    def _apply_filter(self, text: str):
        text = text.strip().lower()
        for row in range(self._table.rowCount()):
            if not text:
                self._table.setRowHidden(row, False)
                continue
            hay = " ".join(self._table.item(row, c).text().lower() for c in range(len(_COLUMNS)))
            self._table.setRowHidden(row, text not in hay)

    def _accept(self):
        items = self._table.selectedItems()
        if not items:
            return
        row = items[0].row()
        self._chosen_index = self._table.item(row, 0).data(Qt.UserRole)
        self.accept()

    def chosen_index(self) -> "int | None":
        """The index into the *infos* list this dialog was built from, or
        None if cancelled/nothing selected."""
        return self._chosen_index

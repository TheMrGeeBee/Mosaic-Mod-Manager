"""Pick which files of a Modhold mod to install (main file plus optional add-ons)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from Modhold.browse_util import default_file_selection, format_size


class ModholdFileChooser(QDialog):
    """Modal list of a mod's files. Main files start checked, optionals do not.

    With ``single=True`` it is a one-choice picker (Change Version): the installed
    file (``installed_id``) is marked and pre-selected, and Install picks one file."""

    def __init__(self, mod_name: str, files: list[dict], parent: QWidget | None = None,
                 single: bool = False, installed_id: int = 0):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Choose version") if single else self.tr("Choose files"))
        self.setMinimumWidth(480)
        self._files = files
        self._boxes: list[tuple[QAbstractButton, dict]] = []
        self._group = QButtonGroup(self) if single else None
        if single:
            ids = {f.get("id") for f in files}
            defaults = {installed_id} if installed_id in ids else {files[0].get("id")} if files else set()
        else:
            defaults = {f.get("id") for f in default_file_selection(files)}

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(self.tr("Files for {0}").format(mod_name)))
        for f in files:
            kind = self.tr("main") if f.get("kind") == "main" else self.tr("optional")
            label = f.get("label") or f.get("name") or ""
            text = "{0} — {1} ({2}, {3})".format(
                label, f.get("version") or "?", kind, format_size(f.get("size") or 0))
            if single and f.get("id") == installed_id:
                text += self.tr(" — installed")
            box = QRadioButton(text) if single else QCheckBox(text)
            if self._group is not None:
                self._group.addButton(box)
            box.setChecked(f.get("id") in defaults)
            box.setToolTip(str(f.get("name") or ""))
            lay.addWidget(box)
            self._boxes.append((box, f))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(self.tr("Install"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def selected_files(self) -> list[dict]:
        return [f for box, f in self._boxes if box.isChecked()]

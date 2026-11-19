"""
openmw_settings_dialog.py
Small dialog for the OpenMW engine settings Mosaic knows about (see
Games.Morrowind.openmw_settings). Only the keys listed there are shown; saving
changes only the ones the user touched and leaves the rest of settings.cfg alone.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from Games.Morrowind.openmw_settings import (
    KNOWN_SETTINGS,
    SettingDef,
    get_value,
    read_settings,
    set_values,
)


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("true", "1", "yes", "on")


class OpenMWSettingsDialog(QDialog):
    def __init__(self, settings_path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("OpenMW settings"))
        self._path = Path(settings_path)
        self._values = read_settings(self._path)
        self._widgets: dict[SettingDef, object] = {}

        lay = QVBoxLayout(self)
        note = QLabel(self.tr(
            "Edits {0}. Only the settings below are changed; everything else in the "
            "file stays as it is. A backup is made before the first change.").format(self._path))
        note.setWordWrap(True)
        lay.addWidget(note)

        form = QFormLayout()
        lay.addLayout(form)
        for d in KNOWN_SETTINGS:
            w = self._make_widget(d)
            self._widgets[d] = w
            form.addRow(f"{d.label}  [{d.section}]", w)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _make_widget(self, d: SettingDef):
        cur = get_value(self._values, d)
        if d.kind == "bool":
            w = QCheckBox()
            w.setChecked(_truthy(cur))
            return w
        if d.kind == "choice":
            w = QComboBox()
            for value, label in d.choices:
                w.addItem(label, value)
            i = w.findData(cur)
            w.setCurrentIndex(i if i >= 0 else 0)
            return w
        w = QDoubleSpinBox()
        w.setRange(d.minimum, d.maximum)
        w.setDecimals(2)
        try:
            w.setValue(float(cur))
        except ValueError:
            w.setValue(float(d.default))
        return w

    def _current(self, d: SettingDef) -> str:
        w = self._widgets[d]
        if d.kind == "bool":
            return "true" if w.isChecked() else "false"
        if d.kind == "choice":
            return str(w.currentData())
        return f"{w.value():g}"

    def _save(self):
        changes: dict[tuple[str, str], str] = {}
        for d in KNOWN_SETTINGS:
            new = self._current(d)
            have = self._values.get((d.section.lower(), d.key.lower()))
            if have is None:
                # Not in the file: only write when it differs from OpenMW's default.
                if new.lower() != d.default.lower() and not _same_number(new, d.default):
                    changes[(d.section, d.key)] = new
            elif new.lower() != have.lower() and not _same_number(new, have):
                changes[(d.section, d.key)] = new
        try:
            set_values(self._path, changes)
        except OSError as exc:
            QMessageBox.warning(self, self.windowTitle(),
                                self.tr("Could not write {0}:\n{1}").format(self._path, exc))
            return
        self.accept()


def _same_number(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def open_openmw_settings(parent, game) -> None:
    """Open the dialog for *game*'s settings.cfg (next to its openmw.cfg)."""
    from Games.Morrowind.openmw_settings import settings_cfg_path
    path = settings_cfg_path(game.get_openmw_cfg_path())
    OpenMWSettingsDialog(path, parent).exec()

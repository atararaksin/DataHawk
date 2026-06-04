"""Reusable driver selection widget."""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit
from PySide6.QtCore import Signal

from datahawk.storage import list_drivers

_NEW_DRIVER = "➕ Add new driver..."


class DriverSelector(QWidget):
    """Combo box with existing drivers + 'Add new' option with inline name input."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Driver:"))
        self._combo = QComboBox()
        self._combo.addItem("")  # blank placeholder
        for d in list_drivers():
            self._combo.addItem(d)
        self._combo.addItem(_NEW_DRIVER)
        self._combo.setCurrentIndex(0)
        self._combo.currentTextChanged.connect(self._on_combo_changed)
        row.addWidget(self._combo)
        layout.addLayout(row)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Driver name")
        self._name_input.setVisible(False)
        self._name_input.textChanged.connect(lambda _: self.changed.emit())
        layout.addWidget(self._name_input)

    def _on_combo_changed(self, text: str):
        self._name_input.setVisible(text == _NEW_DRIVER)
        self.changed.emit()

    @property
    def driver_name(self) -> str:
        if self._combo.currentText() == _NEW_DRIVER:
            return self._name_input.text().strip()
        return self._combo.currentText().strip()

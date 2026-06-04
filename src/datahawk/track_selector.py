"""Reusable track selection widget."""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit
from PySide6.QtCore import Signal

from datahawk.storage import list_tracks

_NEW_TRACK = "➕ Add new track..."


class TrackSelector(QWidget):
    """Combo box with existing tracks + 'Add new' option with inline name input."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Track:"))
        self._combo = QComboBox()
        self._combo.addItem("")  # blank placeholder
        for t in list_tracks():
            self._combo.addItem(t)
        self._combo.addItem(_NEW_TRACK)
        self._combo.setCurrentIndex(0)
        self._combo.currentTextChanged.connect(self._on_combo_changed)
        row.addWidget(self._combo)
        layout.addLayout(row)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Track name")
        self._name_input.setVisible(False)
        self._name_input.textChanged.connect(lambda _: self.changed.emit())
        layout.addWidget(self._name_input)

    def _on_combo_changed(self, text: str):
        self._name_input.setVisible(text == _NEW_TRACK)
        self.changed.emit()

    @property
    def is_new_track(self) -> bool:
        return self._combo.currentText() == _NEW_TRACK

    @property
    def track_name(self) -> str:
        if self.is_new_track:
            return self._name_input.text().strip()
        return self._combo.currentText().strip()

"""Track selection dialog for session import."""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QLineEdit, QDialogButtonBox,
)

from datahawk.storage import list_tracks

_NEW_TRACK = "➕ Add new track..."


class TrackSelectionDialog(QDialog):
    """Dialog to select an existing track or create a new one."""

    def __init__(self, parent=None, *, title="Select Track"):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("Track:"))
        self._combo = QComboBox()
        tracks = list_tracks()
        for t in tracks:
            self._combo.addItem(t)
        self._combo.addItem(_NEW_TRACK)
        self._combo.currentTextChanged.connect(self._on_combo_changed)
        row.addWidget(self._combo)
        layout.addLayout(row)

        self._name_row = QHBoxLayout()
        self._name_row_label = QLabel("Name:")
        self._name_row.addWidget(self._name_row_label)
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Track name")
        self._name_row.addWidget(self._name_input)
        layout.addLayout(self._name_row)

        # Show/hide name input based on initial selection
        is_new = self._combo.currentText() == _NEW_TRACK
        self._name_row_label.setVisible(is_new)
        self._name_input.setVisible(is_new)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_combo_changed(self, text: str):
        is_new = text == _NEW_TRACK
        self._name_row_label.setVisible(is_new)
        self._name_input.setVisible(is_new)

    @property
    def is_new_track(self) -> bool:
        return self._combo.currentText() == _NEW_TRACK

    @property
    def track_name(self) -> str:
        if self.is_new_track:
            return self._name_input.text().strip()
        return self._combo.currentText()

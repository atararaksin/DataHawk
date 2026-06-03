"""Track selection dialog for GoPro session import."""

from PySide6.QtWidgets import QDialog, QVBoxLayout, QDialogButtonBox

from datahawk.track_selector import TrackSelector


class TrackSelectionDialog(QDialog):
    """Dialog wrapping TrackSelector with OK/Cancel buttons."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Track")
        layout = QVBoxLayout(self)

        self._selector = TrackSelector()
        layout.addWidget(self._selector)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def is_new_track(self) -> bool:
        return self._selector.is_new_track

    @property
    def track_name(self) -> str:
        return self._selector.track_name

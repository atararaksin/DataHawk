"""Import sessions dialog for MyChron 5 device."""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
)

from datahawk.mychron import check_device, list_sessions, Session


class _ListWorker(QThread):
    """Background thread to list sessions from device."""
    finished = Signal(list)
    error = Signal(str)

    def run(self):
        try:
            if not check_device():
                self.error.emit("MyChron 5 not found.\nMake sure you're connected to its WiFi network.")
                return
            sessions = list_sessions()
            self.finished.emit(sessions)
        except Exception as e:
            self.error.emit(str(e))


class ImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import from MyChron 5")
        self.setMinimumSize(700, 400)
        self._sessions: list[Session] = []

        layout = QVBoxLayout(self)

        # Status bar
        status_row = QHBoxLayout()
        self._status = QLabel("Connecting to device...")
        status_row.addWidget(self._status)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._load_sessions)
        status_row.addWidget(self._refresh_btn)
        layout.addLayout(status_row)

        # Sessions table
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["Name", "Date", "Time", "Laps", "Track"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._import_btn = QPushButton("Import Selected")
        self._import_btn.setEnabled(False)
        self._import_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._import_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._table.itemSelectionChanged.connect(
            lambda: self._import_btn.setEnabled(len(self._table.selectedItems()) > 0)
        )

        self._worker = None
        self._load_sessions()

    def _load_sessions(self):
        self._status.setText("Connecting to device...")
        self._refresh_btn.setEnabled(False)
        self._table.setRowCount(0)

        self._worker = _ListWorker()
        self._worker.finished.connect(self._on_sessions)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_sessions(self, sessions: list[Session]):
        self._sessions = sessions
        self._status.setText(f"{len(sessions)} sessions found")
        self._refresh_btn.setEnabled(True)

        self._table.setRowCount(len(sessions))
        for i, s in enumerate(sessions):
            self._table.setItem(i, 0, QTableWidgetItem(s.name))
            self._table.setItem(i, 1, QTableWidgetItem(s.date))
            self._table.setItem(i, 2, QTableWidgetItem(s.time))
            self._table.setItem(i, 3, QTableWidgetItem(s.laps))
            self._table.setItem(i, 4, QTableWidgetItem(s.track))

    def _on_error(self, msg: str):
        self._status.setText("Connection failed")
        self._refresh_btn.setEnabled(True)
        QMessageBox.warning(self, "Connection Error", msg)

    def selected_sessions(self) -> list[Session]:
        """Return sessions selected by the user."""
        rows = set(idx.row() for idx in self._table.selectedIndexes())
        return [self._sessions[r] for r in sorted(rows)]

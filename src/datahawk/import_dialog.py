"""Import sessions dialog for MyChron 5 device."""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QProgressBar,
)

from datahawk.mychron import check_device, list_sessions, download_session, Session
from datahawk.storage import get_or_create_device, save_session, get_imported_filenames


DEVICE_NAME = "MyChron 5"


class _ListWorker(QThread):
    finished = Signal(list)
    error = Signal(str)

    def run(self):
        try:
            if not check_device():
                self.error.emit("MyChron 5 not found.\nMake sure you're connected to its WiFi network.")
                return
            self.finished.emit(list_sessions())
        except Exception as e:
            self.error.emit(str(e))


class _DownloadWorker(QThread):
    progress = Signal(int, int)  # current, total
    finished = Signal(int)  # count imported
    error = Signal(str)

    def __init__(self, sessions: list[Session]):
        super().__init__()
        self._sessions = sessions

    def run(self):
        try:
            device_id = get_or_create_device(DEVICE_NAME)
            count = 0
            for i, s in enumerate(self._sessions):
                self.progress.emit(i + 1, len(self._sessions))
                data = download_session(s.name, expected_size=s.size)
                if data and len(data) > 200:
                    save_session(
                        device_id=device_id,
                        original_filename=s.name,
                        data=data,
                        date=s.date,
                        time=s.time,
                        laps=s.laps,
                        track=s.track,
                    )
                    count += 1
            self.finished.emit(count)
        except Exception as e:
            self.error.emit(str(e))


class ImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import from MyChron 5")
        self.setMinimumSize(700, 400)
        self._sessions: list[Session] = []
        self._imported_count = 0

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
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["Name", "Date", "Time", "Laps", "Track", "Status"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        # Progress bar (hidden initially)
        self._progress = QProgressBar()
        self._progress.hide()
        layout.addWidget(self._progress)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._import_btn = QPushButton("Import Selected")
        self._import_btn.setEnabled(False)
        self._import_btn.clicked.connect(self._do_import)
        btn_row.addWidget(self._import_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._table.itemSelectionChanged.connect(
            lambda: self._import_btn.setEnabled(len(self._table.selectedItems()) > 0)
        )

        self._worker = None
        self._dl_worker = None
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

        imported = get_imported_filenames()
        self._table.setRowCount(len(sessions))
        for i, s in enumerate(sessions):
            self._table.setItem(i, 0, QTableWidgetItem(s.name))
            self._table.setItem(i, 1, QTableWidgetItem(s.date))
            self._table.setItem(i, 2, QTableWidgetItem(s.time))
            self._table.setItem(i, 3, QTableWidgetItem(s.laps))
            self._table.setItem(i, 4, QTableWidgetItem(s.track))
            status = "✓ Imported" if s.name in imported else ""
            item = QTableWidgetItem(status)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 5, item)

    def _on_error(self, msg: str):
        self._status.setText("Connection failed")
        self._refresh_btn.setEnabled(True)
        QMessageBox.warning(self, "Connection Error", msg)

    def _do_import(self):
        rows = sorted(set(idx.row() for idx in self._table.selectedIndexes()))
        selected = [self._sessions[r] for r in rows]
        if not selected:
            return

        self._import_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._table.setEnabled(False)
        self._progress.setRange(0, len(selected))
        self._progress.setValue(0)
        self._progress.show()
        self._status.setText("Downloading...")

        self._dl_worker = _DownloadWorker(selected)
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.finished.connect(self._on_dl_done)
        self._dl_worker.error.connect(self._on_dl_error)
        self._dl_worker.start()

    def _on_dl_progress(self, current: int, total: int):
        self._progress.setValue(current)
        self._status.setText(f"Downloading {current}/{total}...")

    def _on_dl_done(self, count: int):
        self._imported_count = count
        self._status.setText(f"Imported {count} session(s)")
        self._progress.hide()
        self.accept()

    def _on_dl_error(self, msg: str):
        self._progress.hide()
        self._table.setEnabled(True)
        self._import_btn.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        QMessageBox.warning(self, "Download Error", msg)

    @property
    def imported_count(self) -> int:
        return self._imported_count

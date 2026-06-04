"""Import sessions dialog for MyChron 5 device."""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QProgressBar, QLineEdit,
)

from datahawk.source.mychron.mychron import check_device, list_sessions, download_session, Session
from datahawk.storage import save_session, get_imported_filenames, load_track, save_track
from datahawk.source.mychron.xrz_parser import parse_xrz
from datahawk.track_selector import TrackSelector


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
    progress = Signal(int, int)
    finished = Signal(int)
    error = Signal(str)

    def __init__(self, sessions: list[Session], driver: str, track_name: str):
        super().__init__()
        self._sessions = sessions
        self._driver = driver
        self._track_name = track_name

    def run(self):
        try:
            count = 0
            track_created = False
            for i, s in enumerate(self._sessions):
                self.progress.emit(i + 1, len(self._sessions))
                data = download_session(s.name, expected_size=s.size)
                if data and len(data) > 200:
                    # Create track from first session if it doesn't exist yet
                    if not track_created and not load_track(self._track_name):
                        try:
                            import tempfile
                            from pathlib import Path
                            from datahawk.session_processing import detect_sf_line, detect_master_lap
                            from datahawk.types import Track
                            tmp = Path(tempfile.mktemp(suffix='.xrz'))
                            tmp.write_bytes(data)
                            parsed = parse_xrz(tmp)
                            sf_line = detect_sf_line(parsed)
                            master_lap = detect_master_lap(parsed, sf_line)
                            save_track(Track(name=self._track_name, sf_line=sf_line, master_lap=master_lap))
                            tmp.unlink()
                        except Exception:
                            pass
                    track_created = True

                    save_session(
                        driver=self._driver,
                        filename=s.name,
                        data=data,
                        date=s.date,
                        time=s.time,
                        laps=s.laps,
                        track=self._track_name,
                        best_lap_time=None,
                        source_type="MyChron 5",
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

        # Driver name input
        driver_row = QHBoxLayout()
        driver_row.addWidget(QLabel("Driver:"))
        self._driver_input = QLineEdit()
        self._driver_input.setPlaceholderText("Enter driver name")
        driver_row.addWidget(self._driver_input)
        layout.addLayout(driver_row)

        # Track selection
        self._track_selector = TrackSelector()
        self._track_selector.changed.connect(self._update_import_btn)
        layout.addWidget(self._track_selector)

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

        # Progress bar
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

        self._table.itemSelectionChanged.connect(self._update_import_btn)
        self._driver_input.textChanged.connect(self._update_import_btn)

        self._worker = None
        self._dl_worker = None
        self._load_sessions()

    def _update_import_btn(self):
        has_selection = len(self._table.selectedItems()) > 0
        has_driver = bool(self._driver_input.text().strip())
        has_track = bool(self._track_selector.track_name)
        self._import_btn.setEnabled(has_selection and has_driver and has_track)

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
            status = "✓ Imported" if (s.name, s.date, s.time) in imported else ""
            item = QTableWidgetItem(status)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 5, item)

    def _on_error(self, msg: str):
        self._status.setText("Connection failed")
        self._refresh_btn.setEnabled(True)
        QMessageBox.warning(self, "Connection Error", msg)

    def _do_import(self):
        driver = self._driver_input.text().strip()
        if not driver:
            QMessageBox.warning(self, "Driver Required", "Please enter a driver name.")
            return

        rows = sorted(set(idx.row() for idx in self._table.selectedIndexes()))
        selected = [self._sessions[r] for r in rows]
        if not selected:
            return

        self._import_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._table.setEnabled(False)
        self._driver_input.setEnabled(False)
        self._progress.setRange(0, len(selected))
        self._progress.setValue(0)
        self._progress.show()
        self._status.setText("Downloading...")

        self._dl_worker = _DownloadWorker(selected, driver, self._track_selector.track_name)
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
        self._driver_input.setEnabled(True)
        QMessageBox.warning(self, "Download Error", msg)

    @property
    def imported_count(self) -> int:
        return self._imported_count

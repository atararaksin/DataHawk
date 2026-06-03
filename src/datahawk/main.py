"""DataHawk main application entry point."""

import sys
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QToolBar, QMessageBox,
    QFileDialog, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QDialogButtonBox,
)
from PySide6.QtGui import QAction

from datahawk.import_dialog import ImportDialog
from datahawk.session_browser import SessionBrowser
from datahawk.session_viewer import SessionViewer
from datahawk.storage import get_session_file_path, get_session_track_name, load_track, save_track


class _GoProDialog(QDialog):
    """Dialog to collect driver name and track for GoPro import."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open GoPro Video")
        layout = QVBoxLayout(self)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Driver:"))
        self.driver_input = QLineEdit()
        self.driver_input.setPlaceholderText("Driver name")
        row1.addWidget(self.driver_input)
        layout.addLayout(row1)

        from datahawk.track_selector import TrackSelector
        self.track_selector = TrackSelector()
        layout.addWidget(self.track_selector)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DataHawk")
        self.setMinimumSize(1024, 768)
        self._viewers: list[SessionViewer] = []

        # Toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        import_action = QAction("Import from MyChron", self)
        import_action.triggered.connect(self._on_import)
        toolbar.addAction(import_action)

        gopro_action = QAction("Open GoPro Video", self)
        gopro_action.triggered.connect(self._on_open_gopro)
        toolbar.addAction(gopro_action)

        # Session browser as central widget
        self._browser = SessionBrowser()
        self._browser.session_opened.connect(self._on_open_session)
        self.setCentralWidget(self._browser)

    def _on_import(self):
        dialog = ImportDialog(self)
        if dialog.exec():
            self.statusBar().showMessage(
                f"Imported {dialog.imported_count} session(s)", 5000
            )
            self._browser.refresh()

    def _on_open_gopro(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open GoPro Video", "", "Video (*.mp4 *.MP4 *.mov *.avi)")
        if not path:
            return

        dialog = _GoProDialog(self)
        if not dialog.exec():
            return
        driver = dialog.driver_input.text().strip() or "Unknown"
        track_name = dialog.track_selector.track_name
        if not track_name:
            QMessageBox.warning(self, "Error", "Track name cannot be empty.")
            return

        try:
            from datahawk.source.gopro.gopro_parser import parse_gopro
            from datahawk.session_processing import build_session, detect_sf_line, detect_master_lap
            from datahawk.types import Track

            parsed, _timo = parse_gopro(path)

            if dialog.track_selector.is_new_track:
                sf_line = detect_sf_line(parsed)
                master_lap = detect_master_lap(parsed, sf_line)
                track = Track(name=track_name, sf_line=sf_line, master_lap=master_lap)
                save_track(track)
            else:
                track = load_track(track_name)

            parsed.metadata.track = track.name
            parsed.metadata.date = ""

            session = build_session(parsed, track)
            viewer = SessionViewer(parsed, session, video_path=Path(path))
            viewer.setWindowTitle(f"DataHawk — {track.name} ({driver}) [GoPro]")
            viewer.show()
            self._viewers.append(viewer)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to process GoPro video:\n{e}")

    def _on_open_session(self, session_id: str):
        path = get_session_file_path(session_id)
        if not path or not path.exists():
            QMessageBox.warning(self, "Error", "Session file not found.")
            return

        track_name = get_session_track_name(session_id)
        if not track_name:
            QMessageBox.warning(self, "Error", "Session has no track assigned.")
            return

        track = load_track(track_name)
        if not track:
            QMessageBox.warning(self, "Error", f"Track '{track_name}' not found in database.")
            return

        from datahawk.source.mychron.xrz_parser import parse_xrz
        from datahawk.session_processing import build_session

        parsed = parse_xrz(path)
        session = build_session(parsed, track)
        viewer = SessionViewer(parsed, session)
        viewer.show()
        self._viewers.append(viewer)

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

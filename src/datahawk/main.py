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
from datahawk.session_viewer import SessionViewer, AnalysisWindow
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
        self._analysis_windows: dict[str, AnalysisWindow] = {}  # track_name -> window

        # Toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        import_action = QAction("Import from MyChron", self)
        import_action.triggered.connect(self._on_import)
        toolbar.addAction(import_action)

        video_action = QAction("Import from Video", self)
        video_action.triggered.connect(self._on_import_video)
        toolbar.addAction(video_action)

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

    def _on_import_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import from Video", "", "Video (*.mp4 *.MP4 *.mov *.avi)")
        if not path:
            return

        dialog = _GoProDialog(self)
        dialog.setWindowTitle("Import from Video")
        if not dialog.exec():
            return
        driver = dialog.driver_input.text().strip() or "Unknown"
        track_name = dialog.track_selector.track_name
        if not track_name:
            QMessageBox.warning(self, "Error", "Track name cannot be empty.")
            return

        try:
            from datahawk.source.gopro.gopro_parser import parse_gopro
            from datahawk.source.gopro.gopro_video_sync import is_gopro_video
            from datahawk.session_processing import build_session, detect_sf_line, detect_master_lap
            from datahawk.storage import save_session, serialize_source_session
            from datahawk.types import Track

            if not is_gopro_video(path):
                QMessageBox.warning(self, "Error",
                    "Unsupported camera or video lacks GPS telemetry")
                return

            parsed, _timo = parse_gopro(path)

            # Extract date/time from video file metadata
            video_file = Path(path)
            from datetime import datetime
            mtime = datetime.fromtimestamp(video_file.stat().st_mtime)
            parsed.metadata.date = mtime.strftime("%Y-%m-%d")
            parsed.metadata.time = mtime.strftime("%H:%M:%S")
            parsed.metadata.track = track_name

            # Create track if new
            if dialog.track_selector.is_new_track:
                sf_line = detect_sf_line(parsed)
                master_lap = detect_master_lap(parsed, sf_line)
                track = Track(name=track_name, sf_line=sf_line, master_lap=master_lap)
                save_track(track)
            else:
                track = load_track(track_name)

            # Save serialized SourceSession to storage
            data = serialize_source_session(parsed)
            session_built = build_session(parsed, track)
            save_session(
                driver=driver,
                filename=video_file.name,
                data=data,
                date=parsed.metadata.date,
                time=parsed.metadata.time,
                laps=str(len(session_built.laps)),
                track=track_name,
                best_lap_time=session_built.laps[session_built.best_lap_index].lap_time if session_built.laps else None,
                extension=".json",
            )

            # Open in analysis window
            window = self._get_or_create_analysis_window(track_name)
            window.add_session(parsed, session_built, video_path=Path(path), label=f"{driver} [GoPro]")
            window.show()
            window.raise_()
            self._browser.refresh()
        except ValueError as e:
            QMessageBox.warning(self, "Error", str(e))
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to import video:\n{e}")

    def _on_open_session(self, session_id: str):
        path = get_session_file_path(session_id)
        if not path or not path.exists():
            QMessageBox.warning(self, "Error", "Session file not found.")
            return

        track_name = get_session_track_name(session_id)
        if not track_name:
            QMessageBox.warning(self, "Error", "Session has no track assigned.")
            return

        from datahawk.session_processing import build_session

        # Check if already open in existing window
        window = self._analysis_windows.get(track_name)
        if window and window.has_session(session_id):
            window.raise_()
            return

        # Parse based on file extension
        if path.suffix == '.json':
            from datahawk.storage import deserialize_source_session
            parsed = deserialize_source_session(path.read_bytes())
        else:
            from datahawk.source.mychron.xrz_parser import parse_xrz
            parsed = parse_xrz(path)

        track = load_track(track_name)
        if not track:
            QMessageBox.warning(self, "Error", f"Track '{track_name}' not found in database.")
            return

        session = build_session(parsed, track)
        window = self._get_or_create_analysis_window(track_name)
        label = f"{session.date} {session.start_time}"
        # For GoPro sessions, also pass video path if original file exists
        video_path = None
        if path.suffix == '.json':
            # TODO: store original video path for auto-load
            pass
        window.add_session(parsed, session, video_path=video_path, label=label)
        window.show()
        window.raise_()

    def _get_or_create_analysis_window(self, track_name: str) -> AnalysisWindow:
        """Get existing or create new AnalysisWindow for a track."""
        window = self._analysis_windows.get(track_name)
        if window and window.isVisible():
            return window
        window = AnalysisWindow(track_name)
        self._analysis_windows[track_name] = window
        return window

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

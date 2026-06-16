"""DataHawk main application entry point."""

import logging
import sys
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QToolBar, QMessageBox,
    QFileDialog, QDialog, QVBoxLayout, QDialogButtonBox,
)
from PySide6.QtGui import QAction

# Configure logging for debug
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
)
# Quiet noisy loggers
logging.getLogger("PySide6").setLevel(logging.WARNING)

from datahawk.import_dialog import ImportDialog
from datahawk.session_browser import SessionBrowser
from datahawk.session_viewer import SessionViewer, AnalysisWindow
from datahawk.storage import get_session_file_path, get_session_track_name, get_session_source_type, get_session_video_info, load_track, save_track


class _GoProDialog(QDialog):
    """Dialog to collect driver name and track for video import."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import from Video")
        layout = QVBoxLayout(self)

        from datahawk.driver_selector import DriverSelector
        self.driver_selector = DriverSelector()
        layout.addWidget(self.driver_selector)

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
        if not dialog.exec():
            return
        driver = dialog.driver_selector.driver_name or "Unknown"
        track_name = dialog.track_selector.track_name
        if not track_name:
            QMessageBox.warning(self, "Error", "Track name cannot be empty.")
            return

        try:
            from datahawk.source.gopro.gopro_video_sync import is_gopro_video
            from datahawk.source.smartycam.smartycam_parser import is_smartycam_video
            from datahawk.source.gopro.gopro_parser import parse_gopro
            from datahawk.session_processing import build_session, detect_sf_line, detect_master_lap
            from datahawk.storage import save_session, serialize_source_session
            from datahawk.types import Track

            if is_smartycam_video(path):
                from datahawk.source.smartycam.smartycam_parser import parse_smartycam
                parsed = parse_smartycam(path)
                source_type = "SmartyCam"
            elif is_gopro_video(path):
                parsed, _timo = parse_gopro(path)
                source_type = "GoPro"
            else:
                QMessageBox.warning(self, "Error",
                    "Unsupported camera or video lacks GPS telemetry")
                return

            # Extract date/time from video file metadata
            video_file = Path(path)
            from datetime import datetime
            mtime = datetime.fromtimestamp(video_file.stat().st_mtime)
            parsed.metadata.date = mtime.strftime("%d/%m/%Y")
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
            sid = save_session(
                driver=driver,
                filename=video_file.name,
                data=data,
                date=parsed.metadata.date,
                time=parsed.metadata.time,
                laps=str(len(session_built.laps)),
                track=track_name,
                best_lap_time=session_built.laps[session_built.best_lap_index].lap_time if session_built.laps else None,
                source_type=source_type,
                extension=".json",
            )

            # Persist video path with offset 0 (video IS telemetry for GoPro)
            from datahawk.storage import save_session_video
            save_session_video(sid, str(video_file), 0.0)

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

        # Parse based on source type
        source_type = get_session_source_type(session_id)
        if source_type in ("GoPro", "SmartyCam"):
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

        # Load persisted video info
        video_path_str, video_offset = get_session_video_info(session_id)
        video_path = Path(video_path_str) if video_path_str and Path(video_path_str).exists() else None

        viewer = window.add_session(parsed, session, video_path=video_path,
                                    label=label, session_id=session_id, source_type=source_type)

        # Load video with persisted offset if available
        if video_path and video_offset is not None:
            is_mychron = source_type not in ("GoPro", "SmartyCam")
            viewer._video.load_video_with_offset(video_path, video_offset, is_mychron_session=is_mychron)
        elif video_path:
            # No persisted offset -- run auto-sync for mychron, offset=0 for others
            is_mychron = source_type not in ("GoPro", "SmartyCam")
            viewer._video.load_video(video_path, is_mychron_session=is_mychron)

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

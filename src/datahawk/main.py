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
from datahawk.storage import get_session_file_path


class _GoProDialog(QDialog):
    """Dialog to collect driver name and track name for GoPro import."""
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

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Track:"))
        self.track_input = QLineEdit()
        self.track_input.setPlaceholderText("Track name")
        row2.addWidget(self.track_input)
        layout.addLayout(row2)

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
        track = dialog.track_input.text().strip() or "Unknown"

        try:
            from datahawk.gopro_parser import parse_gopro
            from datahawk.session_processing import process_session

            parsed, timo = parse_gopro(path)
            parsed.metadata.track = track
            parsed.metadata.date = ""
            session = process_session(parsed)

            video_path = Path(path)
            viewer = SessionViewer(
                video_path, parsed_session=session, video_path=video_path,
                video_offset=-timo,
            )
            viewer.setWindowTitle(f"DataHawk — {track} ({driver}) [GoPro]")
            viewer.show()
            self._viewers.append(viewer)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to process GoPro video:\n{e}")

    def _on_open_session(self, session_id: str):
        path = get_session_file_path(session_id)
        if not path or not path.exists():
            QMessageBox.warning(self, "Error", "Session file not found.")
            return
        viewer = SessionViewer(path)
        viewer.show()
        self._viewers.append(viewer)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

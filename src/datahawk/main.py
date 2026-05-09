"""DataHawk main application entry point."""

import sys
from PySide6.QtWidgets import QApplication, QMainWindow, QToolBar, QMessageBox
from PySide6.QtGui import QAction

from datahawk.import_dialog import ImportDialog
from datahawk.session_browser import SessionBrowser
from datahawk.session_viewer import SessionViewer
from datahawk.storage import get_session_file_path


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

"""DataHawk main application entry point."""

import sys
from PySide6.QtWidgets import QApplication, QMainWindow, QToolBar
from PySide6.QtGui import QAction

from datahawk.import_dialog import ImportDialog
from datahawk.session_browser import SessionBrowser


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DataHawk")
        self.setMinimumSize(1024, 768)

        # Toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        import_action = QAction("Import from MyChron", self)
        import_action.triggered.connect(self._on_import)
        toolbar.addAction(import_action)

        # Session browser as central widget
        self._browser = SessionBrowser()
        self.setCentralWidget(self._browser)

    def _on_import(self):
        dialog = ImportDialog(self)
        if dialog.exec():
            self.statusBar().showMessage(
                f"Imported {dialog.imported_count} session(s)", 5000
            )
            self._browser.refresh()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

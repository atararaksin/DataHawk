"""DataHawk main application entry point."""

import sys
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QToolBar,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction

from datahawk.import_dialog import ImportDialog


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

        # Central placeholder
        label = QLabel("DataHawk — Go-kart Telemetry & Video Analysis")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(label)

    def _on_import(self):
        dialog = ImportDialog(self)
        if dialog.exec():
            sessions = dialog.selected_sessions()
            if sessions:
                names = ", ".join(s.name for s in sessions)
                self.statusBar().showMessage(f"Selected: {names}", 5000)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

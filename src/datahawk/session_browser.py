"""Session browser widget for the main window."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView,
)

from datahawk.storage import list_saved_sessions


class SessionBrowser(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["Name", "Date", "Time", "Laps", "Track", "Device"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        self.refresh()

    def refresh(self):
        sessions = list_saved_sessions()
        self._table.setRowCount(len(sessions))
        for i, s in enumerate(sessions):
            self._table.setItem(i, 0, QTableWidgetItem(s["original_filename"]))
            self._table.setItem(i, 1, QTableWidgetItem(s["date"] or ""))
            self._table.setItem(i, 2, QTableWidgetItem(s["time"] or ""))
            self._table.setItem(i, 3, QTableWidgetItem(s["laps"] or ""))
            self._table.setItem(i, 4, QTableWidgetItem(s["track"] or ""))
            self._table.setItem(i, 5, QTableWidgetItem(s["device_name"]))

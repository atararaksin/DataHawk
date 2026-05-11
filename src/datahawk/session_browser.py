"""Session browser widget for the main window."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView,
)

from datahawk.storage import list_saved_sessions


class SessionBrowser(QWidget):
    session_opened = Signal(str)  # emits session_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sessions: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(["Name", "Date", "Time", "Laps", "Track", "Best Lap", "Driver"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self._table)

        self.refresh()

    def refresh(self):
        self._sessions = list_saved_sessions()
        self._table.setRowCount(len(self._sessions))
        for i, s in enumerate(self._sessions):
            self._table.setItem(i, 0, QTableWidgetItem(s["original_filename"]))
            self._table.setItem(i, 1, QTableWidgetItem(s["date"] or ""))
            self._table.setItem(i, 2, QTableWidgetItem(s["time"] or ""))
            self._table.setItem(i, 3, QTableWidgetItem(s["laps"] or ""))
            self._table.setItem(i, 4, QTableWidgetItem(s["track"] or ""))
            blt = s.get("best_lap_time")
            blt_str = f"{blt:.3f}s" if blt else ""
            self._table.setItem(i, 5, QTableWidgetItem(blt_str))
            self._table.setItem(i, 6, QTableWidgetItem(s["driver"] or ""))

    def _on_double_click(self, index):
        row = index.row()
        if 0 <= row < len(self._sessions):
            self.session_opened.emit(self._sessions[row]["id"])

"""Session browser widget for the main window."""

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView, QMenu, QMessageBox,
)

from datahawk.storage import list_saved_sessions, delete_session


class SessionBrowser(QWidget):
    session_opened = Signal(str)  # emits session_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sessions: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget()
        self._table.setColumnCount(8)
        self._table.setHorizontalHeaderLabels(["Name", "Date", "Time", "Laps", "Track", "Best Lap", "Driver", "Type"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._table)

        self.refresh()

    def refresh(self):
        self._sessions = list_saved_sessions()
        self._table.setRowCount(len(self._sessions))
        for i, s in enumerate(self._sessions):
            self._table.setItem(i, 0, QTableWidgetItem(s["filename"]))
            self._table.setItem(i, 1, QTableWidgetItem(s["date"] or ""))
            self._table.setItem(i, 2, QTableWidgetItem(s["time"] or ""))
            self._table.setItem(i, 3, QTableWidgetItem(s["laps"] or ""))
            self._table.setItem(i, 4, QTableWidgetItem(s["track"] or ""))
            blt = s.get("best_lap_time")
            blt_str = f"{blt:.3f}s" if blt else ""
            self._table.setItem(i, 5, QTableWidgetItem(blt_str))
            self._table.setItem(i, 6, QTableWidgetItem(s["driver"] or ""))
            self._table.setItem(i, 7, QTableWidgetItem(s.get("source_type") or ""))

    def _on_double_click(self, index):
        row = index.row()
        if 0 <= row < len(self._sessions):
            self.session_opened.emit(self._sessions[row]["id"])

    def _on_context_menu(self, pos):
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._sessions):
            return
        menu = QMenu(self)
        delete_action = menu.addAction("Delete")
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == delete_action:
            session = self._sessions[row]
            reply = QMessageBox.question(
                self, "Delete Session",
                f"Delete '{session['filename']}'?",
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                delete_session(session["id"])
                self.refresh()

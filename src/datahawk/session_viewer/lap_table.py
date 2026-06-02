"""Lap table widget showing lap times and sector splits."""

from __future__ import annotations

import math

from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor

from datahawk.types import Session


class LapTable(QTableWidget):
    """Table displaying lap times and sector splits with fastest highlights."""

    lap_clicked = Signal(int)  # row index
    sector_clicked = Signal(int, int)  # row index, sector index

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionBehavior(QTableWidget.SelectItems)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setFixedWidth(400)
        font = self.font()
        font.setPointSize(font.pointSize() - 1)
        self.setFont(font)
        self.setCursor(Qt.PointingHandCursor)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cellClicked.connect(self._on_cell_clicked)

    def rebuild(self, session: Session):
        """Rebuild table contents from session data."""
        self.blockSignals(True)
        n_sectors = len(session.laps[0].sector_times) if session.laps else 0
        headers = ["Lap", "Time"] + [f"S{i+1}" for i in range(n_sectors)]
        self.setColumnCount(len(headers))
        self.setRowCount(len(session.laps))
        self.setHorizontalHeaderLabels(headers)

        purple = QBrush(QColor(128, 0, 128))
        best_lap_idx = session.reference_lap_index

        # Find fastest sector times
        best_sectors = [float('inf')] * n_sectors
        for lap in session.laps:
            for s, st in enumerate(lap.sector_times):
                if not math.isnan(st) and st < best_sectors[s]:
                    best_sectors[s] = st

        for i, lap in enumerate(session.laps):
            self.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            item = QTableWidgetItem(f"{lap.lap_time:.2f}")
            if i == best_lap_idx:
                item.setForeground(purple)
            self.setItem(i, 1, item)
            for s, st in enumerate(lap.sector_times):
                text = f"{st:.2f}" if not math.isnan(st) else "—"
                item = QTableWidgetItem(text)
                if not math.isnan(st) and st == best_sectors[s]:
                    item.setForeground(purple)
                self.setItem(i, 2 + s, item)

        self.resizeColumnsToContents()
        self.blockSignals(False)

    def select_cell(self, row: int, col: int):
        """Programmatically select a cell without emitting signals."""
        self.blockSignals(True)
        self.setCurrentCell(row, col)
        self.blockSignals(False)

    def _on_cell_clicked(self, row: int, col: int):
        if col < 2:
            self.lap_clicked.emit(row)
        else:
            self.sector_clicked.emit(row, col - 2)

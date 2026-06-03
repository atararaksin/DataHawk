"""Lap table widget showing lap times and sector splits."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor

from datahawk.types import Session


@dataclass
class LapTableLapClicked:
    lap_idx: int


@dataclass
class LapTableSectorClicked:
    lap_idx: int
    sector_idx: int


@dataclass
class LapTableRefClicked:
    lap_idx: int


class LapTable(QTableWidget):
    """Table displaying lap times and sector splits with fastest highlights."""

    lap_clicked = Signal(object)  # LapTableLapClicked
    sector_clicked = Signal(object)  # LapTableSectorClicked
    ref_clicked = Signal(object)  # LapTableRefClicked

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
        self._ref_row: int | None = None

    def rebuild(self, session: Session):
        """Rebuild table contents from session data."""
        self.blockSignals(True)
        n_sectors = len(session.laps[0].sector_times) if session.laps else 0
        headers = ["R", "Lap", "Time"] + [f"S{i+1}" for i in range(n_sectors)]
        self.setColumnCount(len(headers))
        self.setRowCount(len(session.laps))
        self.setHorizontalHeaderLabels(headers)

        purple = QBrush(QColor(128, 0, 128))
        best_lap_idx = session.best_lap_index

        # Find fastest sector times
        best_sectors = [float('inf')] * n_sectors
        for lap in session.laps:
            for s, st in enumerate(lap.sector_times):
                if not math.isnan(st) and st < best_sectors[s]:
                    best_sectors[s] = st

        for i, lap in enumerate(session.laps):
            # Ref column
            ref_item = QTableWidgetItem("R")
            ref_item.setTextAlignment(Qt.AlignCenter)
            ref_item.setForeground(QBrush(QColor(180, 180, 180)))
            self.setItem(i, 0, ref_item)

            self.setItem(i, 1, QTableWidgetItem(str(i + 1)))
            item = QTableWidgetItem(f"{lap.lap_time:.2f}")
            if i == best_lap_idx:
                item.setForeground(purple)
            self.setItem(i, 2, item)
            for s, st in enumerate(lap.sector_times):
                text = f"{st:.2f}" if not math.isnan(st) else "—"
                item = QTableWidgetItem(text)
                if not math.isnan(st) and st == best_sectors[s]:
                    item.setForeground(purple)
                self.setItem(i, 3 + s, item)

        self.setColumnWidth(0, 25)
        self.resizeColumnsToContents()
        self.setColumnWidth(0, 25)  # force narrow after resize
        self._apply_ref_highlight()
        self.blockSignals(False)

    def set_ref_row(self, row: int | None):
        """Set which row is the reference lap (red highlight)."""
        self._ref_row = row
        self._apply_ref_highlight()

    def _apply_ref_highlight(self):
        """Apply/remove red background on ref row."""
        red = QBrush(QColor(255, 200, 200))
        default = QBrush(QColor(255, 255, 255))
        for i in range(self.rowCount()):
            bg = red if i == self._ref_row else default
            for c in range(self.columnCount()):
                item = self.item(i, c)
                if item:
                    item.setBackground(bg)

    def select_sector(self, lap_idx: int, sector_idx: int):
        """Highlight the given sector cell for a lap."""
        col = 3 + sector_idx
        self.blockSignals(True)
        self.setCurrentCell(lap_idx, col)
        self.blockSignals(False)

    def _on_cell_clicked(self, row: int, col: int):
        if col == 0:
            self.ref_clicked.emit(LapTableRefClicked(lap_idx=row))
        elif col < 3:
            self.lap_clicked.emit(LapTableLapClicked(lap_idx=row))
        else:
            self.sector_clicked.emit(LapTableSectorClicked(lap_idx=row, sector_idx=col - 3))

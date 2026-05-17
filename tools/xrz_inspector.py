#!/usr/bin/env python3
"""XRZ GPS Inspector -- standalone utility for investigating GPS data in XRZ files.

Usage: python tools/xrz_inspector.py [path/to/file.xrz]

Controls:
  - Arrow keys: navigate table rows
  - "Reset Time" button: set lap start to current row's timestamp
  - Lap time updates as you navigate rows
"""

import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTableWidget, QTableWidgetItem,
    QSplitter, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QFileDialog, QHeaderView,
)
from PySide6.QtCore import Qt, Slot
import pyqtgraph as pg

from datahawk.xrz_parser import parse_xrz, _GPS_LAT_ID, _GPS_LON_ID, _GPS_SPEED_ID


class XrzInspector(QMainWindow):
    def __init__(self, xrz_path: str | None = None):
        super().__init__()
        self.setWindowTitle("XRZ GPS Inspector")
        self.resize(1400, 800)

        self._lap_start_time: float | None = None
        self._rows: list[tuple[float, float, float, float]] = []

        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        # Left panel: lap time + table
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)

        lap_bar = QHBoxLayout()
        self._btn_reset = QPushButton("Reset Time")
        self._btn_reset.clicked.connect(self._reset_lap_time)
        self._lbl_lap = QLabel("Lap: --")
        self._lbl_lap.setStyleSheet("font-size: 18px; font-weight: bold;")
        lap_bar.addWidget(self._btn_reset)
        lap_bar.addWidget(self._lbl_lap, 1)
        left_layout.addLayout(lap_bar)

        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["Time (s)", "Latitude", "Longitude", "Speed (km/h)"])
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.currentCellChanged.connect(self._on_row_changed)
        left_layout.addWidget(self._table)
        splitter.addWidget(left)

        # Right panel: map plot
        self._plot = pg.PlotWidget(title="GPS Track")
        self._plot.setAspectLocked(True)
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setLabel("bottom", "Longitude")
        self._plot.setLabel("left", "Latitude")
        self._trail = self._plot.plot([], [], pen=pg.mkPen("g", width=2))
        self._marker = self._plot.plot([], [], pen=None, symbol="o",
                                       symbolSize=12, symbolBrush="r")
        splitter.addWidget(self._plot)
        splitter.setSizes([500, 900])

        if xrz_path:
            self._load_file(xrz_path)
        else:
            self._prompt_open()

    def _prompt_open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open XRZ File", "", "XRZ Files (*.xrz)")
        if path:
            self._load_file(path)

    def _load_file(self, path: str):
        self.setWindowTitle(f"XRZ GPS Inspector — {Path(path).name}")
        session = parse_xrz(path)
        ch = session.channels

        lat_ch = ch.get(_GPS_LAT_ID)
        lon_ch = ch.get(_GPS_LON_ID)
        speed_ch = ch.get(_GPS_SPEED_ID)

        if not lat_ch or not lon_ch:
            self._lbl_lap.setText("No GPS data in file!")
            return

        self._rows = []
        for i in range(len(lat_ch.timestamps)):
            t = lat_ch.timestamps[i]
            lat = lat_ch.values[i]
            lon = lon_ch.values[i]
            spd = speed_ch.values[i] if speed_ch and i < len(speed_ch.values) else 0.0
            self._rows.append((t, lat, lon, spd))

        # Populate table
        self._table.setRowCount(len(self._rows))
        for i, (t, lat, lon, spd) in enumerate(self._rows):
            self._table.setItem(i, 0, QTableWidgetItem(f"{t:.3f}"))
            self._table.setItem(i, 1, QTableWidgetItem(f"{lat:.7f}"))
            self._table.setItem(i, 2, QTableWidgetItem(f"{lon:.7f}"))
            self._table.setItem(i, 3, QTableWidgetItem(f"{spd:.1f}"))

        # Draw trail
        lons = [r[2] for r in self._rows]
        lats = [r[1] for r in self._rows]
        self._trail.setData(lons, lats)
        self._plot.autoRange()

        self._table.selectRow(0)

    @Slot()
    def _reset_lap_time(self):
        row = self._table.currentRow()
        if 0 <= row < len(self._rows):
            self._lap_start_time = self._rows[row][0]
            self._lbl_lap.setText("Lap: 0.000s")

    @Slot(int, int, int, int)
    def _on_row_changed(self, row, col, prev_row, prev_col):
        if row < 0 or row >= len(self._rows):
            return
        t, lat, lon, spd = self._rows[row]

        # Update marker
        self._marker.setData([lon], [lat])

        # Update lap time
        if self._lap_start_time is not None:
            elapsed = t - self._lap_start_time
            self._lbl_lap.setText(f"Lap: {elapsed:.3f}s")


def main():
    app = QApplication(sys.argv)
    path = sys.argv[1] if len(sys.argv) > 1 else None
    win = XrzInspector(path)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

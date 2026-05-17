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
        self._xs: list[float] = []
        self._ys: list[float] = []

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

        # Right panel: map plot (Mercator-corrected)
        self._plot = pg.PlotWidget(title="GPS Track")
        self._plot.getViewBox().setAspectLocked(True, ratio=1.0)
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setLabel("bottom", "X (m)")
        self._plot.setLabel("left", "Y (m)")
        self._plot.setMouseEnabled(x=True, y=True)  # zoom & pan
        self._trail = self._plot.plot([], [], pen=pg.mkPen("g", width=2))
        self._marker = self._plot.plot([], [], pen=None, symbol="o",
                                       symbolSize=12, symbolBrush="r")
        splitter.addWidget(self._plot)
        splitter.setSizes([400, 1000])

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

        # Draw trail using ECEF->ENU projection for correct geometry
        # XRZ GPS offsets 16/20/24 are ECEF X/Y/Z in centimeters (not lat/lon!)
        import struct as _struct, zlib as _zlib
        from pathlib import Path as _Path
        raw_bytes = _Path(path).read_bytes()
        dec_bytes = _zlib.decompress(raw_bytes)

        ecef_points = []
        _pos = 0
        while True:
            _idx = dec_bytes.find(b'<hGPS\x00', _pos)
            if _idx == -1: break
            _bs = _idx + 12
            _x = _struct.unpack_from('<i', dec_bytes, _bs + 16)[0]
            _y = _struct.unpack_from('<i', dec_bytes, _bs + 20)[0]
            _z = _struct.unpack_from('<i', dec_bytes, _bs + 24)[0]
            _r = math.sqrt(_x**2 + _y**2 + _z**2)
            if 630000000 < _r < 650000000:
                ecef_points.append((_x, _y, _z))
            _pos = _idx + 12

        # Reference point -> geodetic for rotation matrix
        x0, y0, z0 = ecef_points[0]
        _a = 6378137.0 * 100  # WGS84 semi-major in cm
        _f = 1/298.257223563
        _e2 = 2*_f - _f*_f
        _lon0 = math.atan2(y0, x0)
        _p = math.sqrt(x0**2 + y0**2)
        _lat0 = math.atan2(z0, _p * (1 - _e2))
        for _ in range(10):
            _N = _a / math.sqrt(1 - _e2 * math.sin(_lat0)**2)
            _lat0 = math.atan2(z0 + _e2 * _N * math.sin(_lat0), _p)

        _sl, _cl = math.sin(_lat0), math.cos(_lat0)
        _sn, _cn = math.sin(_lon0), math.cos(_lon0)

        self._xs = []
        self._ys = []
        for (x, y, z) in ecef_points:
            dx = (x - x0) / 100.0
            dy = (y - y0) / 100.0
            dz = (z - z0) / 100.0
            self._xs.append(-_sn * dx + _cn * dy)          # East
            self._ys.append(-_sl*_cn*dx - _sl*_sn*dy + _cl*dz)  # North
        self._trail.setData(self._xs, self._ys)
        # Force equal scaling: make both axes span the same range
        x_min, x_max = min(self._xs), max(self._xs)
        y_min, y_max = min(self._ys), max(self._ys)
        x_range = x_max - x_min
        y_range = y_max - y_min
        max_range = max(x_range, y_range) * 1.1
        x_center = (x_min + x_max) / 2
        y_center = (y_min + y_max) / 2
        self._plot.setXRange(x_center - max_range/2, x_center + max_range/2)
        self._plot.setYRange(y_center - max_range/2, y_center + max_range/2)

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
        self._marker.setData([self._xs[row]], [self._ys[row]])

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

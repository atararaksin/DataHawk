#!/usr/bin/env python3
"""GoPro GPS Inspector -- standalone utility for investigating GPS data in GoPro MP4 files.

Usage: python tools/gopro_gps_inspector.py [path/to/file.MP4]

Controls:
  - Arrow keys: navigate table rows
  - "Reset Time" button: set lap start to current row's timestamp
  - Lap time updates as you navigate rows
"""

import sys
import math
import struct
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTableWidget, QTableWidgetItem,
    QSplitter, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QFileDialog, QHeaderView,
)
from PySide6.QtCore import Qt, Slot
import pyqtgraph as pg
import av


def extract_gps_from_gpmf(path: str) -> list[tuple[float, float, float, float, float]]:
    """Extract GPS5 data from GoPro GPMF stream.
    Returns list of (time_s, lat, lon, alt, speed_m_s)."""
    container = av.open(path)

    # Find GoPro MET stream
    met_stream = None
    for s in container.streams:
        if s.type == 'data' and hasattr(s, 'metadata'):
            if 'GoPro MET' in s.metadata.get('handler_name', ''):
                met_stream = s
                break
    if met_stream is None:
        container.close()
        return []

    tb = float(met_stream.time_base)
    results = []

    for packet in container.demux(met_stream):
        if packet.size == 0:
            continue
        pkt_time = packet.pts * tb if packet.pts else 0.0
        _extract_gps5_recursive(bytes(packet), pkt_time, results)

    container.close()
    return results


def _extract_gps5_recursive(data: bytes, pkt_time: float, results: list):
    """Recursively walk GPMF tree (DEVC->STRM->GPS5) to find GPS5 blocks."""
    scale = [1.0] * 5
    gps_fix = -1
    gps5_payload = None
    gps5_repeat = 0

    pos = 0
    while pos < len(data) - 8:
        fourcc = data[pos:pos+4]
        type_byte = data[pos+4]
        size = data[pos+5]
        repeat = struct.unpack('>H', data[pos+6:pos+8])[0]
        payload_size = size * repeat
        padded = (payload_size + 3) & ~3
        payload = data[pos+8:pos+8+payload_size]

        if fourcc == b'SCAL' and type_byte == ord('l') and repeat >= 5:
            for i in range(min(5, repeat)):
                scale[i] = struct.unpack_from('>i', payload, i * 4)[0]
        elif fourcc == b'GPSF' and type_byte == ord('L'):
            gps_fix = struct.unpack_from('>I', payload, 0)[0]
        elif fourcc == b'GPS5' and type_byte == ord('l'):
            gps5_payload = payload
            gps5_repeat = repeat
        elif type_byte == 0 and payload_size > 0:
            _extract_gps5_recursive(payload, pkt_time, results)

        pos += 8 + padded

    # Emit samples if GPS5 found at this level with valid fix
    if gps5_payload is not None and gps_fix >= 2:
        for i in range(gps5_repeat):
            offset = i * 20
            if offset + 20 > len(gps5_payload):
                break
            lat = struct.unpack_from('>i', gps5_payload, offset)[0] / scale[0]
            lon = struct.unpack_from('>i', gps5_payload, offset + 4)[0] / scale[1]
            alt = struct.unpack_from('>i', gps5_payload, offset + 8)[0] / scale[2]
            speed = struct.unpack_from('>i', gps5_payload, offset + 12)[0] / scale[3]
            t = pkt_time + (i / max(gps5_repeat, 1))
            results.append((t, lat, lon, alt, speed))


class GoProGpsInspector(QMainWindow):
    def __init__(self, mp4_path: str | None = None):
        super().__init__()
        self.setWindowTitle("GoPro GPS Inspector")
        self.resize(1400, 800)

        self._lap_start_time: float | None = None
        self._rows: list[tuple[float, float, float, float, float]] = []
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
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["Time (s)", "Latitude", "Longitude", "Alt (m)", "Speed (km/h)", "Δ Speed (km/h)"])
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.currentCellChanged.connect(self._on_row_changed)
        left_layout.addWidget(self._table)
        splitter.addWidget(left)

        # Right panel: map plot
        self._plot = pg.PlotWidget(title="GPS Track")
        self._plot.getViewBox().setAspectLocked(True, ratio=1.0)
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setLabel("bottom", "East (m)")
        self._plot.setLabel("left", "North (m)")
        self._trail = self._plot.plot([], [], pen=pg.mkPen("g", width=2))
        self._marker = self._plot.plot([], [], pen=None, symbol="o",
                                       symbolSize=12, symbolBrush="r")
        splitter.addWidget(self._plot)
        splitter.setSizes([450, 950])

        if mp4_path:
            self._load_file(mp4_path)
        else:
            self._prompt_open()

    def _prompt_open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open GoPro MP4", "", "MP4 Files (*.mp4 *.MP4)")
        if path:
            self._load_file(path)

    def _load_file(self, path: str):
        self.setWindowTitle(f"GoPro GPS Inspector \u2014 {Path(path).name}")
        gps_data = extract_gps_from_gpmf(path)

        if not gps_data:
            self._lbl_lap.setText("No GPS data (fix < 2D) in file!")
            return

        self._rows = gps_data
        duration = gps_data[-1][0] - gps_data[0][0]
        hz = len(gps_data) / duration if duration > 0 else 0
        self._lbl_lap.setText(f"GPS: {len(gps_data)} samples, {hz:.1f} Hz, {duration:.0f}s")

        # Populate table
        self._table.setRowCount(len(self._rows))
        for i, (t, lat, lon, alt, spd) in enumerate(self._rows):
            self._table.setItem(i, 0, QTableWidgetItem(f"{t:.3f}"))
            self._table.setItem(i, 1, QTableWidgetItem(f"{lat:.7f}"))
            self._table.setItem(i, 2, QTableWidgetItem(f"{lon:.7f}"))
            self._table.setItem(i, 3, QTableWidgetItem(f"{alt:.1f}"))
            self._table.setItem(i, 4, QTableWidgetItem(f"{spd * 3.6:.1f}"))
            # Computed speed from position delta
            if i == 0:
                self._table.setItem(i, 5, QTableWidgetItem("--"))
            else:
                t_prev, lat_prev, lon_prev, _, _ = self._rows[i - 1]
                dt = t - t_prev
                if dt > 0:
                    dlat_m = (lat - lat_prev) * 111320.0
                    dlon_m = (lon - lon_prev) * 111320.0 * math.cos(math.radians(lat))
                    dist = math.sqrt(dlat_m**2 + dlon_m**2)
                    computed_spd = dist / dt * 3.6
                    self._table.setItem(i, 5, QTableWidgetItem(f"{computed_spd:.1f}"))
                else:
                    self._table.setItem(i, 5, QTableWidgetItem("--"))

        # Project to local ENU for map
        lat0 = math.radians(gps_data[0][1])
        lon0 = math.radians(gps_data[0][2])
        self._xs = []
        self._ys = []
        for (_, lat, lon, _, _) in gps_data:
            dlat = math.radians(lat) - lat0
            dlon = math.radians(lon) - lon0
            self._ys.append(dlat * 6378137.0)
            self._xs.append(dlon * 6378137.0 * math.cos(lat0))

        self._trail.setData(self._xs, self._ys)

        # Set equal axis ranges
        if self._xs and self._ys:
            x_min, x_max = min(self._xs), max(self._xs)
            y_min, y_max = min(self._ys), max(self._ys)
            max_range = max(x_max - x_min, y_max - y_min) * 1.1
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

        # Update marker
        if row < len(self._xs):
            self._marker.setData([self._xs[row]], [self._ys[row]])

        # Update lap time
        if self._lap_start_time is not None:
            elapsed = self._rows[row][0] - self._lap_start_time
            self._lbl_lap.setText(f"Lap: {elapsed:.3f}s")


def main():
    app = QApplication(sys.argv)
    path = sys.argv[1] if len(sys.argv) > 1 else None
    win = GoProGpsInspector(path)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

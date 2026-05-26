#!/usr/bin/env python3
"""GPS Drift Correction Tool -- visualize and correct per-lap GPS offset drift.

Usage: python tools/gps_drift_correction.py [path/to/file.xrz]

Algorithm:
  1. Split session into laps via S/F crossing
  2. Convert each lap's GPS to local ENU (meters)
  3. Pick reference lap (fastest full lap)
  4. For each other lap, find (dx, dy) offset that minimizes median
     nearest-point distance to reference trajectory
  5. Visualize before/after alignment

Controls:
  - Toggle "Show Corrected" to see aligned laps
  - Slider adjusts which lap is highlighted
"""

import sys
import math
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QWidget, QVBoxLayout,
    QHBoxLayout, QPushButton, QLabel, QFileDialog, QSlider, QCheckBox,
)
from PySide6.QtCore import Qt, Slot
import pyqtgraph as pg

from datahawk.xrz_parser import parse_xrz, _GPS_LAT_ID, _GPS_LON_ID, _GPS_SPEED_ID
from datahawk.lap_detection import get_sf_timestamps_based_on_ch4, detect_start_finish_fine, detect_laps


def latlon_to_enu(lats, lons, lat0_rad, lon0_rad):
    """Convert lat/lon arrays to local ENU (meters) relative to reference point."""
    xs = (np.radians(lons) - lon0_rad) * 6378137.0 * math.cos(lat0_rad)
    ys = (np.radians(lats) - lat0_rad) * 6378137.0
    return xs, ys


def nearest_point_distances(lap_x, lap_y, ref_x, ref_y):
    """For each point in lap, find distance to nearest point in ref. Returns array."""
    # Vectorized: for each lap point, compute distance to all ref points, take min
    # Chunked to avoid memory explosion on large arrays
    dists = np.empty(len(lap_x))
    chunk = 500
    for i in range(0, len(lap_x), chunk):
        end = min(i + chunk, len(lap_x))
        dx = lap_x[i:end, None] - ref_x[None, :]
        dy = lap_y[i:end, None] - ref_y[None, :]
        dists[i:end] = np.sqrt(dx**2 + dy**2).min(axis=1)
    return dists


def find_optimal_offset(lap_x, lap_y, ref_x, ref_y, max_offset=10.0, step=0.5):
    """Brute-force search for (dx, dy) that minimizes median nearest-point distance."""
    best_cost = float('inf')
    best_dx, best_dy = 0.0, 0.0

    # Subsample for speed (every 4th point)
    lap_x_s = lap_x[::4]
    lap_y_s = lap_y[::4]
    ref_x_s = ref_x[::4]
    ref_y_s = ref_y[::4]

    offsets = np.arange(-max_offset, max_offset + step, step)
    for dx in offsets:
        for dy in offsets:
            dists = nearest_point_distances(lap_x_s + dx, lap_y_s + dy, ref_x_s, ref_y_s)
            cost = np.median(dists)
            if cost < best_cost:
                best_cost = cost
                best_dx, best_dy = dx, dy

    # Refine with finer grid around best
    fine_step = step / 5
    fine_range = step
    offsets_fine = np.arange(-fine_range, fine_range + fine_step, fine_step)
    for dx in best_dx + offsets_fine:
        for dy in best_dy + offsets_fine:
            dists = nearest_point_distances(lap_x_s + dx, lap_y_s + dy, ref_x_s, ref_y_s)
            cost = np.median(dists)
            if cost < best_cost:
                best_cost = cost
                best_dx, best_dy = dx, dy

    return best_dx, best_dy, best_cost


class GpsDriftTool(QMainWindow):
    def __init__(self, xrz_path: str | None = None):
        super().__init__()
        self.setWindowTitle("GPS Drift Correction")
        self.resize(1600, 900)

        self._laps_xy: list[tuple[np.ndarray, np.ndarray]] = []
        self._offsets: list[tuple[float, float]] = []
        self._ref_idx: int = 0

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)

        # Controls
        ctrl = QHBoxLayout()
        self._lbl_info = QLabel("Load an XRZ file")
        self._lbl_info.setStyleSheet("font-size: 14px;")
        ctrl.addWidget(self._lbl_info, 1)

        self._chk_corrected = QCheckBox("Show Corrected")
        self._chk_corrected.setChecked(False)
        self._chk_corrected.toggled.connect(self._redraw)
        ctrl.addWidget(self._chk_corrected)

        self._lbl_lap = QLabel("Highlight: All")
        ctrl.addWidget(self._lbl_lap)
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setMinimum(0)
        self._slider.setMaximum(0)
        self._slider.valueChanged.connect(self._on_slider)
        ctrl.addWidget(self._slider)
        layout.addLayout(ctrl)

        # Plots side by side
        splitter = QSplitter(Qt.Horizontal)
        self._plot_raw = pg.PlotWidget(title="Raw GPS (per-lap colored)")
        self._plot_raw.getViewBox().setAspectLocked(True)
        self._plot_raw.showGrid(x=True, y=True, alpha=0.3)
        self._plot_raw.setLabel("bottom", "East (m)")
        self._plot_raw.setLabel("left", "North (m)")
        splitter.addWidget(self._plot_raw)

        self._plot_corr = pg.PlotWidget(title="Drift-Corrected")
        self._plot_corr.getViewBox().setAspectLocked(True)
        self._plot_corr.showGrid(x=True, y=True, alpha=0.3)
        self._plot_corr.setLabel("bottom", "East (m)")
        self._plot_corr.setLabel("left", "North (m)")
        splitter.addWidget(self._plot_corr)
        layout.addWidget(splitter)

        if xrz_path:
            self._load_file(xrz_path)
        else:
            self._prompt_open()

    def _prompt_open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open XRZ File", "", "XRZ Files (*.xrz)")
        if path:
            self._load_file(path)

    def _load_file(self, path: str):
        self.setWindowTitle(f"GPS Drift Correction \u2014 {Path(path).name}")
        session = parse_xrz(path)

        lat_ch = session.channels.get(_GPS_LAT_ID)
        lon_ch = session.channels.get(_GPS_LON_ID)
        speed_ch = session.channels.get(_GPS_SPEED_ID)
        if not lat_ch or not lon_ch:
            self._lbl_info.setText("No GPS data!")
            return

        # Detect laps
        sf_line = detect_start_finish_fine(session)
        crossings = detect_laps(session, sf_line)

        if len(crossings) < 2:
            self._lbl_info.setText("Need at least 2 S/F crossings for lap split")
            return

        # Reference point for ENU
        lat0_rad = math.radians(lat_ch.values[0])
        lon0_rad = math.radians(lon_ch.values[0])

        # Split GPS into laps
        lats = np.array(lat_ch.values)
        lons = np.array(lon_ch.values)
        times = np.array(lat_ch.timestamps)

        # Use Master Clk for time reference
        mclk_ch = session.channels.get(0)
        mclk = np.array(mclk_ch.values) if mclk_ch else times

        # Build boundaries: [session_start] + crossings + [session_end]
        boundaries = [mclk[0]] + crossings + [mclk[-1]]

        self._laps_xy = []
        lap_times = []
        for i in range(len(boundaries) - 1):
            mask = (mclk >= boundaries[i]) & (mclk < boundaries[i + 1])
            if mask.sum() < 10:
                continue
            lap_lats = lats[mask]
            lap_lons = lons[mask]
            xs, ys = latlon_to_enu(lap_lats, lap_lons, lat0_rad, lon0_rad)
            self._laps_xy.append((xs, ys))
            lap_times.append(boundaries[i + 1] - boundaries[i])

        if len(self._laps_xy) < 2:
            self._lbl_info.setText("Not enough laps detected")
            return

        # Reference = fastest full lap (exclude first/last)
        full_laps = list(range(1, len(self._laps_xy) - 1)) if len(self._laps_xy) > 2 else [0]
        self._ref_idx = min(full_laps, key=lambda i: lap_times[i])

        # Compute offsets
        ref_x, ref_y = self._laps_xy[self._ref_idx]
        self._offsets = [(0.0, 0.0)] * len(self._laps_xy)

        self._lbl_info.setText("Computing drift offsets...")
        QApplication.processEvents()

        for i, (lx, ly) in enumerate(self._laps_xy):
            if i == self._ref_idx:
                continue
            dx, dy, cost = find_optimal_offset(lx, ly, ref_x, ref_y)
            self._offsets[i] = (dx, dy)

        # Summary
        offsets_mag = [math.sqrt(dx**2 + dy**2) for dx, dy in self._offsets]
        max_drift = max(offsets_mag)
        self._lbl_info.setText(
            f"{len(self._laps_xy)} laps | Ref: lap {self._ref_idx + 1} | "
            f"Max drift: {max_drift:.1f}m | "
            + " | ".join(f"L{i+1}: ({dx:+.1f}, {dy:+.1f})m" for i, (dx, dy) in enumerate(self._offsets))
        )

        self._slider.setMaximum(len(self._laps_xy))  # 0 = all
        self._slider.setValue(0)
        self._redraw()

    def _redraw(self):
        self._plot_raw.clear()
        self._plot_corr.clear()

        highlight = self._slider.value()  # 0 = all
        colors = ['r', 'g', 'b', 'c', 'm', 'y', 'w', '#ff8800', '#88ff00', '#0088ff']

        for i, (xs, ys) in enumerate(self._laps_xy):
            color = colors[i % len(colors)]
            width = 2 if (highlight == 0 or highlight == i + 1) else 0.5
            alpha = 255 if (highlight == 0 or highlight == i + 1) else 60

            pen_raw = pg.mkPen(color=color, width=width)
            pen_raw.setColor(pg.mkColor(color))

            # Raw
            self._plot_raw.plot(xs, ys, pen=pg.mkPen(color=color, width=width))

            # Corrected
            if self._chk_corrected.isChecked():
                dx, dy = self._offsets[i]
                self._plot_corr.plot(xs + dx, ys + dy, pen=pg.mkPen(color=color, width=width))
            else:
                self._plot_corr.plot(xs, ys, pen=pg.mkPen(color=color, width=width))

        # Mark reference lap
        ref_x, ref_y = self._laps_xy[self._ref_idx]
        self._plot_raw.plot([ref_x[0]], [ref_y[0]], pen=None, symbol='s',
                           symbolSize=14, symbolBrush='yellow')
        if self._chk_corrected.isChecked():
            self._plot_corr.plot([ref_x[0]], [ref_y[0]], pen=None, symbol='s',
                                symbolSize=14, symbolBrush='yellow')

    @Slot(int)
    def _on_slider(self, val):
        if val == 0:
            self._lbl_lap.setText("Highlight: All")
        else:
            dx, dy = self._offsets[val - 1]
            self._lbl_lap.setText(f"Highlight: Lap {val} (offset: {dx:+.1f}, {dy:+.1f}m)")
        self._redraw()


def main():
    app = QApplication(sys.argv)
    path = sys.argv[1] if len(sys.argv) > 1 else None
    win = GpsDriftTool(path)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

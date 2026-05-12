"""Session viewer window with laps table and telemetry plot."""

from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QWidget, QComboBox, QLabel,
    QHBoxLayout, QTableWidget, QTableWidgetItem, QSplitter,
)
from PySide6.QtCore import Qt

from datahawk.xrz_parser import parse_xrz
from datahawk.session_processing import process_session, Session


class SessionViewer(QMainWindow):
    def __init__(self, xrz_path: Path, parent=None):
        super().__init__(parent)
        parsed = parse_xrz(xrz_path)
        self._session: Session = process_session(parsed)

        meta_time = self._session.start_time
        self.setWindowTitle(f"DataHawk — {self._session.track} {self._session.date} {meta_time}")
        self.setMinimumSize(900, 600)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Laps table
        self._table = QTableWidget(len(self._session.laps), 2)
        self._table.setHorizontalHeaderLabels(["Lap", "Time"])
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setMaximumHeight(200)
        for i, lap in enumerate(self._session.laps):
            self._table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self._table.setItem(i, 1, QTableWidgetItem(f"{lap.lap_time:.3f}s"))
        self._table.resizeColumnsToContents()
        layout.addWidget(self._table)

        # Channel selector and reference lap
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Channel:"))
        self._combo = QComboBox()
        self._combo.setMinimumWidth(250)
        top_row.addWidget(self._combo)
        top_row.addWidget(QLabel("Reference:"))
        self._ref_combo = QComboBox()
        self._ref_combo.addItem("None")
        for i, lap in enumerate(self._session.laps):
            self._ref_combo.addItem(f"Lap {i + 1} ({lap.lap_time:.3f}s)")
        top_row.addWidget(self._ref_combo)
        top_row.addStretch()
        layout.addLayout(top_row)

        # Plot widget
        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Time", units="s")
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        layout.addWidget(self._plot)

        # Populate channel dropdown from first lap's channels
        self._channel_names: list[str] = []
        if self._session.laps:
            for name in sorted(self._session.laps[0].channels.keys()):
                self._combo.addItem(name)
                self._channel_names.append(name)

        self._combo.currentIndexChanged.connect(self._update_plot)
        self._ref_combo.currentIndexChanged.connect(self._update_plot)
        self._table.selectionModel().selectionChanged.connect(self._update_plot)

        # Select first lap
        if self._session.laps:
            self._table.selectRow(0)

    def _update_plot(self, *_args):
        self._plot.clear()
        rows = self._table.selectionModel().selectedRows()
        if not rows or not self._channel_names:
            return

        lap_idx = rows[0].row()
        ch_name = self._channel_names[self._combo.currentIndex()]
        lap = self._session.laps[lap_idx]

        if ch_name not in lap.channels:
            return

        mc = lap.channels.get("Master Clk")
        if not mc:
            return

        t0 = mc.samples[0] if mc.samples else 0
        times = [t - t0 for t in mc.samples]
        samples = lap.channels[ch_name].samples

        self._plot.setLabel("left", ch_name)
        self._plot.plot(times, samples, pen=pg.mkPen("y", width=1), name=f"Lap {lap_idx + 1}")

        # Reference lap overlay (same sample indices = same track position)
        ref_sel = self._ref_combo.currentIndex() - 1  # 0 = "None"
        if ref_sel >= 0 and ref_sel != lap_idx:
            ref_lap = self._session.laps[ref_sel]
            if ch_name in ref_lap.channels:
                ref_samples = ref_lap.channels[ch_name].samples
                self._plot.plot(times, ref_samples, pen=pg.mkPen("c", width=1, style=Qt.DashLine), name=f"Lap {ref_sel + 1}")

        self._plot.enableAutoRange()

"""Session viewer window with telemetry plot."""

from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget, QComboBox, QLabel, QHBoxLayout

from datahawk.xrz_parser import parse_xrz, ParsedSession


class SessionViewer(QMainWindow):
    def __init__(self, xrz_path: Path, parent=None):
        super().__init__(parent)
        self._session: ParsedSession = parse_xrz(xrz_path)

        meta = self._session.metadata
        self.setWindowTitle(f"DataHawk — {meta.track} {meta.date} {meta.time}")
        self.setMinimumSize(900, 500)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Channel selector
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Channel:"))
        self._combo = QComboBox()
        self._combo.setMinimumWidth(250)
        top_row.addWidget(self._combo)
        top_row.addStretch()
        layout.addLayout(top_row)

        # Plot widget
        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Time", units="s")
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        layout.addWidget(self._plot)

        # Populate dropdown with channels that have data
        self._channel_ids: list[int] = []
        for ch_id in sorted(self._session.channels.keys()):
            ch = self._session.channels[ch_id]
            if ch.samples:
                self._combo.addItem(f"{ch.name} ({len(ch.samples)} pts)")
                self._channel_ids.append(ch_id)

        self._combo.currentIndexChanged.connect(self._on_channel_changed)
        if self._channel_ids:
            self._on_channel_changed(0)

    def _on_channel_changed(self, index: int):
        if index < 0 or index >= len(self._channel_ids):
            return
        ch = self._session.channels[self._channel_ids[index]]
        self._plot.clear()
        self._plot.setLabel("left", ch.name)
        self._plot.plot(ch.timestamps, ch.values, pen=pg.mkPen("y", width=1))

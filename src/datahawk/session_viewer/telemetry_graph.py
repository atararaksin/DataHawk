"""Telemetry graph widget with channel plotting, reference overlay, and cursor."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pyqtgraph as pg
from PySide6.QtCore import Signal

from datahawk.types import Session, Lap


@dataclass
class GraphClicked:
    """Emitted when the graph is clicked."""
    session_time: float


class TelemetryGraph(pg.PlotWidget):
    """Graph showing channel data for the active lap with optional reference overlay."""

    clicked = Signal(object)  # GraphClicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLabel("bottom", "Time", units="s")
        self.showGrid(x=True, y=True, alpha=0.3)
        self.scene().sigMouseClicked.connect(self._on_click)

        self._cursor = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen("r", width=2))
        self.addItem(self._cursor)
        self._lap_start_time = 0.0

    def set_cursor_session_time(self, session_time: float):
        """Set cursor position using session time."""
        self._cursor.setPos(session_time - self._lap_start_time)

    def update_plot(self, *, session: Session, lap_idx: int, channel_name: str,
                    ref_lap_idx: int | None, diff_mode: bool):
        """Redraw the graph for the given lap/channel/reference configuration."""
        self.clear()
        self.addItem(self._cursor)

        if lap_idx >= len(session.laps):
            return

        lap = session.laps[lap_idx]
        self._lap_start_time = lap.lap_start_time

        if channel_name not in lap.channels:
            return

        ch = lap.channels[channel_name]

        if diff_mode:
            self._plot_diff(session, lap, lap_idx, ch, channel_name, ref_lap_idx)
        else:
            self._plot_normal(session, lap, lap_idx, ch, channel_name, ref_lap_idx)

        # Sector split lines
        s1_line = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen("w", width=1),
                                  label="S1", labelOpts={"position": 0.95, "color": "w"})
        self.addItem(s1_line)
        for i, split_time in enumerate(lap.sector_split_times):
            if not math.isnan(split_time):
                x = split_time - lap.lap_start_time
                line = pg.InfiniteLine(pos=x, angle=90, pen=pg.mkPen("w", width=1),
                                       label=f"S{i+2}", labelOpts={"position": 0.95, "color": "w"})
                self.addItem(line)

        self.enableAutoRange()

    def _plot_diff(self, session: Session, lap: Lap, lap_idx: int,
                   ch, channel_name: str, ref_lap_idx: int | None):
        mc = lap.master_clk
        if not mc:
            return
        t0 = lap.lap_start_time
        ref_lap = session.laps[ref_lap_idx] if ref_lap_idx is not None and ref_lap_idx != lap_idx else None
        ref_ch = ref_lap.channels.get(channel_name) if ref_lap else None

        diff_times = []
        diff_values = []
        for i, (t, cur_v) in enumerate(zip(mc.samples, ch.samples)):
            if math.isnan(t) or math.isnan(cur_v):
                continue
            if ref_ch and i < len(ref_ch.samples) and not math.isnan(ref_ch.samples[i]):
                diff_times.append(t - t0)
                diff_values.append(cur_v - ref_ch.samples[i])
            else:
                diff_times.append(t - t0)
                diff_values.append(float('nan'))

        self.setLabel("left", f"{channel_name} (diff)")
        if diff_times:
            self.plot(diff_times, diff_values, pen=pg.mkPen("c", width=1), name="Diff")

    def _plot_normal(self, session: Session, lap: Lap, lap_idx: int,
                     ch, channel_name: str, ref_lap_idx: int | None):
        self.setLabel("left", channel_name)
        self.plot(ch.raw_timestamps, ch.raw_values, pen=pg.mkPen("y", width=1), name=f"Lap {lap_idx + 1}")

        # Reference lap overlay
        if ref_lap_idx is not None and ref_lap_idx != lap_idx:
            ref_lap = session.laps[ref_lap_idx]
            if channel_name in ref_lap.channels:
                mc = lap.master_clk
                if mc:
                    t0 = lap.lap_start_time
                    ref_times = []
                    ref_samples = []
                    for t, v in zip(mc.samples, ref_lap.channels[channel_name].samples):
                        if t == t and v == v:  # skip NaN
                            ref_times.append(t - t0)
                            ref_samples.append(v)
                    if ref_times:
                        self.plot(ref_times, ref_samples, pen=pg.mkPen("g", width=1), name=f"Lap {ref_lap_idx + 1}")

    def _on_click(self, event):
        pos = event.scenePos()
        if not self.sceneBoundingRect().contains(pos):
            return
        mouse_point = self.plotItem.vb.mapSceneToView(pos)
        session_time = self._lap_start_time + mouse_point.x()
        self.clicked.emit(GraphClicked(session_time=session_time))

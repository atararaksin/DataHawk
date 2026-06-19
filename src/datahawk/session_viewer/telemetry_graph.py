"""Telemetry graph widget with channel plotting, reference overlay, and cursor."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pyqtgraph as pg
from PySide6.QtWidgets import QLabel
from PySide6.QtCore import Signal, Qt, QEvent

from datahawk.types import Session, Lap
from datahawk.session_utils import get_channel_value_in_another_lap_with_interpolation


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
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)
        self.scene().sigMouseClicked.connect(self._on_click)

        self._cursor = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen("r", width=2))
        self.addItem(self._cursor)
        self._lap_start_time = 0.0

        # Current value label (bottom-left overlay, doesn't affect graph range)
        self._value_label = QLabel(self)
        self._value_label.setStyleSheet("color: yellow; background: transparent; font-size: 12px; padding: 4px;")
        self._value_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._value_label.setFixedSize(150, 40)
        self._value_label.move(50, 0)

        # State for value lookup
        self._session = None
        self._current_lap = None
        self._ref_lap = None
        self._channel_name = None

    def set_cursor_session_time(self, session_time: float):
        """Set cursor position using session time."""
        x = session_time - self._lap_start_time
        self._cursor.setPos(x)
        self._update_value_labels(session_time)

    def update_plot(self, *, session: Session, lap_idx: int, channel_name: str,
                    ref_lap=None, diff_mode: bool):
        """Redraw the graph for the given lap/channel/reference configuration."""
        self.clear()
        self._cursor = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen("r", width=2))
        self.addItem(self._cursor)

        if lap_idx >= len(session.laps):
            return

        lap = session.laps[lap_idx]
        self._lap_start_time = lap.lap_start_time

        # Store for value display
        self._session = session
        self._current_lap = lap
        self._ref_lap = ref_lap
        self._channel_name = channel_name

        if channel_name not in lap.channels:
            return

        ch = lap.channels[channel_name]

        if diff_mode:
            self._plot_diff(session, lap, lap_idx, ch, channel_name, ref_lap)
        else:
            self._plot_normal(session, lap, lap_idx, ch, channel_name, ref_lap)

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
                   ch, channel_name: str, ref_lap):
        mc = lap.master_clk
        if not mc:
            return
        t0 = lap.lap_start_time
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
                     ch, channel_name: str, ref_lap):
        self.setLabel("left", channel_name)
        self.plot(ch.raw_timestamps, ch.raw_values, pen=pg.mkPen("y", width=1), name=f"Lap {lap_idx + 1}")

        # Reference lap overlay
        if ref_lap is not None and ref_lap is not lap:
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
                        self.plot(ref_times, ref_samples, pen=pg.mkPen("r", width=1), name="Ref")

    def _on_click(self, event):
        pos = event.scenePos()
        if not self.sceneBoundingRect().contains(pos):
            return
        mouse_point = self.plotItem.vb.mapSceneToView(pos)
        session_time = self._lap_start_time + mouse_point.x()
        self.clicked.emit(GraphClicked(session_time=session_time))

    def _update_value_labels(self, session_time: float):
        """Update bottom-left value label overlay."""
        if not self._session or not self._current_lap or not self._channel_name:
            self._value_label.setText("")
            return

        lap_val = get_channel_value_in_another_lap_with_interpolation(
            self._session, session_time, self._current_lap, self._channel_name)

        lines = []
        if not math.isnan(lap_val):
            lines.append(f'<span style="color: yellow;">Lap: {lap_val:.1f}</span>')

        if self._ref_lap is not None:
            ref_val = get_channel_value_in_another_lap_with_interpolation(
                self._session, session_time, self._ref_lap, self._channel_name)
            if not math.isnan(ref_val):
                lines.append(f'<span style="color: red;">Ref: {ref_val:.1f}</span>')

        self._value_label.setText("<br>".join(lines))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_value_label'):
            self._value_label.move(50, self.height() - 40)

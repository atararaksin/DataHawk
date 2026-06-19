"""Live delta bar widget — iRacing-style time delta to reference lap."""

from __future__ import annotations

import math

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QColor, QFont

from datahawk.source.channel_constants import LAP_TIME
from datahawk.types import Session, Lap
from datahawk.session_utils import get_channel_value_in_another_lap_with_interpolation


class DeltaBar(QWidget):
    """Horizontal bar: center=0, right/green=faster, left/red=slower."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(24)
        self._delta = 0.0
        self._visible = False
        self._max_delta = 2.0  # ±2s full scale

    def update_delta(self, session: Session, session_time: float, current_lap: Lap, ref_lap: Lap | None):
        """Compute delta from Master Clk values at same spatial position."""
        if ref_lap is None or ref_lap is current_lap:
            self._visible = False
            self.update()
            return

        cur_mc = get_channel_value_in_another_lap_with_interpolation(
            session, session_time, current_lap, LAP_TIME)
        ref_mc = get_channel_value_in_another_lap_with_interpolation(
            session, session_time, ref_lap, LAP_TIME)

        if math.isnan(cur_mc) or math.isnan(ref_mc):
            self._visible = False
            self.update()
            return

        # positive = slower (current lap elapsed more time), negative = faster
        self._delta = cur_mc - ref_mc
        self._visible = True
        self.update()

    def paintEvent(self, event):
        if not self._visible:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx = w / 2

        # Dark background
        p.fillRect(0, 0, w, h, QColor(30, 30, 30))

        # Bar
        clamped = max(-self._max_delta, min(self._max_delta, self._delta))
        bar_frac = clamped / self._max_delta
        bar_px = abs(bar_frac) * (cx - 2)

        if self._delta > 0:
            # Slower — red bar extends LEFT from center
            p.fillRect(int(cx - bar_px), 2, int(bar_px), h - 4, QColor(220, 40, 40))
        elif self._delta < 0:
            # Faster — green bar extends RIGHT from center
            p.fillRect(int(cx), 2, int(bar_px), h - 4, QColor(40, 200, 40))

        # Center tick
        p.setPen(QColor(200, 200, 200))
        p.drawLine(int(cx), 0, int(cx), h)

        # Delta text centered
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255))
        sign = "+" if self._delta > 0 else "-" if self._delta < 0 else ""
        p.drawText(0, 0, w, h, Qt.AlignCenter, f"{sign}{abs(self._delta):.2f}")
        p.end()

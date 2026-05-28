"""Source-agnostic types for parsed telemetry sessions."""

from __future__ import annotations

from dataclasses import dataclass, field

from datahawk.source.channel_constants import (
    GPS_LATITUDE, GPS_LONGITUDE, GPS_SPEED, GPS_HEADING, MASTER_CLK,
)


@dataclass
class SourceChannel:
    """A telemetry channel definition."""
    name: str
    timestamps: list[float] = field(default_factory=list, repr=False)
    values: list[float] = field(default_factory=list, repr=False)

    def append(self, ts: float, val: float) -> None:
        self.timestamps.append(ts)
        self.values.append(val)

    def get_value_at_time_with_interpolation(self, ts: float) -> float:
        """Interpolate channel value at given timestamp using binary search."""
        import bisect
        i = bisect.bisect_right(self.timestamps, ts)
        if i == 0:
            return self.values[0]
        if i >= len(self.timestamps):
            return self.values[-1]
        t0, t1 = self.timestamps[i - 1], self.timestamps[i]
        frac = (ts - t0) / (t1 - t0) if t1 != t0 else 0.0
        return self.values[i - 1] + frac * (self.values[i] - self.values[i - 1])


@dataclass
class SourceSessionMetadata:
    """Non-temporal session metadata."""
    track: str = ""
    date: str = ""
    time: str = ""
    session_type: str = ""


@dataclass
class SourceSession:
    """Complete parsed session from any telemetry source."""
    metadata: SourceSessionMetadata
    channels: dict[str, SourceChannel] = field(default_factory=dict)

    @property
    def gps_lat(self) -> SourceChannel:
        return self.channels[GPS_LATITUDE]

    @property
    def gps_lon(self) -> SourceChannel:
        return self.channels[GPS_LONGITUDE]

    @property
    def gps_speed(self) -> SourceChannel:
        return self.channels[GPS_SPEED]

    @property
    def gps_heading(self) -> SourceChannel:
        return self.channels[GPS_HEADING]

    @property
    def master_clk(self) -> SourceChannel:
        return self.channels[MASTER_CLK]

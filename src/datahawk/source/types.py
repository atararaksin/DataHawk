"""Source-agnostic types for parsed telemetry sessions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SourceChannel:
    """A telemetry channel definition."""
    id: int
    short_name: str
    long_name: str
    is_float16: bool = True
    timestamps: list[float] = field(default_factory=list, repr=False)
    values: list[float] = field(default_factory=list, repr=False)

    @property
    def name(self) -> str:
        return self.long_name or self.short_name

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
    channels: dict[int, SourceChannel]

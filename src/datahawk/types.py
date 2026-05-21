"""Core data types for processed sessions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Channel:
    """A reindexed channel with fixed sample count per lap."""
    name: str
    samples: list[float]  # NaN for missing data
    raw_timestamps: list[float] = field(default_factory=list, repr=False)
    raw_values: list[float] = field(default_factory=list, repr=False)


@dataclass
class Lap:
    """A single lap reindexed to track position."""
    lap_index: int
    lap_time: float
    lap_start_time: float
    channels: dict[str, Channel] = field(default_factory=dict)
    sector_times: list[float] = field(default_factory=list)  # duration of each sector (NaN if unknown)
    sector_split_times: list[float] = field(default_factory=list)  # absolute times of sector splits (NaN if unknown)

@dataclass
class Track:
    name: str
    sf_line: Line
    sector_split_lines: list[Line] = field(default_factory=list)

@dataclass
class TemporalIndexEntry:
    """Maps a time step to a position in the reindexed data."""
    lap_index: int
    sample_index: int


@dataclass
class Session:
    """Processed session with laps aligned by track position."""
    start_time: str
    date: str
    track: Track
    samples_per_lap: int
    reference_lap_index: int
    best_lap_index: int
    best_lap_time: float
    laps: list[Lap] = field(default_factory=list)
    temporal_index: list[TemporalIndexEntry] = field(default_factory=list)
    time_resolution: float = 0.04  # 25Hz

@dataclass
class Point:
    lat: float
    lon: float

@dataclass
class Line:
    a: Point
    b: Point

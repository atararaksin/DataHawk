"""Session processing: lap detection, reindexing by track position."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Optional

from datahawk.xrz_parser import ParsedSession, Channel as XrzChannel
from datahawk.lap_detection import detect_lap_boundaries


@dataclass
class Channel:
    """A reindexed channel with fixed sample count per lap."""
    name: str
    samples: list[Optional[float]]  # NaN for missing data


@dataclass
class Lap:
    """A single lap reindexed to track position."""
    lap_index: int
    lap_time: float
    lap_start_time: float
    channels: dict[str, Channel] = field(default_factory=dict)


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
    track: str
    samples_per_lap: int
    reference_lap_index: int
    best_lap_index: int
    best_lap_time: float
    laps: list[Lap] = field(default_factory=list)
    temporal_index: list[TemporalIndexEntry] = field(default_factory=list)



def _interpolate_at(target_time: float, times: list[float], values: list[float]) -> Optional[float]:
    """Linear interpolation of values at target_time."""
    if not times or target_time < times[0] or target_time > times[-1]:
        return float('nan')
    hi = bisect_right(times, target_time)
    if hi == 0:
        return values[0]
    if hi >= len(times):
        return values[-1]
    lo = hi - 1
    if times[lo] == target_time:
        return values[lo]
    frac = (target_time - times[lo]) / (times[hi] - times[lo])
    return values[lo] + frac * (values[hi] - values[lo])


def _distance_along_track(lats: list[float], lons: list[float]) -> list[float]:
    """Compute cumulative distance along track in meters."""
    cos_lat = math.cos(math.radians(lats[0])) if lats else 1.0
    dist = [0.0]
    for i in range(1, len(lats)):
        dlat = (lats[i] - lats[i-1]) * 111000
        dlon = (lons[i] - lons[i-1]) * 111000 * cos_lat
        dist.append(dist[-1] + math.sqrt(dlat**2 + dlon**2))
    return dist


def process_session(parsed: ParsedSession) -> Session:
    """Process a parsed XRZ session into position-indexed laps."""
    crossings = detect_lap_boundaries(parsed)

    lat_ch = parsed.channels.get(-1)
    lon_ch = parsed.channels.get(-2)
    speed_ch = parsed.channels.get(-3)

    if len(crossings) < 2 or not lat_ch or not lon_ch:
        return Session(
            start_time=parsed.metadata.time,
            date=parsed.metadata.date,
            track=parsed.metadata.track,
            samples_per_lap=0,
            reference_lap_index=0,
            best_lap_index=0,
            best_lap_time=0.0,
        )

    # Find fastest lap
    lap_times = [crossings[i+1] - crossings[i] for i in range(len(crossings)-1)]
    fastest_idx = min(range(len(lap_times)), key=lambda i: lap_times[i])

    # Reference lap: use GPS at 25Hz
    ref_start = crossings[fastest_idx]
    ref_end = crossings[fastest_idx + 1]
    gps_times = lat_ch.timestamps
    gps_lats = lat_ch.values
    gps_lons = lon_ch.values

    # Extract reference lap GPS samples
    ref_indices = [i for i, t in enumerate(gps_times) if ref_start <= t < ref_end]
    ref_times = [gps_times[i] for i in ref_indices]
    ref_lats = [gps_lats[i] for i in ref_indices]
    ref_lons = [gps_lons[i] for i in ref_indices]
    samples_per_lap = len(ref_indices)

    if samples_per_lap < 10:
        return Session(
            start_time=parsed.metadata.time, date=parsed.metadata.date,
            track=parsed.metadata.track, samples_per_lap=0, reference_lap_index=0,
            best_lap_index=0, best_lap_time=0.0,
        )

    # Compute reference lap distance array
    ref_dist = _distance_along_track(ref_lats, ref_lons)

    # Collect all channel names we want to reindex
    channel_names = {}
    for ch_id, ch in parsed.channels.items():
        if ch.timestamps:
            channel_names[ch_id] = ch.name

    # Process each lap
    session = Session(
        start_time=parsed.metadata.time,
        date=parsed.metadata.date,
        track=parsed.metadata.track,
        samples_per_lap=samples_per_lap,
        reference_lap_index=fastest_idx,
        best_lap_index=fastest_idx,
        best_lap_time=lap_times[fastest_idx],
    )

    cos_lat = math.cos(math.radians(ref_lats[0]))

    for lap_idx in range(len(crossings) - 1):
        lap_start_time = crossings[lap_idx]
        lap_end = crossings[lap_idx + 1]
        lap_time = lap_end - lap_start_time

        lap = Lap(lap_index=lap_idx, lap_time=lap_time, lap_start_time=lap_start_time)

        if lap_idx == fastest_idx:
            # Reference lap: time-based interpolation (uniform time samples)
            for ch_id, ch_name in channel_names.items():
                ch = parsed.channels[ch_id]
                lo_i = max(0, bisect_left(ch.timestamps, ref_start) - 1)
                hi_i = min(len(ch.timestamps), bisect_right(ch.timestamps, ref_end) + 1)
                ch_times = ch.timestamps[lo_i:hi_i]
                ch_vals = ch.values[lo_i:hi_i]
                resampled = []
                for t in ref_times:
                    val = _interpolate_at(t, ch_times, ch_vals)
                    resampled.append(val)
                lap.channels[ch_name] = Channel(name=ch_name, samples=resampled)
        else:
            # Other laps: distance-based interpolation
            # Get this lap's GPS positions and compute distance
            lap_gps_idx = [i for i, t in enumerate(gps_times) if lap_start_time <= t < lap_end]
            if len(lap_gps_idx) < 10:
                # Incomplete lap
                for ch_id, ch_name in channel_names.items():
                    lap.channels[ch_name] = Channel(
                        name=ch_name, samples=[float('nan')] * samples_per_lap
                    )
                session.laps.append(lap)
                continue

            lap_lats = [gps_lats[i] for i in lap_gps_idx]
            lap_lons = [gps_lons[i] for i in lap_gps_idx]
            lap_times_arr = [gps_times[i] for i in lap_gps_idx]
            lap_dist = _distance_along_track(lap_lats, lap_lons)

            # For each reference sample position, find corresponding time in this lap
            # by matching distance along track
            total_ref_dist = ref_dist[-1] if ref_dist[-1] > 0 else 1.0
            total_lap_dist = lap_dist[-1] if lap_dist[-1] > 0 else 1.0

            for ch_id, ch_name in channel_names.items():
                ch = parsed.channels[ch_id]
                # Pre-slice channel to lap time range for fast interpolation
                lo_i = bisect_left(ch.timestamps, lap_start_time) - 1
                hi_i = bisect_right(ch.timestamps, lap_end) + 1
                lo_i = max(0, lo_i)
                hi_i = min(len(ch.timestamps), hi_i)
                ch_times = ch.timestamps[lo_i:hi_i]
                ch_vals = ch.values[lo_i:hi_i]
                resampled = []
                for s_idx in range(samples_per_lap):
                    # Normalized position (0-1) from reference lap
                    norm_pos = ref_dist[s_idx] / total_ref_dist
                    # Target distance in this lap
                    target_dist = norm_pos * total_lap_dist
                    # Find time at this distance (interpolate in lap_dist -> lap_times)
                    t_at_dist = _interpolate_at(target_dist, lap_dist, lap_times_arr)
                    if t_at_dist is None or math.isnan(t_at_dist):
                        resampled.append(float('nan'))
                    else:
                        val = _interpolate_at(t_at_dist, ch_times, ch_vals)
                        resampled.append(val if val is not None else float('nan'))
                lap.channels[ch_name] = Channel(name=ch_name, samples=resampled)

        session.laps.append(lap)

    # Build temporal index
    session.temporal_index = _build_temporal_index(session)

    return session


def _build_temporal_index(session: Session) -> list[TemporalIndexEntry]:
    """Build time-to-position mapping using reindexed Master Clk values."""
    if not session.laps or session.samples_per_lap == 0:
        return []
    if "Master Clk" not in session.laps[0].channels:
        return []

    time_resolution = 0.04  # 25Hz

    # Build flat sequence of (time, lap_index, sample_index)
    flat: list[tuple[float, int, int]] = []
    for lap in session.laps:
        mc = lap.channels["Master Clk"]
        for si, t in enumerate(mc.samples):
            if t is not None and not math.isnan(t):
                flat.append((t, lap.lap_index, si))

    if not flat:
        return []

    start = flat[0][0]
    end = flat[-1][0]
    n_steps = int((end - start) / time_resolution)

    index: list[TemporalIndexEntry] = []
    ptr = 0
    for step in range(n_steps):
        t = start + step * time_resolution
        while ptr < len(flat) - 1 and flat[ptr][0] < t:
            ptr += 1
        _, lap_idx, sample_idx = flat[ptr]
        index.append(TemporalIndexEntry(lap_index=lap_idx, sample_index=sample_idx))

    return index

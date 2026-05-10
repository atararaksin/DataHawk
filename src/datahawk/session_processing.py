"""Session processing: lap detection, reindexing by track position."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from datahawk.xrz_parser import ParsedSession, Channel as XrzChannel


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
    channels: dict[str, Channel] = field(default_factory=dict)


@dataclass
class Session:
    """Processed session with laps aligned by track position."""
    start_time: str
    date: str
    track: str
    samples_per_lap: int
    reference_lap_index: int
    laps: list[Lap] = field(default_factory=list)


def _find_lap_boundaries(parsed: ParsedSession) -> list[float]:
    """Detect lap boundaries from channel 4 (lap time) events.
    Returns list of lap boundary timestamps in seconds.
    
    Channel 4 stores lap times as uint32 milliseconds (not float32).
    At each lap crossing, it fires with the completed lap duration.
    Chain detection: each lap's end timestamp = next lap's start.
    """
    import zlib, struct

    # We need raw uint32 values from channel 4, but the parser decodes as float32.
    # Use the raw (S frame timestamps from channel 4 in the parsed data,
    # and reconstruct uint32 values from the float32 bit pattern.
    ch4 = parsed.channels.get(4)
    if not ch4 or not ch4.samples:
        return []

    NAN_VAL = 4294955006  # sentinel for "no lap time"

    # Reinterpret float32 -> uint32 (same bits, different type)
    events = []
    for t, fval in ch4.samples:
        raw = struct.unpack('<I', struct.pack('<f', fval))[0]
        events.append((t, raw))

    # Valid lap time events (45-90s = 45000-90000ms)
    valid_events = [(t, v) for t, v in events if 45000 <= v <= 90000]

    if not valid_events:
        return []

    # Chain laps: find consecutive events where each lap's start
    # (timestamp - lap_time) matches the previous lap's end timestamp.
    # Try each valid event as potential chain start, pick longest chain.
    best_chain = []

    for start_idx in range(len(valid_events)):
        ts_sec, val_ms = valid_events[start_idx]
        chain = [ts_sec - val_ms / 1000.0, ts_sec]
        current_end_ms = ts_sec * 1000

        for j in range(start_idx + 1, len(valid_events)):
            ts_j, val_j = valid_events[j]
            ts_j_ms = ts_j * 1000
            lap_start_ms = ts_j_ms - val_j
            if abs(lap_start_ms - current_end_ms) < 500:
                chain.append(ts_j)
                current_end_ms = ts_j_ms

        if len(chain) > len(best_chain):
            best_chain = chain

    return best_chain


def _interpolate_at(target_time: float, times: list[float], values: list[float]) -> Optional[float]:
    """Linear interpolation of values at target_time."""
    if not times or target_time < times[0] or target_time > times[-1]:
        return float('nan')
    # Binary search
    lo, hi = 0, len(times) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if times[mid] <= target_time:
            lo = mid
        else:
            hi = mid
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
    crossings = _find_lap_boundaries(parsed)

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
        )

    # Compute reference lap distance array
    ref_dist = _distance_along_track(ref_lats, ref_lons)

    # Collect all channel names we want to reindex
    channel_names = {}
    for ch_id, ch in parsed.channels.items():
        if ch.samples:
            channel_names[ch_id] = ch.name

    # Process each lap
    session = Session(
        start_time=parsed.metadata.time,
        date=parsed.metadata.date,
        track=parsed.metadata.track,
        samples_per_lap=samples_per_lap,
        reference_lap_index=fastest_idx,
    )

    cos_lat = math.cos(math.radians(ref_lats[0]))

    for lap_idx in range(len(crossings) - 1):
        lap_start = crossings[lap_idx]
        lap_end = crossings[lap_idx + 1]
        lap_time = lap_end - lap_start

        lap = Lap(lap_index=lap_idx, lap_time=lap_time)

        if lap_idx == fastest_idx:
            # Reference lap: time-based interpolation (uniform time samples)
            for ch_id, ch_name in channel_names.items():
                ch = parsed.channels[ch_id]
                resampled = []
                for t in ref_times:
                    val = _interpolate_at(t, ch.timestamps, ch.values)
                    resampled.append(val)
                lap.channels[ch_name] = Channel(name=ch_name, samples=resampled)
        else:
            # Other laps: distance-based interpolation
            # Get this lap's GPS positions and compute distance
            lap_gps_idx = [i for i, t in enumerate(gps_times) if lap_start <= t < lap_end]
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
                        val = _interpolate_at(t_at_dist, ch.timestamps, ch.values)
                        resampled.append(val if val is not None else float('nan'))
                lap.channels[ch_name] = Channel(name=ch_name, samples=resampled)

        session.laps.append(lap)

    return session

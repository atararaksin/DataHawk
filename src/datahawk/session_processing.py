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
    lap_start_time: float
    channels: dict[str, Channel] = field(default_factory=dict)


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


def _find_lap_boundaries(parsed: ParsedSession) -> list[float]:
    """Detect lap boundaries from channel 4 (lap time) events.
    
    Channel 4 emits NaN at each S/F crossing, then predictive time updates,
    then the actual lap time value when the next crossing occurs.
    A lap boundary event = value matches elapsed time since last boundary.
    """
    import zlib, struct

    # Read channel 4 raw from the XRZ (including NaN values the parser drops)
    # We need to re-parse channel 4 from the decompressed data
    from datahawk.xrz_parser import _CHS_PATTERN, _CHS_BODY_LEN
    import pathlib

    ch4 = parsed.channels.get(4)
    if not ch4 or not ch4.samples:
        return []

    # Reconstruct full ch4 events including NaN by reinterpreting parsed float32
    # AND finding the dropped NaN events from raw data
    # Since we can't easily re-read the file here, use the parsed timestamps
    # and reconstruct: the NaN events were at specific times before the first parsed sample

    # Get all parsed ch4 values as uint32
    events = []
    for t, fval in ch4.samples:
        raw = struct.unpack('<I', struct.pack('<f', fval))[0]
        events.append((t, raw))

    # The NaN events (dropped by parser) were at the start.
    # From our analysis: last NaN was ~1.6s before first parsed sample.
    # First parsed sample is a predictive time (~82000ms).
    # The NaN timestamp = first_parsed_time - gap (~1.6s)
    # But more precisely: NaN value = 4294955006, and it marks S/F crossing.
    # We know from CSV that first S/F was at ~122.4s (offset 9.161 from CSV's 113.236s)
    # Let's estimate: first_boundary = first_parsed_timestamp - first_parsed_value/1000
    # Because the first predictive value tells us "predicted lap time from current position"
    # and it was emitted shortly after the S/F crossing.

    # Actually simpler: use the chain rule starting from the first event.
    # The first real lap boundary is the event where:
    # timestamp - value ≈ some earlier reference point (the NaN/S/F crossing)
    # Since we don't have the exact NaN timestamp, find it by:
    # first_boundary_time = first event where (timestamp - value) is consistent
    # with subsequent events forming a chain.

    # Strategy: find events where value = elapsed time since last boundary (±500ms)
    # Start with the assumption that the first boundary is at
    # (first_valid_lap_event_timestamp - first_valid_lap_event_value)

    valid_events = [(t, v) for t, v in events if 45000 <= v <= 90000]
    if not valid_events:
        return []

    # Try each valid event as the first lap completion.
    # Its implied start = ts - val. Then chain forward.
    best_chain = []
    for start_idx in range(min(len(valid_events), 50)):  # only try first 50
        ts, val = valid_events[start_idx]
        lap_start_time = ts - val / 1000.0
        chain = [lap_start_time, ts]
        current_end_ms = ts * 1000

        for j in range(start_idx + 1, len(valid_events)):
            ts_j, val_j = valid_events[j]
            ts_j_ms = ts_j * 1000
            elapsed_since_last = ts_j_ms - current_end_ms
            # Lap boundary: reported value matches elapsed time since last boundary
            if abs(val_j - elapsed_since_last) < 500:
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
        if ch.samples:
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
                resampled = []
                for t in ref_times:
                    val = _interpolate_at(t, ch.timestamps, ch.values)
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

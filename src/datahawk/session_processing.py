"""Session processing: lap detection, reindexing by track position."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Optional

from datahawk.xrz_parser import XrzSession, XrzChannel as XrzChannel
from datahawk.lap_detection import detect_start_finish_fine, detect_laps
from datahawk.types import Channel, Lap, TemporalIndexEntry, Session, Track



def _find_nearest_points(
    ref_x: list[float], ref_y: list[float],
    lap_x: list[float], lap_y: list[float],
    max_radius: float,
) -> list[tuple[int, float]]:
    """For each ref point, find the nearest point on the current lap by 2D proximity.

    Returns list of (segment_index, fraction) tuples. (-1, 0) if no point within max_radius.
    Uses advancing pointer: only advances on match, searches up to 50 segments ahead.
    """
    n_ref = len(ref_x)
    n_lap = len(lap_x)
    result: list[tuple[int, float]] = []
    search_start = 0
    max_r2 = max_radius * max_radius
    MAX_SEARCH = 50

    for ri in range(n_ref):
        rx, ry = ref_x[ri], ref_y[ri]
        best_d2 = max_r2
        best_seg = -1
        best_frac = 0.0

        search_end = min(search_start + MAX_SEARCH, n_lap - 1)
        for li in range(search_start, search_end):
            sx = lap_x[li + 1] - lap_x[li]
            sy = lap_y[li + 1] - lap_y[li]
            seg_len2 = sx * sx + sy * sy
            if seg_len2 < 1e-12:
                d2 = (lap_x[li] - rx) ** 2 + (lap_y[li] - ry) ** 2
                frac = 0.0
            else:
                t = ((rx - lap_x[li]) * sx + (ry - lap_y[li]) * sy) / seg_len2
                t = max(0.0, min(1.0, t))
                px = lap_x[li] + t * sx
                py = lap_y[li] + t * sy
                d2 = (px - rx) ** 2 + (py - ry) ** 2
                frac = t

            if d2 < best_d2:
                best_d2 = d2
                best_seg = li
                best_frac = frac

        if best_seg >= 0:
            result.append((best_seg, best_frac))
            search_start = best_seg  # advance only on match
        else:
            result.append((-1, 0.0))

    return result


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


def process_session(parsed: XrzSession) -> Session:
    """Process a parsed XRZ session into position-indexed laps."""
    sf_line = detect_start_finish_fine(parsed)

    lap_times = detect_laps(parsed, sf_line)
    crossings = lap_times  # detect_laps returns crossing timestamps directly

    lat_ch = parsed.channels.get(-1)
    lon_ch = parsed.channels.get(-2)
    speed_ch = parsed.channels.get(-3)

    if len(crossings) < 2 or not lat_ch or not lon_ch:
        return Session(
            start_time=parsed.metadata.time,
            date=parsed.metadata.date,
            track=Track(name=parsed.metadata.track, sf_line=sf_line),
            samples_per_lap=0,
            reference_lap_index=0,
            best_lap_index=0,
            best_lap_time=0.0,
        )

    # Build full boundary list: session_start, crossings..., session_end
    mclk_ch = parsed.channels.get(0)
    session_start_time = mclk_ch.timestamps[0] if mclk_ch and mclk_ch.timestamps else crossings[0]
    session_end_time = mclk_ch.timestamps[-1] if mclk_ch and mclk_ch.timestamps else crossings[-1]
    boundaries = [session_start_time] + list(crossings) + [session_end_time]

    # Compute lap times for all laps (including out-lap and in-lap)
    lap_times = [boundaries[i+1] - boundaries[i] for i in range(len(boundaries)-1)]

    # Fastest lap: exclude first (out-lap) and last (in-lap)
    full_lap_range = range(1, len(lap_times) - 1)
    fastest_idx = min(full_lap_range, key=lambda i: lap_times[i])

    # Reference lap: use GPS at 25Hz
    ref_start = boundaries[fastest_idx]
    ref_end = boundaries[fastest_idx + 1]
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
            track=Track(name=parsed.metadata.track, sf_line=sf_line), samples_per_lap=0, reference_lap_index=0,
            best_lap_index=0, best_lap_time=0.0,
        )

    # Reference lap in local meter coordinates (for perpendicular intersection)
    cos_lat = math.cos(math.radians(ref_lats[0]))
    ref_x = [(lon - ref_lons[0]) * 111000 * cos_lat for lon in ref_lons]
    ref_y = [(lat - ref_lats[0]) * 111000 for lat in ref_lats]

    # Collect all channel names we want to reindex
    channel_names = {}
    for ch_id, ch in parsed.channels.items():
        if ch.timestamps:
            channel_names[ch_id] = ch.name

    # Process each lap
    session = Session(
        start_time=parsed.metadata.time,
        date=parsed.metadata.date,
        track=Track(name=parsed.metadata.track, sf_line=sf_line),
        samples_per_lap=samples_per_lap,
        reference_lap_index=fastest_idx,
        best_lap_index=fastest_idx,
        best_lap_time=lap_times[fastest_idx],
    )

    for lap_idx in range(len(boundaries) - 1):
        lap_start_time = boundaries[lap_idx]
        lap_end = boundaries[lap_idx + 1]
        lap_time = lap_end - lap_start_time

        lap = Lap(lap_index=len(session.laps), lap_time=lap_time, lap_start_time=lap_start_time)

        if lap_start_time == ref_start:
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
                lap.channels[ch_name] = Channel(
                    name=ch_name, samples=resampled,
                    raw_timestamps=[t - ref_start for t in ch_times],
                    raw_values=list(ch_vals),
                )
        else:
            # Other laps: spatial reindexing
            # For each ref sample, find where current lap crosses the perpendicular
            # line through that ref point (normal to ref trajectory)
            lap_gps_idx = [i for i, t in enumerate(gps_times) if lap_start_time <= t < lap_end]
            if len(lap_gps_idx) < 10:
                for ch_id, ch_name in channel_names.items():
                    ch = parsed.channels[ch_id]
                    lo_i = max(0, bisect_left(ch.timestamps, lap_start_time) - 1)
                    hi_i = min(len(ch.timestamps), bisect_right(ch.timestamps, lap_end) + 1)
                    lap.channels[ch_name] = Channel(
                        name=ch_name, samples=[float('nan')] * samples_per_lap,
                        raw_timestamps=[t - lap_start_time for t in ch.timestamps[lo_i:hi_i]],
                        raw_values=list(ch.values[lo_i:hi_i]),
                    )
                session.laps.append(lap)
                continue

            lap_lats = [gps_lats[i] for i in lap_gps_idx]
            lap_lons = [gps_lons[i] for i in lap_gps_idx]
            lap_times_arr = [gps_times[i] for i in lap_gps_idx]

            # Convert to local meters for intersection math
            lap_x = [(lon - ref_lons[0]) * 111000 * cos_lat for lon in lap_lons]
            lap_y = [(lat - ref_lats[0]) * 111000 for lat in lap_lats]

            # Find interpolation fractions: for each ref sample, where does
            # current lap cross the perpendicular line?
            MAX_RADIUS = 8.0  # meters
            fracs = _find_nearest_points(ref_x, ref_y, lap_x, lap_y, MAX_RADIUS)

            # Interpolate all channels using the crossing fractions
            for ch_id, ch_name in channel_names.items():
                ch = parsed.channels[ch_id]
                lo_i = max(0, bisect_left(ch.timestamps, lap_start_time) - 1)
                hi_i = min(len(ch.timestamps), bisect_right(ch.timestamps, lap_end) + 1)
                ch_times = ch.timestamps[lo_i:hi_i]
                ch_vals = ch.values[lo_i:hi_i]
                resampled = []
                for s_idx in range(samples_per_lap):
                    seg_idx, frac = fracs[s_idx]
                    if seg_idx < 0:
                        resampled.append(float('nan'))
                    else:
                        t_interp = lap_times_arr[seg_idx] + frac * (
                            lap_times_arr[seg_idx + 1] - lap_times_arr[seg_idx]
                        )
                        val = _interpolate_at(t_interp, ch_times, ch_vals)
                        resampled.append(val if val is not None else float('nan'))
                # Raw data: timestamps relative to lap start, values as-is
                raw_ts = [t - lap_start_time for t in ch_times]
                lap.channels[ch_name] = Channel(
                    name=ch_name, samples=resampled,
                    raw_timestamps=raw_ts, raw_values=list(ch_vals),
                )

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

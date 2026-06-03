"""Session processing: lap detection, reindexing by track position."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from typing import Optional

from datahawk.source.types import SourceSession
from datahawk.source.channel_constants import GPS_LATITUDE, GPS_LONGITUDE, MASTER_CLK, BEACON
from datahawk.session_processing.lap_detection import detect_sf_from_mychron_beacon, detect_laps, detect_sf_from_max_speed
from datahawk.session_processing.synthetic_channels import add_lap_level_synthetic_channels
from datahawk.types import Channel, Lap, TemporalIndexEntry, Session, Track, Line, MasterLap


def detect_sf_line(source_session: SourceSession) -> Line:
    """Detect start/finish line from session data.

    Uses ch4-based S/F detection if available, otherwise max-speed method.
    """
    ch4 = source_session.channels.get(BEACON)
    if ch4 and ch4.timestamps:
        return detect_sf_from_mychron_beacon(source_session, ch4)
    else:
        return detect_sf_from_max_speed(source_session)


def detect_master_lap(source_session: SourceSession, sf_line: Line) -> MasterLap:
    """Detect the master (fastest) lap and return its GPS coordinates."""
    boundaries = detect_laps(source_session, sf_line)

    lat_ch = source_session.channels.get(GPS_LATITUDE)
    lon_ch = source_session.channels.get(GPS_LONGITUDE)

    if len(boundaries) < 4 or not lat_ch or not lon_ch:
        raise ValueError("Not enough laps or GPS data to detect master lap")

    lap_times = [boundaries[i + 1] - boundaries[i] for i in range(len(boundaries) - 1)]

    # Fastest lap: exclude first (out-lap) and last (in-lap)
    full_lap_range = range(1, len(lap_times) - 1)
    fastest_idx = min(full_lap_range, key=lambda i: lap_times[i])

    ref_start = boundaries[fastest_idx]
    ref_end = boundaries[fastest_idx + 1]
    gps_times = lat_ch.timestamps

    ref_indices = [i for i, t in enumerate(gps_times) if ref_start <= t < ref_end]
    master_lap_lats = [lat_ch.values[i] for i in ref_indices]
    master_lap_lons = [lon_ch.values[i] for i in ref_indices]

    return MasterLap(lats=master_lap_lats, lons=master_lap_lons)


def build_session(
    source_session: SourceSession,
    track: Track,
) -> Session:
    """Build a Session by reindexing all laps against the master lap trajectory."""

    boundaries = detect_laps(source_session, track.sf_line)

    lat_ch = source_session.channels.get(GPS_LATITUDE)
    lon_ch = source_session.channels.get(GPS_LONGITUDE)

    master_lap_lats = track.master_lap.lats
    master_lap_lons = track.master_lap.lons

    if len(boundaries) < 4 or not lat_ch or not lon_ch:
        return Session(
            start_time=source_session.metadata.time,
            date=source_session.metadata.date,
            track=track,
            samples_per_lap=0, reference_lap_index=0,
            best_lap_index=0, best_lap_time=0.0,
        )

    samples_per_lap = len(master_lap_lats)
    if samples_per_lap < 10:
        return Session(
            start_time=source_session.metadata.time, date=source_session.metadata.date,
            track=track,
            samples_per_lap=0, reference_lap_index=0,
            best_lap_index=0, best_lap_time=0.0,
        )

    lap_times = [boundaries[i + 1] - boundaries[i] for i in range(len(boundaries) - 1)]
    full_lap_range = range(1, len(lap_times) - 1)
    fastest_idx = min(full_lap_range, key=lambda i: lap_times[i])

    # Master lap in local meter coordinates
    cos_lat = math.cos(math.radians(master_lap_lats[0]))
    master_x = [(lon - master_lap_lons[0]) * 111000 * cos_lat for lon in master_lap_lons]
    master_y = [(lat - master_lap_lats[0]) * 111000 for lat in master_lap_lats]

    gps_times = lat_ch.timestamps
    gps_lats = lat_ch.values
    gps_lons = lon_ch.values

    channel_names = [name for name, ch in source_session.channels.items() if ch.timestamps]

    session = Session(
        start_time=source_session.metadata.time,
        date=source_session.metadata.date,
        track=track,
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

        # Spatial reindexing for all laps
        lap_gps_idx = [i for i, t in enumerate(gps_times) if lap_start_time <= t < lap_end]
        if len(lap_gps_idx) < 10:
            for ch_name in channel_names:
                ch = source_session.channels[ch_name]
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

        lap_x = [(lon - master_lap_lons[0]) * 111000 * cos_lat for lon in lap_lons]
        lap_y = [(lat - master_lap_lats[0]) * 111000 for lat in lap_lats]

        MAX_RADIUS = 8.0
        fracs = _find_nearest_points(master_x, master_y, lap_x, lap_y, MAX_RADIUS)

        for ch_name in channel_names:
            ch = source_session.channels[ch_name]
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
            raw_ts = [t - lap_start_time for t in ch_times]
            lap.channels[ch_name] = Channel(
                name=ch_name, samples=resampled,
                raw_timestamps=raw_ts, raw_values=list(ch_vals),
            )

        session.laps.append(lap)

    # Add lap-level synthetic channels
    for lap in session.laps:
        add_lap_level_synthetic_channels(lap)

    # Build temporal index
    session.temporal_index = _build_temporal_index(session)

    return session


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


def _build_temporal_index(session: Session) -> list[TemporalIndexEntry]:
    """Build time-to-position mapping starting at session start time.

    Advances through laps at time_resolution increments. For each time step,
    finds the current lap and the latest valid reindexed sample index.
    When reindexed data has gaps (NaN), the sample index freezes until
    valid data resumes.
    """
    if not session.laps or session.samples_per_lap == 0:
        return []

    time_resolution = session.time_resolution
    start = session.laps[0].lap_start_time
    end = session.laps[-1].lap_start_time + session.laps[-1].lap_time
    n_steps = int((end - start) / time_resolution)

    index: list[TemporalIndexEntry] = []
    current_lap_idx = 0
    current_sample_idx = 0

    for step in range(n_steps):
        t = start + step * time_resolution

        # Advance lap if needed
        while (current_lap_idx < len(session.laps) - 1 and
               t >= session.laps[current_lap_idx + 1].lap_start_time):
            current_lap_idx += 1
            current_sample_idx = 0

        # Advance sample index within current lap's reindexed Master Clk
        lap = session.laps[current_lap_idx]
        mc = lap.master_clk
        if mc:
            # Advance pointer to the sample closest to t
            while (current_sample_idx < len(mc.samples) - 1):
                next_val = mc.samples[current_sample_idx + 1]
                if next_val is None or math.isnan(next_val):
                    break
                cur_val = mc.samples[current_sample_idx]
                if cur_val is None or math.isnan(cur_val):
                    current_sample_idx += 1
                elif abs(next_val - t) <= abs(cur_val - t):
                    current_sample_idx += 1
                    current_sample_idx += 1
                else:
                    break

        index.append(TemporalIndexEntry(lap_index=current_lap_idx, sample_index=current_sample_idx))

    return index

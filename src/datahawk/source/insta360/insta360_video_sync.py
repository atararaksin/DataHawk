"""Insta360 video-to-telemetry synchronization.

Uses accelerometer cross-correlation from the embedded IMU data in the inst box.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import NamedTuple

from datahawk.source.types import SourceSession
from datahawk.source.channel_constants import GPS_LAT_ACC, GPS_LON_ACC
from datahawk.source.insta360.insta360_parser import detect as _detect_insta360, parse as _parse_insta360


class SyncResult(NamedTuple):
    """Result of video-telemetry synchronization."""
    offset_seconds: float  # video_time = mycron_time + offset
    correlation: float  # peak correlation strength (0-1)
    confidence: str  # "high", "medium", "low"
    method: str  # "accel"


def is_insta360_video(path: str | Path) -> bool:
    """Detect if an MP4 file is from an Insta360 camera (has inst telemetry box)."""
    return _detect_insta360(str(path))


def sync_by_acceleration(video_path: str | Path, session: SourceSession) -> SyncResult:
    """Find time offset between an Insta360 MP4 and a MyChron session.

    Uses horizontal acceleration magnitude cross-correlation.
    Returns offset such that: video_time = mycron_time + offset
    """
    insta_mag = _extract_insta360_accel_magnitude(Path(video_path))
    mycron_mag = _compute_mycron_accel_magnitude(session)

    if not insta_mag or not mycron_mag:
        raise ValueError("Could not extract acceleration data from one or both sources")

    # Resample both to uniform 25Hz
    g_sig = _resample_25hz(insta_mag)
    m_sig = _resample_25hz(mycron_mag)

    # Cross-correlate
    offset_samples, corr = _cross_correlate(g_sig, m_sig)
    offset_s = offset_samples / 25.0

    confidence = "high" if corr > 0.4 else "medium" if corr > 0.25 else "low"

    return SyncResult(offset_seconds=offset_s, correlation=corr, confidence=confidence, method="accel")


def _extract_insta360_accel_magnitude(path: Path) -> list[tuple[float, float]]:
    """Extract horizontal acceleration magnitude from Insta360 IMU data.

    Returns list of (time_seconds_relative_to_video_start, magnitude_g).
    Downsamples from ~1000Hz to ~50Hz for efficiency.
    """
    telem = _parse_insta360(str(path))
    if not telem.accelerometer:
        raise ValueError("No accelerometer data found in Insta360 video")

    # Convert timestamps to be relative to video start
    # Both raw IMU timestamps and first_frame_timestamp are in microseconds;
    # our parser already converts raw to seconds, so convert fft to seconds too
    video_start_s = telem.first_frame_timestamp_us / 1_000_000.0

    # Downsample from ~1000Hz to ~50Hz (take every 20th sample)
    duration = telem.accelerometer[-1][0] - telem.accelerometer[0][0]
    step = max(1, int(len(telem.accelerometer) / (50 * duration))) if duration > 0 else 1

    result = []
    for i in range(0, len(telem.accelerometer), step):
        t, ax, ay, az = telem.accelerometer[i]
        t_rel = t - video_start_s
        # Horizontal magnitude (assuming Y is gravity axis based on first samples)
        mag = math.sqrt(ax ** 2 + az ** 2)
        result.append((t_rel, mag))

    return result


def _compute_mycron_accel_magnitude(session: SourceSession) -> list[tuple[float, float]]:
    """Compute horizontal acceleration magnitude from MyChron GPS data."""
    lat_ch = session.channels.get(GPS_LAT_ACC)
    lon_ch = session.channels.get(GPS_LON_ACC)

    if not lat_ch or not lon_ch:
        raise ValueError("Session missing GPS Lat Acc / Lon Acc channels")

    n = min(len(lat_ch.values), len(lon_ch.values))
    return [(lat_ch.timestamps[i], math.sqrt(lat_ch.values[i] ** 2 + lon_ch.values[i] ** 2))
            for i in range(n)]


def _resample_25hz(time_val_pairs: list[tuple[float, float]]) -> list[float]:
    """Resample time-value pairs to uniform 25Hz."""
    duration = time_val_pairs[-1][0] - time_val_pairs[0][0]
    t_start = time_val_pairs[0][0]
    n = int(duration * 25)
    result = []
    idx = 0
    for i in range(n):
        t = t_start + i * 0.04
        while idx < len(time_val_pairs) - 1 and time_val_pairs[idx + 1][0] < t:
            idx += 1
        if idx >= len(time_val_pairs) - 1:
            result.append(time_val_pairs[-1][1])
        else:
            t0, v0 = time_val_pairs[idx]
            t1, v1 = time_val_pairs[idx + 1]
            frac = (t - t0) / (t1 - t0) if t1 > t0 else 0
            result.append(v0 + frac * (v1 - v0))
    return result


def _cross_correlate(g_sig: list[float], m_sig: list[float],
                     max_lag_seconds: int = 1500) -> tuple[int, float]:
    """Cross-correlate two signals using coarse-to-fine search."""
    def normalize(sig):
        n = len(sig)
        mean = sum(sig) / n
        sig = [s - mean for s in sig]
        std = (sum(s ** 2 for s in sig) / n) ** 0.5
        return [s / std for s in sig] if std > 0 else sig

    def _search(g, m, max_lag, min_overlap):
        n_g, n_m = len(g), len(m)
        best_corr, best_lag = -1.0, 0
        for lag in range(-max_lag, max_lag):
            g_start = max(0, lag)
            g_end = min(n_g, lag + n_m)
            m_start = max(0, -lag)
            overlap = g_end - g_start
            if overlap < min_overlap:
                continue
            corr = sum(g[g_start + i] * m[m_start + i]
                       for i in range(overlap)) / overlap
            if corr > best_corr:
                best_corr = corr
                best_lag = lag
        return best_lag, best_corr

    # Coarse: downsample to 2Hz, search full range
    step = 12
    g_coarse = normalize(g_sig[::step])
    m_coarse = normalize(m_sig[::step])
    coarse_lag, _ = _search(g_coarse, m_coarse,
                            max_lag_seconds * 2,
                            min(len(g_coarse), len(m_coarse)) // 2)

    # Fine: full 25Hz, search ±1s around coarse result
    g_fine = normalize(g_sig)
    m_fine = normalize(m_sig)
    center = coarse_lag * step
    fine_radius = 25

    n_g, n_m = len(g_fine), len(m_fine)
    best_corr, best_lag = -1.0, 0
    for lag in range(center - fine_radius, center + fine_radius):
        g_start = max(0, lag)
        g_end = min(n_g, lag + n_m)
        m_start = max(0, -lag)
        overlap = g_end - g_start
        if overlap < 25 * 60:
            continue
        corr = sum(g_fine[g_start + i] * m_fine[m_start + i]
                   for i in range(overlap)) / overlap
        if corr > best_corr:
            best_corr = corr
            best_lag = lag

    return best_lag, best_corr

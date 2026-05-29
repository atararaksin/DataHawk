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
    Returns offset such that: master_clk_time = video_time + offset
    """
    insta_mag = _extract_insta360_accel_magnitude(Path(video_path))
    mycron_mag = _compute_mycron_accel_magnitude(session)

    if not insta_mag or not mycron_mag:
        raise ValueError("Could not extract acceleration data from one or both sources")

    # Get the Master Clk base (session start in Master Clk time)
    lat_ch = session.channels.get(GPS_LAT_ACC)
    master_clk_base = lat_ch.timestamps[0] if lat_ch and lat_ch.timestamps else 0

    # Remember start times before resampling strips them
    insta_t0 = insta_mag[0][0]
    mycron_t0 = mycron_mag[0][0]  # ~0 after subtracting base

    # Resample both to uniform 25Hz
    g_sig = _resample_25hz(insta_mag)
    m_sig = _resample_25hz(mycron_mag)

    # Cross-correlate: finds lag such that g_sig[lag:] aligns with m_sig[0:]
    lag_samples, corr = _cross_correlate(g_sig, m_sig)
    lag_seconds = lag_samples / 25.0

    # Convert lag to absolute offset:
    # g_sig[lag] corresponds to m_sig[0]
    # (insta_t0 + lag_seconds) in video time = (mycron_t0) in session-relative time
    # We want: master_clk = video_time + offset
    # master_clk_base + mycron_t0 = (insta_t0 + lag_seconds) + offset
    offset_s = master_clk_base + mycron_t0 - insta_t0 - lag_seconds

    confidence = "high" if corr > 0.4 else "medium" if corr > 0.25 else "low"

    return SyncResult(offset_seconds=offset_s, correlation=corr, confidence=confidence, method="accel")


def _extract_insta360_accel_magnitude(path: Path) -> list[tuple[float, float]]:
    """Extract horizontal acceleration magnitude from Insta360 IMU data.

    Returns list of (time_seconds_relative_to_video_start, magnitude_g).
    Low-pass filters at ~25Hz (moving average) then downsamples to ~50Hz.
    This matches the bandwidth of GPS-derived acceleration from MyChron.
    """
    telem = _parse_insta360(str(path))
    if not telem.accelerometer:
        raise ValueError("No accelerometer data found in Insta360 video")

    # Convert timestamps to be relative to video start
    video_start_s = telem.first_frame_timestamp_us / 1_000_000.0

    # Low-pass filter: moving average over 40 samples at ~1000Hz ≈ 25Hz cutoff
    # Then downsample to ~50Hz (every 20th sample)
    n = len(telem.accelerometer)
    window = 40
    step = 20

    result = []
    for i in range(window // 2, n - window // 2, step):
        t = telem.accelerometer[i][0] - video_start_s
        ax_sum = sum(telem.accelerometer[j][1] for j in range(i - window // 2, i + window // 2))
        az_sum = sum(telem.accelerometer[j][3] for j in range(i - window // 2, i + window // 2))
        mag = math.sqrt((ax_sum / window) ** 2 + (az_sum / window) ** 2)
        result.append((t, mag))

    return result


def _compute_mycron_accel_magnitude(session: SourceSession) -> list[tuple[float, float]]:
    """Compute horizontal acceleration magnitude from MyChron GPS data.
    
    Returns timestamps relative to session start (not raw Master Clk).
    """
    lat_ch = session.channels.get(GPS_LAT_ACC)
    lon_ch = session.channels.get(GPS_LON_ACC)

    if not lat_ch or not lon_ch:
        raise ValueError("Session missing GPS Lat Acc / Lon Acc channels")

    n = min(len(lat_ch.values), len(lon_ch.values))
    # Subtract session start time so timestamps begin near 0
    t_offset = lat_ch.timestamps[0] if lat_ch.timestamps else 0
    return [(lat_ch.timestamps[i] - t_offset, math.sqrt(lat_ch.values[i] ** 2 + lon_ch.values[i] ** 2))
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

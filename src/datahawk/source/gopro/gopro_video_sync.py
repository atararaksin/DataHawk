"""GoPro video-to-telemetry synchronization.

Two methods:
- Accelerometer cross-correlation (works without GPS on camera)
- Timestamp-based (requires GPS-synced clock on camera)
"""

from __future__ import annotations

import datetime
import math
from pathlib import Path
from typing import NamedTuple

import av

from datahawk.source.types import SourceSession
from datahawk.source.channel_constants import GPS_LAT_ACC, GPS_LON_ACC
from datahawk.source.gopro.gopro_parser import extract_gopro_accel_magnitude
from datahawk.utils.mp4_utils import get_mp4_creation_time


def is_gopro_video(path: str | Path) -> bool:
    """Detect if an MP4 file is from a GoPro (has GPMF telemetry track)."""
    try:
        container = av.open(str(path))
        for stream in container.streams:
            if hasattr(stream, 'metadata'):
                handler = stream.metadata.get('handler_name', '')
                if 'GoPro' in handler or 'GPMF' in handler:
                    container.close()
                    return True
        container.close()
    except Exception:
        pass
    return False


class SyncResult(NamedTuple):
    """Result of video-telemetry synchronization."""
    offset_seconds: float  # video_time = mycron_time + offset
    correlation: float  # peak correlation strength (0-1), or 1.0 for timestamp method
    confidence: str  # "high", "medium", "low"
    method: str  # "accel" or "timestamp"


def sync_by_acceleration(video_path: str | Path, session: SourceSession) -> SyncResult:
    """Find time offset between a GoPro MP4 and a MyChron session.

    Uses horizontal acceleration magnitude cross-correlation.
    Returns offset such that: video_time = mycron_time + offset
    """
    gopro_mag, timo = extract_gopro_accel_magnitude(Path(video_path))
    mycron_mag = _compute_mycron_accel_magnitude(session)

    if not gopro_mag or not mycron_mag:
        raise ValueError("Could not extract acceleration data from one or both sources")

    # Resample both to uniform 25Hz
    g_sig = _resample_25hz(gopro_mag)
    m_sig = _resample_25hz(mycron_mag)

    # Cross-correlate
    offset_samples, corr = _cross_correlate(g_sig, m_sig)
    offset_s = offset_samples / 25.0

    # Apply TIMO correction: GPMF telemetry starts timo seconds before video.
    # Cross-correlation aligned telemetry streams, but video starts later than telemetry.
    # To make video_time = mycron_time + offset correct, add timo.
    offset_s += timo

    # Assess confidence based on peak sharpness
    confidence = "high" if corr > 0.4 else "medium" if corr > 0.25 else "low"

    return SyncResult(offset_seconds=offset_s, correlation=corr, confidence=confidence, method="accel")


def sync_by_timestamp(video_path: str | Path, session: SourceSession) -> SyncResult:
    """Find time offset using MP4 creation timestamp vs MyChron session start.

    Requires the camera's clock to be GPS-synced (accurate).
    Returns offset such that: video_time = mycron_time + offset
    """
    video_start = get_mp4_creation_time(Path(video_path))
    if video_start is None:
        return SyncResult(offset_seconds=0, correlation=0, confidence="low", method="timestamp")

    # Parse MyChron session start time
    # session.metadata has date="05/02/2026" and time="14:35:42"
    try:
        date_str = session.metadata.date  # "MM/DD/YYYY"
        time_str = session.metadata.time  # "HH:MM:SS"
        session_start = datetime.datetime.strptime(
            f"{date_str} {time_str}", "%m/%d/%Y %H:%M:%S"
        ).replace(tzinfo=datetime.timezone.utc)
    except (ValueError, AttributeError):
        return SyncResult(offset_seconds=0, correlation=0, confidence="low", method="timestamp")

    # offset = video_start - session_start
    offset_s = (video_start - session_start).total_seconds()

    return SyncResult(offset_seconds=offset_s, correlation=1.0, confidence="high", method="timestamp")


# Convenience wrapper (legacy name)
sync_gopro_to_session = sync_by_acceleration


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
    duration = time_val_pairs[-1][0]
    n = int(duration * 25)
    result = []
    idx = 0
    for i in range(n):
        t = i * 0.04
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
    """Cross-correlate two signals using coarse-to-fine search.

    Coarse pass at 2Hz finds approximate offset,
    then fine pass at 25Hz refines within ±1s.
    Returns (best_lag_samples_at_25Hz, correlation).
    """
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
    step = 12  # 25Hz / 2Hz ≈ 12
    g_coarse = normalize(g_sig[::step])
    m_coarse = normalize(m_sig[::step])
    coarse_lag, _ = _search(g_coarse, m_coarse,
                            max_lag_seconds * 2,
                            min(len(g_coarse), len(m_coarse)) // 2)

    # Fine: full 25Hz, search ±1s around coarse result
    g_fine = normalize(g_sig)
    m_fine = normalize(m_sig)
    center = coarse_lag * step
    fine_radius = 1 * 25

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

"""Video-to-telemetry synchronization via accelerometer cross-correlation."""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import NamedTuple

from datahawk.xrz_parser import ParsedSession

_GPS_LATACC_ID = -7
_GPS_LONACC_ID = -8


class SyncResult(NamedTuple):
    """Result of video-telemetry synchronization."""
    offset_seconds: float  # positive = video started before telemetry
    correlation: float  # peak correlation strength (0-1)
    confidence: str  # "high", "medium", "low"


def sync_gopro_to_session(video_path: str | Path, session: ParsedSession) -> SyncResult:
    """Find time offset between a GoPro MP4 and a MyChron session.

    Uses horizontal acceleration magnitude cross-correlation.
    Returns offset such that: video_time = mycron_time + offset
    """
    gopro_mag, timo = _extract_gopro_accel_magnitude(Path(video_path))
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

    return SyncResult(offset_seconds=offset_s, correlation=corr, confidence=confidence)


def _extract_gopro_accel_magnitude(path: Path) -> tuple[list[tuple[float, float]], float]:
    """Extract horizontal acceleration magnitude from GoPro GPMF at ~25Hz.

    Returns (time_value_pairs, timo) where timo is the telemetry-to-video offset.
    """
    with open(path, "rb") as f:
        stco_data, stsz_data, sample_count = _find_gpmf_track(f)
        if sample_count == 0:
            return [], 0.0

        raw = []
        timo = 0.0
        for i in range(sample_count):
            off = struct.unpack(">I", stco_data[8 + i * 4:12 + i * 4])[0]
            sz = struct.unpack(">I", stsz_data[12 + i * 4:16 + i * 4])[0]
            f.seek(off)
            sample = f.read(sz)
            _parse_accl_from_sample(sample, i, raw)
            # Extract TIMO (telemetry-to-video offset) from first sample that has it
            if timo == 0.0:
                timo_idx = sample.find(b"TIMO")
                if timo_idx >= 0 and timo_idx + 12 <= len(sample):
                    timo = struct.unpack(">f", sample[timo_idx + 8:timo_idx + 12])[0]

    if not raw:
        return [], 0.0

    # TIMO is extracted but applied post-correlation (see sync_gopro_to_session)

    # Low-pass filter: average over 8 samples (200Hz -> 25Hz)
    window = 8
    filtered = []
    for i in range(0, len(raw) - window, window):
        chunk = raw[i:i + window]
        t = sum(s[0] for s in chunk) / window
        a = sum(s[1] for s in chunk) / window
        b = sum(s[2] for s in chunk) / window
        filtered.append((t, a, b))

    # Subtract static bias (first 3 seconds = gravity leakage from tilt)
    cal_n = min(75, len(filtered))
    bias_a = sum(s[1] for s in filtered[:cal_n]) / cal_n
    bias_b = sum(s[2] for s in filtered[:cal_n]) / cal_n

    # Horizontal magnitude in g
    mag = [(t, math.sqrt(((a - bias_a) / 9.81) ** 2 + ((b - bias_b) / 9.81) ** 2))
           for t, a, b in filtered]
    return mag, timo


def _find_gpmf_track(f) -> tuple[bytes, bytes, int]:
    """Find the GPMF telemetry track's stco and stsz data."""
    # Read file size
    f.seek(0, 2)
    file_size = f.tell()
    f.seek(0)

    # Find moov box
    moov_offset = _find_top_level_box(f, file_size, b"moov")
    if moov_offset < 0:
        return b"", b"", 0

    f.seek(moov_offset)
    moov_size = struct.unpack(">I", f.read(4))[0]
    f.read(4)  # skip 'moov'
    moov_data = f.read(moov_size - 8)

    # Find trak boxes with 'meta' handler and timescale=1000 (GPMF track)
    pos = 0
    while pos + 8 <= len(moov_data):
        size = struct.unpack(">I", moov_data[pos:pos + 4])[0]
        btype = moov_data[pos + 4:pos + 8]
        if size < 8:
            break
        if btype == b"trak":
            trak = moov_data[pos + 8:pos + size]
            # Check if this is the GPMF track (meta handler, timescale=1000)
            if b"ACCL" in moov_data[pos:pos + size] or _is_gpmf_track(trak):
                stco, stsz, count = _extract_sample_table(trak)
                if count > 100:  # GPMF track has hundreds of samples
                    return stco, stsz, count
        pos += size

    return b"", b"", 0


def _is_gpmf_track(trak_data: bytes) -> bool:
    """Check if a trak contains GPMF telemetry (meta handler, ~1000 timescale)."""
    hdlr_idx = trak_data.find(b"hdlr")
    if hdlr_idx < 0:
        return False
    handler = trak_data[hdlr_idx + 12:hdlr_idx + 16]
    if handler != b"meta":
        return False
    # Check timescale in mdhd
    mdhd_idx = trak_data.find(b"mdhd")
    if mdhd_idx < 0:
        return False
    version = trak_data[mdhd_idx + 4]
    if version == 0:
        timescale = struct.unpack(">I", trak_data[mdhd_idx + 16:mdhd_idx + 20])[0]
    else:
        timescale = struct.unpack(">I", trak_data[mdhd_idx + 24:mdhd_idx + 28])[0]
    return timescale == 1000


def _extract_sample_table(trak_data: bytes) -> tuple[bytes, bytes, int]:
    """Extract stco and stsz from a trak's stbl."""
    stco_idx = trak_data.find(b"stco")
    stsz_idx = trak_data.find(b"stsz")
    if stco_idx < 0 or stsz_idx < 0:
        return b"", b"", 0

    # stco: size(4) + 'stco'(4) already found at stco_idx-4
    stco_size = struct.unpack(">I", trak_data[stco_idx - 4:stco_idx])[0]
    stco_data = trak_data[stco_idx - 4:stco_idx - 4 + stco_size]

    stsz_size = struct.unpack(">I", trak_data[stsz_idx - 4:stsz_idx])[0]
    stsz_data = trak_data[stsz_idx - 4:stsz_idx - 4 + stsz_size]

    # Entry count from stco (offset 4 into payload: version+flags(4) + count(4))
    count = struct.unpack(">I", stco_data[12:16])[0]
    return stco_data[8:], stsz_data[8:], count


def _find_top_level_box(f, file_size: int, box_type: bytes) -> int:
    """Find a top-level MP4 box by type. Returns offset or -1."""
    f.seek(0)
    while f.tell() < file_size:
        pos = f.tell()
        header = f.read(8)
        if len(header) < 8:
            break
        size = struct.unpack(">I", header[:4])[0]
        btype = header[4:8]
        if size == 0:
            size = file_size - pos
        elif size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
        if btype == box_type:
            return pos
        f.seek(pos + size)
    return -1


def _parse_accl_from_sample(sample: bytes, sample_idx: int, out: list) -> None:
    """Parse ACCL data from a GPMF sample, append (t, a, b) tuples to out."""
    accl_idx = sample.find(b"ACCL")
    if accl_idx < 0:
        return

    # Find SCAL
    strm_start = sample.rfind(b"STRM", 0, accl_idx)
    scal_idx = sample.find(b"SCAL", strm_start if strm_start >= 0 else 0, accl_idx)
    scale = 418
    if scal_idx >= 0 and sample[scal_idx + 5] == 2:
        scale = struct.unpack(">h", sample[scal_idx + 8:scal_idx + 10])[0]

    struct_size = sample[accl_idx + 5]
    repeat = struct.unpack(">H", sample[accl_idx + 6:accl_idx + 8])[0]
    if struct_size != 6 or repeat == 0:
        return

    payload = sample[accl_idx + 8:accl_idx + 8 + 6 * repeat]
    for j in range(repeat):
        if (j + 1) * 6 > len(payload):
            break
        a, b, c = struct.unpack(">3h", payload[j * 6:(j + 1) * 6])
        t = sample_idx + j / repeat
        # a, b are horizontal axes; c is gravity axis
        out.append((t, a / scale, b / scale))


def _compute_mycron_accel_magnitude(session: ParsedSession) -> list[tuple[float, float]]:
    """Compute horizontal acceleration magnitude from MyChron GPS data."""
    lat_ch = session.channels.get(_GPS_LATACC_ID)
    lon_ch = session.channels.get(_GPS_LONACC_ID)

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
                            60 * 2)

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

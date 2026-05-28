"""Parse GoPro MP4 GPMF telemetry into a SourceSession structure.

Extracts GPS5 (lat, lon, alt, speed2D, speed3D) and computes heading from
position deltas. Produces a SourceSession with channels:
  -1: GPS Latitude
  -2: GPS Longitude
  -3: GPS Speed
  -10: GPS Heading
   0: Master Clk
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

from datahawk.source.types import SourceChannel, SourceSession, SourceSessionMetadata
from datahawk.source.channel_constants import (
    GPS_LATITUDE, GPS_LONGITUDE, GPS_SPEED, GPS_HEADING, MASTER_CLK,
    GPS_LAT_ACC, GPS_LON_ACC,
)
from datahawk.utils.mp4_utils import find_top_level_box


def parse_gopro(video_path: str | Path) -> tuple[SourceSession, float]:
    """Parse GPS telemetry from a GoPro MP4 file into a SourceSession.

    Returns (session, timo_seconds) where timo is the telemetry-to-video offset.
    Video time = session_time - timo (telemetry starts before video).
    """
    path = Path(video_path)
    gps_samples, timo = _extract_gps5(path)
    if not gps_samples:
        raise ValueError("No GPS data found in GoPro video")

    # Build channels
    lat_ch = SourceChannel(name=GPS_LATITUDE)
    lon_ch = SourceChannel(name=GPS_LONGITUDE)
    speed_ch = SourceChannel(name=GPS_SPEED)
    heading_ch = SourceChannel(name=GPS_HEADING)
    mclk_ch = SourceChannel(name=MASTER_CLK)

    for t, lat, lon, speed in gps_samples:
        lat_ch.append(t, lat)
        lon_ch.append(t, lon)
        speed_ch.append(t, speed)
        mclk_ch.append(t, t)

    # Compute heading from position deltas (gap=5, threshold 2.5 km/h)
    gap = 5
    for i in range(len(gps_samples)):
        if i < gap or speed_ch.values[i] < 2.5:
            heading_ch.append(gps_samples[i][0], float('nan'))
            continue
        lat1, lon1 = lat_ch.values[i - gap], lon_ch.values[i - gap]
        lat2, lon2 = lat_ch.values[i], lon_ch.values[i]
        dlat = (lat2 - lat1) * 111320
        dlon = (lon2 - lon1) * 111320 * math.cos(math.radians(lat1))
        hdg = math.degrees(math.atan2(dlon, dlat)) % 360
        heading_ch.append(gps_samples[i][0], hdg)

    channels = {
        GPS_LATITUDE: lat_ch,
        GPS_LONGITUDE: lon_ch,
        GPS_SPEED: speed_ch,
        GPS_HEADING: heading_ch,
        MASTER_CLK: mclk_ch,
    }

    # Extract hardware accelerometer channels from GPMF ACCL
    _extract_accel_channels(path, channels)

    metadata = SourceSessionMetadata(
        track="",
        date="",
        time="",
        session_type="GoPro",
    )

    return SourceSession(metadata=metadata, channels=channels), timo



def _extract_accel_channels(path: Path, channels: dict[str, SourceChannel]) -> None:
    """Extract accelerometer channels from GoPro GPMF ACCL data.

    Resamples 200Hz ACCL to GPS timestamps by averaging all raw samples within
    each GPS interval. Then applies full 3D rotation: pitch/roll from gravity
    vector, yaw from auto-calibration.
    """
    # Need GPS timestamps to resample to
    gps_ch = channels.get(GPS_LATITUDE)
    if not gps_ch or len(gps_ch.timestamps) < 10:
        return

    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()

        moov_offset = find_top_level_box(f, file_size, b"moov")
        if moov_offset < 0:
            return

        f.seek(moov_offset)
        moov_size = struct.unpack(">I", f.read(4))[0]
        f.read(4)  # skip 'moov'
        moov_data = f.read(moov_size - 8)

        stco_data, stsz_data, sample_count, sample_duration = _find_gpmf_track_from_moov(moov_data)
        if sample_count == 0:
            return

        raw = []
        for i in range(sample_count):
            off = struct.unpack(">I", stco_data[i * 4:i * 4 + 4])[0]
            sz = struct.unpack(">I", stsz_data[i * 4:i * 4 + 4])[0]
            f.seek(off)
            sample = f.read(sz)
            _parse_accl_from_sample(sample, i, raw)

    if not raw:
        return

    # Convert raw sample-index times to seconds
    raw_with_time = [(s[0] * sample_duration, s[1], s[2], s[3]) for s in raw]

    # Resample to GPS timestamps: average all ACCL samples within each GPS interval
    gps_times = gps_ch.timestamps
    filtered = []
    raw_idx = 0
    n_raw = len(raw_with_time)

    for gi in range(len(gps_times)):
        # Interval: midpoint between prev and current GPS fix to midpoint between current and next
        t_lo = (gps_times[gi - 1] + gps_times[gi]) / 2 if gi > 0 else 0.0
        t_hi = (gps_times[gi] + gps_times[gi + 1]) / 2 if gi < len(gps_times) - 1 else gps_times[gi] + 0.1

        # Advance to start of window
        while raw_idx < n_raw and raw_with_time[raw_idx][0] < t_lo:
            raw_idx += 1

        # Collect samples in window
        sum_a, sum_b, sum_c, count = 0.0, 0.0, 0.0, 0
        j = raw_idx
        while j < n_raw and raw_with_time[j][0] < t_hi:
            sum_a += raw_with_time[j][1]
            sum_b += raw_with_time[j][2]
            sum_c += raw_with_time[j][3]
            count += 1
            j += 1

        if count > 0:
            filtered.append((gps_times[gi], sum_a / count, sum_b / count, sum_c / count))

    if not filtered:
        return

    # Determine pitch/roll from gravity vector (static bias over first 3s)
    # ~18Hz * 3s ≈ 54 samples
    cal_n = min(54, len(filtered))
    g_a = sum(s[1] for s in filtered[:cal_n]) / cal_n
    g_b = sum(s[2] for s in filtered[:cal_n]) / cal_n
    g_c = sum(s[3] for s in filtered[:cal_n]) / cal_n
    g_mag = math.sqrt(g_a * g_a + g_b * g_b + g_c * g_c)
    if g_mag < 0.1:
        return

    # Rotation matrix to align gravity vector with world Z (down)
    gx, gy, gz = g_a / g_mag, g_b / g_mag, g_c / g_mag
    cos_angle = gz
    if cos_angle > 0.9999:
        rot = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    elif cos_angle < -0.9999:
        rot = ((-1, 0, 0), (0, -1, 0), (0, 0, -1))
    else:
        kx, ky = gy, -gx
        k_mag = math.sqrt(kx * kx + ky * ky)
        kx, ky = kx / k_mag, ky / k_mag
        sin_angle = math.sqrt(1 - cos_angle * cos_angle)
        c_a = cos_angle
        omc = 1 - c_a
        rot = (
            (c_a + kx * kx * omc,    kx * ky * omc,         ky * sin_angle),
            (kx * ky * omc,          c_a + ky * ky * omc,   -kx * sin_angle),
            (-ky * sin_angle,        kx * sin_angle,         c_a),
        )

    # Apply pitch/roll rotation to remove gravity, get world-horizontal accelerations
    world_samples = []
    for t, a, b, c in filtered:
        a -= g_a
        b -= g_b
        c -= g_c
        wx = rot[0][0] * a + rot[0][1] * b + rot[0][2] * c
        wy = rot[1][0] * a + rot[1][1] * b + rot[1][2] * c
        world_samples.append((t, wx, wy))

    # Auto-calibrate yaw (mounting offset around vertical axis)
    mounting_offset = _calibrate_mounting_offset(world_samples)

    # Rotate by yaw offset to get track-aligned lat/lon
    lat_acc = SourceChannel(name=GPS_LAT_ACC)
    lon_acc = SourceChannel(name=GPS_LON_ACC)

    cos_off = math.cos(mounting_offset)
    sin_off = math.sin(mounting_offset)

    for t_sec, wx, wy in world_samples:
        lon_g = wx * cos_off + wy * sin_off
        lat_g = -wx * sin_off + wy * cos_off
        lat_acc.append(t_sec, lat_g / 9.81)
        lon_acc.append(t_sec, lon_g / 9.81)

    if lat_acc.timestamps:
        channels[GPS_LAT_ACC] = lat_acc
        channels[GPS_LON_ACC] = lon_acc


def _calibrate_mounting_offset(world_samples: list[tuple[float, float, float]]) -> float:
    """Find camera yaw offset by minimizing lateral G during braking events.

    Searches 360 degrees in 1-degree steps on world-horizontal samples.
    """
    braking_samples = [(wx, wy) for _, wx, wy in world_samples
                       if math.sqrt(wx * wx + wy * wy) > 0.3 * 9.81]

    if len(braking_samples) < 20:
        return 0.0

    best_offset = 0.0
    best_cost = float('inf')

    for deg in range(360):
        rad = math.radians(deg)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)
        cost = sum((-wx * sin_r + wy * cos_r) ** 2 for wx, wy in braking_samples)
        if cost < best_cost:
            best_cost = cost
            best_offset = rad

    return best_offset


def _extract_gps5(path: Path) -> tuple[list[tuple[float, float, float, float]], float]:
    """Extract GPS5 data from GPMF track.

    Returns ([(time_seconds, lat, lon, speed_kmh), ...], timo_seconds).
    Timestamps account for dropped fixes within samples by detecting position
    jumps via GPS speed comparison and assigning double time gaps at those points.
    """

    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()

        moov_offset = find_top_level_box(f, file_size, b"moov")
        if moov_offset < 0:
            return [], 0.0

        f.seek(moov_offset)
        moov_size = struct.unpack(">I", f.read(4))[0]
        f.read(4)  # skip 'moov'
        moov_data = f.read(moov_size - 8)

        # Find GPMF track (includes sample_duration from stts)
        stco_data, stsz_data, sample_count, sample_duration = _find_gpmf_track_from_moov(moov_data)
        if sample_count == 0:
            return [], 0.0

        # Collect fixes per sample
        samples_fixes = []  # list of [(lat, lon, speed_kmh), ...]
        timo = 0.0
        for i in range(sample_count):
            off = struct.unpack(">I", stco_data[i * 4:i * 4 + 4])[0]
            sz = struct.unpack(">I", stsz_data[i * 4:i * 4 + 4])[0]
            f.seek(off)
            sample = f.read(sz)
            sample_fixes = []
            _parse_gps5_raw(sample, sample_fixes)
            samples_fixes.append(sample_fixes)
            # Extract TIMO from first sample that has it
            if timo == 0.0:
                timo_idx = sample.find(b"TIMO")
                if timo_idx >= 0 and timo_idx + 12 <= len(sample):
                    timo = struct.unpack(">f", sample[timo_idx + 8:timo_idx + 12])[0]

    if not samples_fixes:
        return [], timo

    # Compute nominal fix rate from total fixes / total duration
    total_fixes = sum(len(s) for s in samples_fixes)
    total_duration = sample_count * sample_duration
    nominal_interval = total_duration / total_fixes

    # Assign timestamps per sample, detecting dropped-fix gaps using GPS speed
    results = []
    prev_fix = None  # last fix from previous sample for cross-boundary detection

    for sample_idx, sample_fixes in enumerate(samples_fixes):
        sample_start = sample_idx * sample_duration

        if not sample_fixes:
            continue

        # Detect gaps within this sample: compare position delta to GPS speed
        # A gap exists where derived_speed / gps_speed > 1.5
        n = len(sample_fixes)
        has_gap = [False] * n  # True at index j means gap BEFORE fix j

        for j in range(1, n):
            lat, lon, speed = sample_fixes[j]
            lat0, lon0, _ = sample_fixes[j - 1]
            if speed < 10:
                continue
            dlat = (lat - lat0) * 111320
            dlon = (lon - lon0) * 111320 * math.cos(math.radians(lat))
            dist = math.sqrt(dlat * dlat + dlon * dlon)
            # Expected distance at this speed for one fix interval
            expected_dist = speed / 3.6 * nominal_interval
            if expected_dist > 0 and dist / expected_dist > 1.5:
                has_gap[j] = True

        # Also check cross-boundary (last fix of prev sample -> first fix of this sample)
        if prev_fix is not None and n > 0:
            lat, lon, speed = sample_fixes[0]
            lat0, lon0, _ = prev_fix
            if speed >= 10:
                dlat = (lat - lat0) * 111320
                dlon = (lon - lon0) * 111320 * math.cos(math.radians(lat))
                dist = math.sqrt(dlat * dlat + dlon * dlon)
                expected_dist = speed / 3.6 * nominal_interval
                if expected_dist > 0 and dist / expected_dist > 1.5:
                    has_gap[0] = True

        # Assign timestamps: normal interval for regular fixes, double for gaps
        # Total slots: n + num_gaps (each gap counts as 2 intervals instead of 1)
        num_gaps = sum(has_gap)
        slot_total = n + num_gaps
        dt = sample_duration / slot_total

        t = sample_start
        for j in range(n):
            if j > 0:
                t += (2 * dt) if has_gap[j] else dt

            lat, lon, speed = sample_fixes[j]
            results.append((t, lat, lon, speed))

        prev_fix = sample_fixes[-1]

    # Normalize: ensure last timestamp ~ total_duration
    # The per-sample approach should naturally sum to total_duration
    return results, timo


def _find_gpmf_track_from_moov(moov_data: bytes) -> tuple[bytes, bytes, int, float]:
    """Find GPMF track's stco and stsz offsets from moov data.

    Returns (stco_offsets, stsz_sizes, sample_count, sample_duration_seconds).
    """
    pos = 0
    while pos + 8 <= len(moov_data):
        size = struct.unpack(">I", moov_data[pos:pos + 4])[0]
        btype = moov_data[pos + 4:pos + 8]
        if size < 8:
            break
        if btype == b"trak":
            trak = moov_data[pos + 8:pos + size]
            # Check for meta handler (GPMF track)
            hdlr_idx = trak.find(b"hdlr")
            if hdlr_idx >= 0 and trak[hdlr_idx + 12:hdlr_idx + 16] == b"meta":
                # Get timescale from mdhd
                timescale = 1000
                mdhd_idx = trak.find(b"mdhd")
                if mdhd_idx >= 0:
                    version = trak[mdhd_idx + 4]
                    if version == 0:
                        timescale = struct.unpack(">I", trak[mdhd_idx + 16:mdhd_idx + 20])[0]
                    else:
                        timescale = struct.unpack(">I", trak[mdhd_idx + 24:mdhd_idx + 28])[0]

                # Get sample duration from stts
                sample_duration = 1.001  # default fallback
                stts_idx = trak.find(b"stts")
                if stts_idx >= 0:
                    stts_dur = struct.unpack(">I", trak[stts_idx + 16:stts_idx + 20])[0]
                    sample_duration = stts_dur / timescale

                stco_idx = trak.find(b"stco")
                stsz_idx = trak.find(b"stsz")
                if stco_idx >= 0 and stsz_idx >= 0:
                    # stco: version(4) + count(4) + offsets...
                    count = struct.unpack(">I", trak[stco_idx + 8:stco_idx + 12])[0]
                    stco_data = trak[stco_idx + 12:stco_idx + 12 + count * 4]
                    # stsz: version(4) + sample_size(4) + count(4) + sizes...
                    stsz_data = trak[stsz_idx + 16:stsz_idx + 16 + count * 4]
                    if count > 10:
                        return stco_data, stsz_data, count, sample_duration
        pos += size
    return b"", b"", 0, 1.0


def _parse_gps5_raw(sample: bytes, out: list[tuple[float, float, float]]) -> None:
    """Parse GPS5 fixes from a GPMF sample, appending (lat, lon, speed_kmh) tuples.

    Timestamps are NOT assigned here -- caller assigns global uniform timestamps.
    """
    gps5_idx = sample.find(b"GPS5")
    if gps5_idx < 0:
        return

    # Find SCAL for this STRM
    strm_start = sample.rfind(b"STRM", 0, gps5_idx)
    scal_idx = sample.find(b"SCAL", strm_start if strm_start >= 0 else 0, gps5_idx + 200)
    scales = [10000000, 10000000, 1000, 1000, 100]  # default GPS5 scales
    if scal_idx >= 0:
        scal_type = sample[scal_idx + 4]
        scal_struct_size = sample[scal_idx + 5]
        scal_repeat = struct.unpack(">H", sample[scal_idx + 6:scal_idx + 8])[0]
        scal_payload = sample[scal_idx + 8:scal_idx + 8 + scal_struct_size * scal_repeat]
        if scal_type == ord('l') and scal_struct_size == 4 and scal_repeat >= 5:
            scales = [struct.unpack(">i", scal_payload[j*4:(j+1)*4])[0] for j in range(5)]
        elif scal_type == ord('s') and scal_struct_size == 2 and scal_repeat >= 5:
            scales = [struct.unpack(">h", scal_payload[j*2:(j+1)*2])[0] for j in range(5)]

    # Parse GPS5 payload
    struct_size = sample[gps5_idx + 5]
    repeat = struct.unpack(">H", sample[gps5_idx + 6:gps5_idx + 8])[0]
    if struct_size != 20 or repeat == 0:
        return

    payload = sample[gps5_idx + 8:gps5_idx + 8 + 20 * repeat]
    for j in range(repeat):
        offset = j * 20
        if offset + 20 > len(payload):
            break
        lat_raw, lon_raw, alt_raw, speed2d_raw, speed3d_raw = struct.unpack(
            ">5i", payload[offset:offset + 20]
        )
        lat = lat_raw / scales[0]
        lon = lon_raw / scales[1]
        speed_ms = speed2d_raw / scales[3]  # m/s
        speed_kmh = speed_ms * 3.6
        out.append((lat, lon, speed_kmh))



def extract_gopro_accel_magnitude(path: Path) -> tuple[list[tuple[float, float]], float]:
    """Extract horizontal acceleration magnitude from GoPro GPMF at ~25Hz.

    Returns (time_value_pairs, timo) where timo is the telemetry-to-video offset.
    """
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()

        moov_offset = find_top_level_box(f, file_size, b"moov")
        if moov_offset < 0:
            return [], 0.0

        f.seek(moov_offset)
        moov_size = struct.unpack(">I", f.read(4))[0]
        f.read(4)  # skip 'moov'
        moov_data = f.read(moov_size - 8)

        stco_data, stsz_data, sample_count, _ = _find_gpmf_track_from_moov(moov_data)
        if sample_count == 0:
            return [], 0.0

        raw = []
        timo = 0.0
        for i in range(sample_count):
            off = struct.unpack(">I", stco_data[i * 4:i * 4 + 4])[0]
            sz = struct.unpack(">I", stsz_data[i * 4:i * 4 + 4])[0]
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
        out.append((t, a / scale, b / scale, c / scale))

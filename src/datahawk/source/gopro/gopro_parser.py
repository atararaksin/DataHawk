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

from datahawk.source.types import SourceChannel as XrzChannel, SourceSession as XrzSession, SourceSessionMetadata as XrzSessionMetadata

_GPS_LAT_ID = -1
_GPS_LON_ID = -2
_GPS_SPEED_ID = -3
_GPS_HEADING_ID = -10
_MASTER_CLK_ID = 0


def parse_gopro(video_path: str | Path) -> tuple[XrzSession, float]:
    """Parse GPS telemetry from a GoPro MP4 file into a SourceSession.

    Returns (session, timo_seconds) where timo is the telemetry-to-video offset.
    Video time = session_time - timo (telemetry starts before video).
    """
    path = Path(video_path)
    gps_samples, timo = _extract_gps5(path)
    if not gps_samples:
        raise ValueError("No GPS data found in GoPro video")

    # Build channels
    lat_ch = XrzChannel(id=_GPS_LAT_ID, short_name="GPSLat", long_name="GPS Latitude")
    lon_ch = XrzChannel(id=_GPS_LON_ID, short_name="GPSLon", long_name="GPS Longitude")
    speed_ch = XrzChannel(id=_GPS_SPEED_ID, short_name="GPSSpd", long_name="GPS Speed")
    heading_ch = XrzChannel(id=_GPS_HEADING_ID, short_name="GPSHdg", long_name="GPS Heading")
    mclk_ch = XrzChannel(id=_MASTER_CLK_ID, short_name="MClk", long_name="Master Clk")

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
        _GPS_LAT_ID: lat_ch,
        _GPS_LON_ID: lon_ch,
        _GPS_SPEED_ID: speed_ch,
        _GPS_HEADING_ID: heading_ch,
        _MASTER_CLK_ID: mclk_ch,
    }

    metadata = XrzSessionMetadata(
        track="",
        date="",
        time="",
        session_type="GoPro",
    )

    return XrzSession(metadata=metadata, channels=channels), timo


def _extract_gps5(path: Path) -> tuple[list[tuple[float, float, float, float]], float]:
    """Extract GPS5 data from GPMF track.

    Returns ([(time_seconds, lat, lon, speed_kmh), ...], timo_seconds).
    Timestamps account for dropped fixes within samples by detecting position
    jumps via GPS speed comparison and assigning double time gaps at those points.
    """
    from datahawk.video_sync import _find_top_level_box

    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()

        moov_offset = _find_top_level_box(f, file_size, b"moov")
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

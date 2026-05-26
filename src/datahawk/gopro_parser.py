"""Parse GoPro MP4 GPMF telemetry into an XrzSession-compatible structure.

Extracts GPS5 (lat, lon, alt, speed2D, speed3D) and computes heading from
position deltas. Produces an XrzSession with channels:
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

from datahawk.xrz_parser import XrzSession, XrzSessionMetadata, XrzChannel

_GPS_LAT_ID = -1
_GPS_LON_ID = -2
_GPS_SPEED_ID = -3
_GPS_HEADING_ID = -10
_MASTER_CLK_ID = 0


def parse_gopro(video_path: str | Path) -> XrzSession:
    """Parse GPS telemetry from a GoPro MP4 file into an XrzSession."""
    path = Path(video_path)
    gps_samples = _extract_gps5(path)
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

    return XrzSession(metadata=metadata, channels=channels)


def _extract_gps5(path: Path) -> list[tuple[float, float, float, float]]:
    """Extract GPS5 data from GPMF track.

    Returns list of (time_seconds, lat_degrees, lon_degrees, speed_km_h).
    """
    from datahawk.video_sync import _find_top_level_box

    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()

        moov_offset = _find_top_level_box(f, file_size, b"moov")
        if moov_offset < 0:
            return []

        f.seek(moov_offset)
        moov_size = struct.unpack(">I", f.read(4))[0]
        f.read(4)  # skip 'moov'
        moov_data = f.read(moov_size - 8)

        # Find GPMF track
        stco_data, stsz_data, sample_count = _find_gpmf_track_from_moov(moov_data)
        if sample_count == 0:
            return []

        results = []
        for i in range(sample_count):
            off = struct.unpack(">I", stco_data[i * 4:i * 4 + 4])[0]
            sz = struct.unpack(">I", stsz_data[i * 4:i * 4 + 4])[0]
            f.seek(off)
            sample = f.read(sz)
            _parse_gps5_from_sample(sample, i, results)

    return results


def _find_gpmf_track_from_moov(moov_data: bytes) -> tuple[bytes, bytes, int]:
    """Find GPMF track's stco and stsz offsets from moov data."""
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
                stco_idx = trak.find(b"stco")
                stsz_idx = trak.find(b"stsz")
                if stco_idx >= 0 and stsz_idx >= 0:
                    # stco: version(4) + count(4) + offsets...
                    count = struct.unpack(">I", trak[stco_idx + 8:stco_idx + 12])[0]
                    stco_data = trak[stco_idx + 12:stco_idx + 12 + count * 4]
                    # stsz: version(4) + sample_size(4) + count(4) + sizes...
                    stsz_data = trak[stsz_idx + 16:stsz_idx + 16 + count * 4]
                    if count > 10:
                        return stco_data, stsz_data, count
        pos += size
    return b"", b"", 0


def _parse_gps5_from_sample(sample: bytes, sample_idx: int,
                            out: list[tuple[float, float, float, float]]) -> None:
    """Parse GPS5 data from a GPMF sample.

    GPS5 contains: lat, lon, alt, speed2D, speed3D (all int32, scaled by SCAL).
    """
    gps5_idx = sample.find(b"GPS5")
    if gps5_idx < 0:
        return

    # Find SCAL for this STRM
    strm_start = sample.rfind(b"STRM", 0, gps5_idx)
    scal_idx = sample.find(b"SCAL", strm_start if strm_start >= 0 else 0, gps5_idx + 200)
    scales = [10000000, 10000000, 1000, 1000, 100]  # default GPS5 scales
    if scal_idx >= 0:
        scal_type = sample[scal_idx + 4]  # type char
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

        # Time: sample_idx seconds + fractional within sample
        t = sample_idx + j / repeat
        out.append((t, lat, lon, speed_kmh))

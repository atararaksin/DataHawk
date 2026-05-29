"""
Insta360 telemetry parser - extracts accelerometer and gyroscope data from MP4 files.

Ported from AdrianEddy/telemetry-parser (Rust, MIT/Apache-2.0 licensed).
The Insta360 format appends an "extra" data section at the end of the MP4 file,
identified by a 32-byte magic string. Records are read backwards from the end.

For DataHawk, we only need the accelerometer data for video sync via cross-correlation.
"""

import struct
from dataclasses import dataclass

MAGIC = b"8db42d694ccc418790edff439fe026bf"
HEADER_SIZE = 32 + 4 + 4 + 32  # padding(32) + size(4) + version(4) + magic(32)


# Record type IDs
class RecordType:
    OFFSETS = 0
    METADATA = 1
    GYRO = 3
    EXPOSURE = 4
    GPS = 7


@dataclass
class IMUSample:
    timestamp: float  # seconds
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


@dataclass
class Insta360Telemetry:
    accelerometer: list  # list of (timestamp_s, ax, ay, az)
    gyroscope: list  # list of (timestamp_s, gx, gy, gz)
    sample_rate_hz: float
    is_raw_gyro: bool
    acc_range: float  # ±g
    gyro_range: float  # dps
    first_frame_timestamp_us: float  # microseconds (same unit as raw IMU timestamps)
    camera_type: str


def detect(filepath: str) -> bool:
    """Check if file has Insta360 telemetry by looking for magic at end."""
    with open(filepath, 'rb') as f:
        f.seek(-len(MAGIC), 2)
        return f.read(len(MAGIC)) == MAGIC


def parse(filepath: str) -> Insta360Telemetry:
    """Parse Insta360 telemetry from an MP4 file. Returns accelerometer + gyroscope data."""
    with open(filepath, 'rb') as f:
        f.seek(0, 2)
        file_size = f.tell()

        # Read and verify header at end of file
        f.seek(-HEADER_SIZE, 2)
        header = f.read(HEADER_SIZE)
        if header[HEADER_SIZE - 32:] != MAGIC:
            raise ValueError("Not an Insta360 file (magic not found)")

        extra_size = struct.unpack_from('<I', header, 32)[0]
        version = struct.unpack_from('<I', header, 36)[0]
        extra_start = file_size - extra_size

        # State
        is_raw_gyro = False
        acc_range = 16.0
        gyro_range = 2000.0
        first_frame_timestamp = 0.0
        camera_type = ""
        accel_data = []
        gyro_data = []

        # Try to read offsets table first
        offsets = {}
        offset = HEADER_SIZE + 4 + 1 + 1
        f.seek(-offset + 1, 2)
        first_id = struct.unpack('B', f.read(1))[0]

        if first_id == RecordType.OFFSETS:
            size = struct.unpack('<I', f.read(4))[0]
            f.seek(-(offset + size), 2)
            buf = f.read(size)
            offsets = _parse_offsets(buf)

        if offsets:
            # Read metadata first to get is_raw_gyro
            if RecordType.METADATA in offsets:
                rec_offset, rec_size = offsets[RecordType.METADATA]
                f.seek(extra_start + rec_offset)
                buf = f.read(rec_size)
                _fmt = struct.unpack('B', f.read(1))[0]
                _id2 = struct.unpack('B', f.read(1))[0]
                _size2 = struct.unpack('<I', f.read(4))[0]
                # Actually the format byte and id come AFTER the data in offset mode
                # Let me re-read: offset points to data, then format+id+size follow
                f.seek(extra_start + rec_offset)
                buf = f.read(rec_size)
                meta = _parse_metadata_protobuf(buf)
                is_raw_gyro = meta.get('is_raw_gyro', False)
                acc_range = meta.get('acc_range', 16.0)
                gyro_range = meta.get('gyro_range', 2000.0)
                first_frame_timestamp = meta.get('first_frame_timestamp', 0.0)
                camera_type = meta.get('camera_type', '')

            # Read gyro record
            if RecordType.GYRO in offsets:
                rec_offset, rec_size = offsets[RecordType.GYRO]
                f.seek(extra_start + rec_offset)
                buf = f.read(rec_size)
                accel_data, gyro_data = _parse_gyro_record(buf, is_raw_gyro, acc_range, gyro_range)
        else:
            # Sequential mode: read records backwards from end
            offset = HEADER_SIZE + 4 + 1 + 1
            while offset < extra_size:
                f.seek(-offset, 2)
                fmt = struct.unpack('B', f.read(1))[0]
                rec_id = struct.unpack('B', f.read(1))[0]
                rec_size = struct.unpack('<I', f.read(4))[0]
                f.seek(-(offset + rec_size), 2)
                buf = f.read(rec_size)

                if rec_id == RecordType.METADATA:
                    meta = _parse_metadata_protobuf(buf)
                    is_raw_gyro = meta.get('is_raw_gyro', False)
                    acc_range = meta.get('acc_range', 16.0)
                    gyro_range = meta.get('gyro_range', 2000.0)
                    first_frame_timestamp = meta.get('first_frame_timestamp', 0.0)
                    camera_type = meta.get('camera_type', '')
                elif rec_id == RecordType.GYRO:
                    accel_data, gyro_data = _parse_gyro_record(buf, is_raw_gyro, acc_range, gyro_range)

                offset += rec_size + 4 + 1 + 1

        # Compute sample rate
        sample_rate = 0.0
        if len(accel_data) >= 2:
            total_time = accel_data[-1][0] - accel_data[0][0]
            if total_time > 0:
                sample_rate = (len(accel_data) - 1) / total_time

        return Insta360Telemetry(
            accelerometer=accel_data,
            gyroscope=gyro_data,
            sample_rate_hz=sample_rate,
            is_raw_gyro=is_raw_gyro,
            acc_range=acc_range,
            gyro_range=gyro_range,
            first_frame_timestamp_us=first_frame_timestamp,
            camera_type=camera_type,
        )


def _parse_offsets(data: bytes) -> dict:
    """Parse offsets table: id(1) + format(1) + size(4) + offset(4) per entry."""
    offsets = {}
    pos = 0
    while pos + 10 <= len(data):
        rec_id = data[pos]
        # _format = data[pos + 1]
        size = struct.unpack_from('<I', data, pos + 2)[0]
        offset = struct.unpack_from('<I', data, pos + 6)[0]
        if rec_id > 0:
            offsets[rec_id] = (offset, size)
        pos += 10
    return offsets


def _parse_gyro_record(data: bytes, is_raw_gyro: bool, acc_range: float, gyro_range: float):
    """Parse gyro record: timestamp(u64) + accel_xyz + gyro_xyz per sample.
    Raw mode: xyz as u16 (offset by 32768), scaled by range.
    Float mode: xyz as f64 in rad/s (gyro) and g (accel).
    """
    item_size = 8 + 6 * 2 if is_raw_gyro else 8 + 6 * 8
    n_samples = len(data) // item_size
    accel = []
    gyro = []

    pos = 0
    for _ in range(n_samples):
        timestamp_us = struct.unpack_from('<Q', data, pos)[0]
        timestamp_s = timestamp_us / 1_000_000.0  # microseconds to seconds
        pos += 8

        if is_raw_gyro:
            ax = struct.unpack_from('<H', data, pos)[0] - 32768.0
            ay = struct.unpack_from('<H', data, pos + 2)[0] - 32768.0
            az = struct.unpack_from('<H', data, pos + 4)[0] - 32768.0
            gx = struct.unpack_from('<H', data, pos + 6)[0] - 32768.0
            gy = struct.unpack_from('<H', data, pos + 8)[0] - 32768.0
            gz = struct.unpack_from('<H', data, pos + 10)[0] - 32768.0
            pos += 12

            # Scale raw values to physical units
            accl_scale = 32768.0 / acc_range  # LSB per g
            gyro_scale = 32768.0 / gyro_range  # LSB per deg/s
            ax /= accl_scale
            ay /= accl_scale
            az /= accl_scale
            gx /= gyro_scale
            gy /= gyro_scale
            gz /= gyro_scale
        else:
            ax, ay, az = struct.unpack_from('<3d', data, pos)
            pos += 24
            gx, gy, gz = struct.unpack_from('<3d', data, pos)
            pos += 24

        accel.append((timestamp_s, ax, ay, az))
        gyro.append((timestamp_s, gx, gy, gz))

    return accel, gyro


def _parse_metadata_protobuf(data: bytes) -> dict:
    """Minimal protobuf decoder for ExtraMetadata - extracts only fields we need.
    
    Protobuf wire format:
    - field_number = tag >> 3, wire_type = tag & 0x7
    - wire_type 0 = varint, 1 = 64-bit, 2 = length-delimited, 5 = 32-bit
    """
    result = {
        'is_raw_gyro': False,
        'acc_range': 16.0,
        'gyro_range': 2000.0,
        'first_frame_timestamp': 0.0,
        'camera_type': '',
    }

    pos = 0
    while pos < len(data):
        # Read varint tag
        tag, pos = _read_varint(data, pos)
        if tag is None:
            break
        field_number = tag >> 3
        wire_type = tag & 0x7

        if wire_type == 0:  # varint
            value, pos = _read_varint(data, pos)
            if value is None:
                break
            if field_number == 62:  # is_raw_gyro (bool)
                result['is_raw_gyro'] = bool(value)
            elif field_number == 24:  # first_frame_timestamp (int64)
                result['first_frame_timestamp'] = float(value)
        elif wire_type == 1:  # 64-bit fixed
            if pos + 8 > len(data):
                break
            if field_number == 25:  # rolling_shutter_time (double)
                pass  # not needed
            elif field_number == 28:  # gyro_timestamp (double)
                pass  # not needed for sync
            pos += 8
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(data, pos)
            if length is None or pos + length > len(data):
                break
            if field_number == 2:  # camera_type (string)
                result['camera_type'] = data[pos:pos + length].decode('utf-8', errors='replace')
            elif field_number == 65:  # gyro_cfg_info (message)
                cfg = _parse_gyro_config(data[pos:pos + length])
                result['acc_range'] = cfg.get('acc_range', 16.0)
                result['gyro_range'] = cfg.get('gyro_range', 2000.0)
            pos += length
        elif wire_type == 5:  # 32-bit fixed
            pos += 4
        else:
            break  # unknown wire type

    return result


def _parse_gyro_config(data: bytes) -> dict:
    """Parse GyroConfigInfo message: acc_range(tag 1, uint32) + gyro_range(tag 2, uint32)."""
    result = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        if tag is None:
            break
        field_number = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            if value is None:
                break
            if field_number == 1:
                result['acc_range'] = float(value)
            elif field_number == 2:
                result['gyro_range'] = float(value)
        else:
            break
    return result


def _read_varint(data: bytes, pos: int):
    """Read a protobuf varint. Returns (value, new_pos) or (None, pos) on error."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
        if shift >= 64:
            return None, pos
    return None, pos

"""GPS drift correction using track boundary polygon.

Mode 1: Per-lap constant offset (for master lap correction)
Mode 2: Session-level variable correction with smooth transitions

Requires: track boundary mask from track_boundary_detect.py
"""

from __future__ import annotations

import math
import numpy as np


def _lat_lon_to_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Return (dx_meters, dy_meters) from point 1 to point 2. X=east, Y=north."""
    avg_lat = math.radians((lat1 + lat2) / 2)
    dy = (lat2 - lat1) * 111320
    dx = (lon2 - lon1) * 111320 * math.cos(avg_lat)
    return dx, dy


def _meters_to_lat_lon(lat_ref: float, dx_m: float, dy_m: float) -> tuple[float, float]:
    """Convert meter offset to lat/lon delta."""
    dlat = dy_m / 111320
    dlon = dx_m / (111320 * math.cos(math.radians(lat_ref)))
    return dlat, dlon


def _point_in_mask(lat: float, lon: float, mask: np.ndarray, origin_px: float, origin_py: float, zoom: int) -> bool:
    """Check if a lat/lon point is inside the track mask."""
    n = 2 ** zoom
    px = int((lon + 180.0) / 360.0 * n * 256 - origin_px)
    lat_rad = math.radians(lat)
    py = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n * 256 - origin_py)
    h, w = mask.shape
    if 0 <= px < w and 0 <= py < h:
        return mask[py, px] > 0
    return False


def _fraction_outside(lats: list[float], lons: list[float], mask: np.ndarray,
                      origin_px: float, origin_py: float, zoom: int,
                      dx_m: float = 0, dy_m: float = 0) -> float:
    """Fraction of points outside the mask after applying offset."""
    outside = 0
    for lat, lon in zip(lats, lons):
        if math.isnan(lat) or math.isnan(lon):
            continue
        dlat, dlon = _meters_to_lat_lon(lat, dx_m, dy_m)
        if not _point_in_mask(lat + dlat, lon + dlon, mask, origin_px, origin_py, zoom):
            outside += 1
    total = sum(1 for la, lo in zip(lats, lons) if not (math.isnan(la) or math.isnan(lo)))
    return outside / total if total > 0 else 0


def correct_lap(
    lats: list[float],
    lons: list[float],
    mask: np.ndarray,
    origin_px: float,
    origin_py: float,
    zoom: int,
    max_offset_m: float = 8.0,
    tolerance: float = 0.05,
) -> tuple[list[float], list[float], tuple[float, float]]:
    """Mode 1: Find smallest constant offset to place lap inside polygon.

    Returns (corrected_lats, corrected_lons, (dx_m, dy_m)).
    """
    # Check if already inside
    if _fraction_outside(lats, lons, mask, origin_px, origin_py, zoom) <= tolerance:
        return lats, lons, (0.0, 0.0)

    # Grid search: coarse (1m steps) then refine (0.25m steps)
    best_offset = (0.0, 0.0)
    best_mag = float('inf')

    for step, radius in [(1.0, max_offset_m), (0.25, 2.0)]:
        search_cx, search_cy = best_offset
        offsets = []
        r = radius
        dy = -r
        while dy <= r:
            dx = -r
            while dx <= r:
                ox, oy = search_cx + dx, search_cy + dy
                mag = math.sqrt(ox ** 2 + oy ** 2)
                if mag <= max_offset_m:
                    offsets.append((ox, oy, mag))
                dx += step
            dy += step

        # Sort by magnitude (prefer smallest offset)
        offsets.sort(key=lambda x: x[2])

        for dx, dy_off, mag in offsets:
            if mag >= best_mag:
                continue
            frac = _fraction_outside(lats, lons, mask, origin_px, origin_py, zoom, dx, dy_off)
            if frac <= tolerance:
                best_offset = (dx, dy_off)
                best_mag = mag
                break

    dx_m, dy_m = best_offset
    if best_mag == float('inf'):
        # No valid offset found within constraints
        return lats, lons, (0.0, 0.0)

    # Apply offset
    corrected_lats = []
    corrected_lons = []
    for lat, lon in zip(lats, lons):
        if math.isnan(lat) or math.isnan(lon):
            corrected_lats.append(lat)
            corrected_lons.append(lon)
        else:
            dlat, dlon = _meters_to_lat_lon(lat, dx_m, dy_m)
            corrected_lats.append(lat + dlat)
            corrected_lons.append(lon + dlon)

    return corrected_lats, corrected_lons, (dx_m, dy_m)


def correct_session(
    lats: list[float],
    lons: list[float],
    timestamps: list[float],
    speeds: list[float] | None,
    master_lats: list[float],
    master_lons: list[float],
    mask: np.ndarray,
    origin_px: float,
    origin_py: float,
    zoom: int,
    buffer_m: float = 0.25,
    min_duration_s: float = 2.0,
    tolerance: float = 0.05,
    max_offset_m: float = 8.0,
    merge_gap_s: float = 2.0,
    min_speed_kmh: float = 25.0,
) -> tuple[list[float], list[float]]:
    """Mode 2: Session-level variable correction with smooth transitions.

    Args:
        lats, lons: raw GPS from SourceSession
        timestamps: corresponding timestamps (seconds)
        speeds: GPS speed in km/h (same length as lats), or None to skip speed filter
        master_lats, master_lons: master lap coordinates (for transition decay)
        mask: track boundary mask from detect_track_boundary
        origin_px, origin_py, zoom: mask coordinate system
        buffer_m: polygon buffer tolerance
        min_duration_s: minimum sequence duration to flag
        tolerance: fraction of points allowed outside
        max_offset_m: maximum correction magnitude
        merge_gap_s: merge sequences closer than this
        min_speed_kmh: ignore points below this speed (paddock/spinout)

    Returns (corrected_lats, corrected_lons).
    """
    n_pts = len(lats)
    # Buffer: dilate mask by buffer_m pixels
    from scipy.ndimage import binary_dilation
    mpp = 156543.03 * math.cos(math.radians(np.nanmean(lats))) / (2 ** zoom)
    buffer_px = max(1, int(buffer_m / mpp))
    buffered_mask = binary_dilation(mask > 0, iterations=buffer_px).astype(np.uint8) * 255

    # Step 1: classify each point as inside/outside buffered polygon
    outside = np.zeros(n_pts, dtype=bool)
    for i in range(n_pts):
        if math.isnan(lats[i]) or math.isnan(lons[i]):
            continue
        # Skip low-speed points (paddock, spinout, out-lap crawl)
        if speeds is not None and (math.isnan(speeds[i]) or speeds[i] < min_speed_kmh):
            continue
        if not _point_in_mask(lats[i], lons[i], buffered_mask, origin_px, origin_py, zoom):
            outside[i] = True

    # Step 2: find consecutive sequences of outside points
    sequences = []  # list of (start_idx, end_idx)
    i = 0
    while i < n_pts:
        if outside[i]:
            start = i
            while i < n_pts and (outside[i] or
                  # allow up to tolerance% inside points within a run
                  (i < n_pts - 1 and outside[i + 1])):
                i += 1
            end = i  # exclusive
            # Check duration and outside fraction
            if end > start:
                duration = timestamps[min(end - 1, n_pts - 1)] - timestamps[start]
                frac_out = outside[start:end].sum() / (end - start)
                if duration >= min_duration_s and frac_out > tolerance:
                    sequences.append((start, end))
        i += 1

    # Step 3: merge sequences closer than merge_gap_s
    merged = []
    for seq in sequences:
        if merged and timestamps[seq[0]] - timestamps[merged[-1][1] - 1] < merge_gap_s:
            merged[-1] = (merged[-1][0], seq[1])
        else:
            merged.append(seq)
    sequences = merged

    if not sequences:
        return list(lats), list(lons)

    # Step 4: find offset for each sequence
    seq_offsets = []
    for start, end in sequences:
        seg_lats = [lats[i] for i in range(start, end)]
        seg_lons = [lons[i] for i in range(start, end)]
        _, _, offset = correct_lap(seg_lats, seg_lons, buffered_mask, origin_px, origin_py, zoom,
                                   max_offset_m, tolerance)
        seq_offsets.append(offset)

    # Step 5: build per-point offset array with smooth onset/offset
    # The sequence boundaries are at polygon edges. To avoid abrupt jumps,
    # ramp the offset up/down over the first/last 1 second of each sequence.
    offset_x = np.zeros(n_pts)
    offset_y = np.zeros(n_pts)

    ramp_s = 1.0  # ramp over 1 second at each end of the sequence

    for seq_idx, ((start, end), (off_dx, off_dy)) in enumerate(zip(sequences, seq_offsets)):
        off_mag = math.sqrt(off_dx ** 2 + off_dy ** 2)
        if off_mag < 0.01:
            continue

        seq_start_t = timestamps[start]
        seq_end_t = timestamps[min(end - 1, n_pts - 1)]
        seq_duration = seq_end_t - seq_start_t

        for i in range(start, end):
            # Ramp up at start of sequence
            dt_from_start = timestamps[i] - seq_start_t
            # Ramp down at end of sequence
            dt_from_end = seq_end_t - timestamps[i]

            ramp_in = min(1.0, dt_from_start / ramp_s) if seq_duration > 2 * ramp_s else 1.0
            ramp_out = min(1.0, dt_from_end / ramp_s) if seq_duration > 2 * ramp_s else 1.0
            scale = min(ramp_in, ramp_out)

            offset_x[i] = off_dx * scale
            offset_y[i] = off_dy * scale

    # Apply offsets
    corrected_lats = []
    corrected_lons = []
    for i in range(n_pts):
        if math.isnan(lats[i]) or math.isnan(lons[i]) or (offset_x[i] == 0 and offset_y[i] == 0):
            corrected_lats.append(lats[i])
            corrected_lons.append(lons[i])
        else:
            dlat, dlon = _meters_to_lat_lon(lats[i], offset_x[i], offset_y[i])
            corrected_lats.append(lats[i] + dlat)
            corrected_lons.append(lons[i] + dlon)

    return corrected_lats, corrected_lons

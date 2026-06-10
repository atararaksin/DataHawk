"""Detect track boundary polygon from satellite imagery using GPS-guided CV.

Uses GPS trace as a seed to identify track surface, then expands to find
actual track edges within a distance limit.

Usage:
    from tools.track_boundary_detect import detect_track_boundary
    result = detect_track_boundary(master_lap_lats, master_lap_lons)

Dependencies: numpy, Pillow, scipy.
"""

from __future__ import annotations

import math
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import binary_erosion, binary_dilation, binary_closing, label

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TILE_SIZE = 256


def lat_lon_to_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    n = 2 ** zoom
    px = (lon + 180.0) / 360.0 * n * TILE_SIZE
    lat_rad = math.radians(lat)
    py = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return px, py


def pixel_to_lat_lon(px: float, py: float, zoom: int) -> tuple[float, float]:
    n = 2 ** zoom
    lon = px / (n * TILE_SIZE) * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * py / (n * TILE_SIZE))))
    return math.degrees(lat_rad), lon


def _fetch_tile(z: int, x: int, y: int) -> Image.Image | None:
    url = TILE_URL.format(z=z, y=y, x=x)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DataHawk/0.1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return Image.open(__import__("io").BytesIO(resp.read()))
    except Exception:
        return None


def detect_track_boundary(
    gps_lats: list[float],
    gps_lons: list[float],
    zoom: int = 18,
    max_distance_m: float = 11.0,
    color_sigma: float = 1.5,
    padding_m: float = 50,
) -> dict:
    """Detect track boundary using GPS trace as seed.

    Args:
        gps_lats: GPS latitude points (e.g. master lap)
        gps_lons: GPS longitude points
        zoom: tile zoom level (18 = ~0.4m/px)
        max_distance_m: maximum distance from GPS trace to include (default 11m)
        color_sigma: number of std deviations for color threshold (default 1.5)
        padding_m: extra margin for tile fetching

    Returns dict with:
        - outer: list of (lat, lon) for boundary, or None
        - mask: binary numpy array of detected track
        - overlay_path: path to debug overlay image
    """
    # Filter NaN
    valid = [(lat, lon) for lat, lon in zip(gps_lats, gps_lons)
             if not (math.isnan(lat) or math.isnan(lon))]
    if not valid:
        return {"outer": None, "error": "No valid GPS points"}

    lats = [p[0] for p in valid]
    lons = [p[1] for p in valid]
    center_lat = (min(lats) + max(lats)) / 2
    center_lon = (min(lons) + max(lons)) / 2
    mpp = 156543.03 * math.cos(math.radians(center_lat)) / (2 ** zoom)

    # Compute tile area needed
    n_tiles = 2 ** zoom
    cx, cy = lat_lon_to_pixel(center_lat, center_lon, zoom)
    max_dist_px = 0
    for lat, lon in valid:
        px, py = lat_lon_to_pixel(lat, lon, zoom)
        max_dist_px = max(max_dist_px, max(abs(px - cx), abs(py - cy)))
    radius_px = max_dist_px + padding_m / mpp

    # Fetch tiles
    tx_min = int((cx - radius_px) // TILE_SIZE)
    tx_max = int((cx + radius_px) // TILE_SIZE)
    ty_min = int((cy - radius_px) // TILE_SIZE)
    ty_max = int((cy + radius_px) // TILE_SIZE)
    w = (tx_max - tx_min + 1) * TILE_SIZE
    h = (ty_max - ty_min + 1) * TILE_SIZE
    composite = Image.new("RGB", (w, h))
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            tile = _fetch_tile(zoom, tx, ty)
            if tile:
                composite.paste(tile, ((tx - tx_min) * TILE_SIZE, (ty - ty_min) * TILE_SIZE))

    img_arr = np.array(composite)
    origin_px = tx_min * TILE_SIZE
    origin_py = ty_min * TILE_SIZE

    # GPS to local pixel coordinates
    gps_pixels = []
    for lat, lon in valid:
        px, py = lat_lon_to_pixel(lat, lon, zoom)
        gps_pixels.append((int(px - origin_px), int(py - origin_py)))

    # Distance mask from GPS trace
    max_dist_px_track = int(max_distance_m / mpp)
    gps_line = np.zeros((h, w), dtype=bool)
    for lx, ly in gps_pixels:
        if 0 <= lx < w and 0 <= ly < h:
            gps_line[ly, lx] = True
    distance_mask = binary_dilation(gps_line, iterations=max_dist_px_track)

    # Adaptive color thresholds from GPS trace samples
    r, g, b = img_arr[:, :, 0].astype(float), img_arr[:, :, 1].astype(float), img_arr[:, :, 2].astype(float)
    max_c = np.maximum(np.maximum(r, g), b)
    rgb_range = max_c - np.minimum(np.minimum(r, g), b)

    narrow_seed = binary_dilation(gps_line, iterations=3)
    val_mean = max_c[narrow_seed].mean()
    val_std = max_c[narrow_seed].std()
    range_mean = rgb_range[narrow_seed].mean()
    range_std = rgb_range[narrow_seed].std()

    val_lo = max(20, val_mean - color_sigma * val_std)
    val_hi = min(250, val_mean + color_sigma * val_std)
    range_hi = min(60, range_mean + color_sigma * range_std)

    # Detect and constrain
    asphalt = (max_c >= val_lo) & (max_c <= val_hi) & (rgb_range <= range_hi)
    track = asphalt & distance_mask
    track = binary_closing(track, iterations=3)

    # Keep largest GPS-connected component
    labeled, n_comp = label(track)
    best_comp, best_overlap = 0, 0
    for i in range(1, n_comp + 1):
        overlap = ((labeled == i) & narrow_seed).sum()
        if overlap > best_overlap:
            best_overlap = overlap
            best_comp = i

    if best_comp == 0:
        return {"outer": None, "error": "No track region found"}

    track_final = (labeled == best_comp).astype(np.uint8) * 255

    # Extract boundary as lat/lon
    boundary = (track_final > 0) & ~binary_erosion(track_final > 0, iterations=1)
    ys, xs = np.where(boundary)
    step = max(1, len(ys) // 2000)
    outer_coords = []
    for i in range(0, len(ys), step):
        lat, lon = pixel_to_lat_lon(xs[i] + origin_px, ys[i] + origin_py, zoom)
        outer_coords.append((lat, lon))

    # Save debug output
    out_dir = Path(__file__).parent / "track_detect_output"
    out_dir.mkdir(exist_ok=True)
    Image.fromarray(img_arr).save(out_dir / "satellite.png")
    Image.fromarray(track_final).save(out_dir / "mask.png")

    # Overlay
    from PIL import ImageDraw
    overlay = Image.fromarray(img_arr).convert('RGBA')
    mask_rgba = np.zeros((h, w, 4), dtype=np.uint8)
    mask_rgba[track_final > 0] = [0, 200, 0, 100]
    overlay = Image.alpha_composite(overlay, Image.fromarray(mask_rgba))
    b_layer = np.zeros((h, w, 4), dtype=np.uint8)
    b_layer[boundary] = [255, 255, 0, 220]
    overlay = Image.alpha_composite(overlay, Image.fromarray(b_layer))
    draw = ImageDraw.Draw(overlay)
    for i in range(len(gps_pixels) - 1):
        draw.line([gps_pixels[i], gps_pixels[i + 1]], fill=(255, 50, 50, 255), width=2)
    overlay.convert('RGB').save(out_dir / "overlay.png")

    return {
        "outer": outer_coords,
        "mask": track_final,
        "mpp": mpp,
        "origin_px": origin_px,
        "origin_py": origin_py,
        "zoom": zoom,
        "overlay_path": str(out_dir / "overlay.png"),
    }

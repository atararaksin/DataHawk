"""Satellite map widget with lap trajectory overlay."""

from __future__ import annotations

import math
import urllib.request

import pyqtgraph as pg
from PySide6.QtGui import QImage, QPixmap, QTransform
from PySide6.QtWidgets import QGraphicsPixmapItem

from datahawk.types import Lap
from datahawk.source.channel_constants import GPS_LATITUDE, GPS_LONGITUDE

# Esri World Imagery (free, no API key required)
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TILE_SIZE = 256


def _lat_lon_to_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Convert lat/lon to absolute pixel coordinates at given zoom."""
    n = 2 ** zoom
    px = (lon + 180.0) / 360.0 * n * TILE_SIZE
    lat_rad = math.radians(lat)
    py = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return px, py


def _filter_nan(lats: list[float], lons: list[float]) -> tuple[list[float], list[float]]:
    """Filter out NaN coordinates."""
    valid_lats, valid_lons = [], []
    for lat, lon in zip(lats, lons):
        if not (math.isnan(lat) or math.isnan(lon)):
            valid_lats.append(lat)
            valid_lons.append(lon)
    return valid_lats, valid_lons


class MapWidget(pg.PlotWidget):
    """Satellite map with GPS trajectory overlay and position markers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAspectLocked(True)
        self.hideAxis("left")
        self.hideAxis("bottom")
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)
        self.getViewBox().setBackgroundColor("k")
        self._tile_cache: dict[tuple[int, int, int], QPixmap] = {}
        self._zoom = 17
        self._current_lap: Lap | None = None
        self._ref_lap: Lap | None = None
        self._cur_marker = None
        self._ref_marker = None

    def set_laps(self, current_lap: Lap | None, ref_lap: Lap | None):
        """Full redraw: tiles + trajectories. Call when lap or reference changes."""
        self._current_lap = current_lap
        self._ref_lap = ref_lap
        self._full_redraw()

    def update_position(self, sample_idx: int):
        """Update only the position markers (fast, no tile reload)."""
        if self._cur_marker:
            self.removeItem(self._cur_marker)
            self._cur_marker = None
        if self._ref_marker:
            self.removeItem(self._ref_marker)
            self._ref_marker = None

        if self._current_lap is None:
            return

        lat_ch = self._current_lap.channels.get(GPS_LATITUDE)
        lon_ch = self._current_lap.channels.get(GPS_LONGITUDE)
        if lat_ch and lon_ch and 0 <= sample_idx < len(lat_ch.samples):
            clat, clon = lat_ch.samples[sample_idx], lon_ch.samples[sample_idx]
            if not (math.isnan(clat) or math.isnan(clon)):
                x, y = self._to_plot(clat, clon)
                self._cur_marker = self.plot(
                    [x], [y], pen=None, symbol="o", symbolSize=12,
                    symbolBrush="y", symbolPen="w")

        if self._ref_lap:
            ref_lat_ch = self._ref_lap.channels.get(GPS_LATITUDE)
            ref_lon_ch = self._ref_lap.channels.get(GPS_LONGITUDE)
            if ref_lat_ch and ref_lon_ch and 0 <= sample_idx < len(ref_lat_ch.samples):
                rlat, rlon = ref_lat_ch.samples[sample_idx], ref_lon_ch.samples[sample_idx]
                if not (math.isnan(rlat) or math.isnan(rlon)):
                    x, y = self._to_plot(rlat, rlon)
                    self._ref_marker = self.plot(
                        [x], [y], pen=None, symbol="o", symbolSize=12,
                        symbolBrush="r", symbolPen="w")

    def _to_plot(self, lat: float, lon: float) -> tuple[float, float]:
        """Convert lat/lon to plot coordinates (absolute pixel X, inverted pixel Y)."""
        px, py = _lat_lon_to_pixel(lat, lon, self._zoom)
        return px, -py

    def _full_redraw(self):
        """Redraw tiles and trajectories."""
        self.clear()
        self._cur_marker = None
        self._ref_marker = None

        if self._current_lap is None:
            return

        lat_ch = self._current_lap.channels.get(GPS_LATITUDE)
        lon_ch = self._current_lap.channels.get(GPS_LONGITUDE)
        if not lat_ch or not lon_ch:
            return

        cur_lats, cur_lons = _filter_nan(lat_ch.samples, lon_ch.samples)
        if not cur_lats:
            return

        # Collect all points for bounding box
        all_lats, all_lons = list(cur_lats), list(cur_lons)
        ref_lats, ref_lons = [], []
        if self._ref_lap:
            ref_lat_ch = self._ref_lap.channels.get(GPS_LATITUDE)
            ref_lon_ch = self._ref_lap.channels.get(GPS_LONGITUDE)
            if ref_lat_ch and ref_lon_ch:
                ref_lats, ref_lons = _filter_nan(ref_lat_ch.samples, ref_lon_ch.samples)
                all_lats.extend(ref_lats)
                all_lons.extend(ref_lons)

        # Bounding box with 15% margin
        min_lat, max_lat = min(all_lats), max(all_lats)
        min_lon, max_lon = min(all_lons), max(all_lons)
        lat_range = max_lat - min_lat or 0.001
        lon_range = max_lon - min_lon or 0.001
        min_lat -= lat_range * 0.15
        max_lat += lat_range * 0.15
        min_lon -= lon_range * 0.15
        max_lon += lon_range * 0.15

        self._zoom = self._fit_zoom(min_lat, max_lat, min_lon, max_lon)

        # Load tiles
        self._load_tiles(min_lat, max_lat, min_lon, max_lon)

        # Plot current lap trajectory (yellow)
        cur_pts = [self._to_plot(lat, lon) for lat, lon in zip(cur_lats, cur_lons)]
        self.plot([p[0] for p in cur_pts], [p[1] for p in cur_pts], pen=pg.mkPen("y", width=2))

        # Plot reference lap trajectory (red)
        if ref_lats:
            ref_pts = [self._to_plot(lat, lon) for lat, lon in zip(ref_lats, ref_lons)]
            self.plot([p[0] for p in ref_pts], [p[1] for p in ref_pts], pen=pg.mkPen("r", width=2))

        self.getViewBox().autoRange(padding=0)

    def _load_tiles(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float):
        """Fetch and display satellite tiles covering the bounding box."""
        z = self._zoom
        # Get pixel bounds
        px_left, px_top = _lat_lon_to_pixel(max_lat, min_lon, z)
        px_right, px_bottom = _lat_lon_to_pixel(min_lat, max_lon, z)

        tx_min = int(px_left // TILE_SIZE)
        tx_max = int(px_right // TILE_SIZE)
        ty_min = int(px_top // TILE_SIZE)
        ty_max = int(px_bottom // TILE_SIZE)

        for ty in range(ty_min, ty_max + 1):
            for tx in range(tx_min, tx_max + 1):
                pixmap = self._fetch_tile(tx, ty, z)
                if pixmap is None:
                    continue
                # ViewBox Y is inverted (north up), so pixmap appears flipped.
                # Flip vertically to compensate.
                flipped = pixmap.transformed(QTransform().scale(1, -1))
                item = QGraphicsPixmapItem(flipped)
                # Tile covers plot-y from -(ty*256) to -((ty+1)*256).
                # Pixmap renders downward in scene = upward in plot from pos.
                # So pos = bottom of tile in plot coords.
                item.setPos(tx * TILE_SIZE, -((ty + 1) * TILE_SIZE))
                item.setZValue(-1)
                self.addItem(item)

    def _fit_zoom(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> int:
        """Find zoom where bounding box fits in ~4x4 tiles."""
        for z in range(18, 12, -1):
            px0, py0 = _lat_lon_to_pixel(max_lat, min_lon, z)
            px1, py1 = _lat_lon_to_pixel(min_lat, max_lon, z)
            if (px1 - px0) <= 4 * TILE_SIZE and (py1 - py0) <= 4 * TILE_SIZE:
                return z
        return 13

    def _fetch_tile(self, tx: int, ty: int, z: int) -> QPixmap | None:
        """Fetch a tile with in-memory cache."""
        key = (tx, ty, z)
        if key in self._tile_cache:
            return self._tile_cache[key]
        url = TILE_URL.format(z=z, x=tx, y=ty)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DataHawk/0.1"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
            img = QImage()
            img.loadFromData(data)
            pixmap = QPixmap.fromImage(img)
            self._tile_cache[key] = pixmap
            return pixmap
        except Exception:
            return None

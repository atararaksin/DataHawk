"""Satellite map widget with lap trajectory overlay."""

from __future__ import annotations

import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pyqtgraph as pg
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QPixmap, QTransform
from PySide6.QtWidgets import QGraphicsPixmapItem

from datahawk.types import Lap, Track, Session
from datahawk.source.channel_constants import GPS_LATITUDE, GPS_LONGITUDE
from datahawk.session_utils import get_sample_index_for_session_time

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
    """Filter out NaN coordinates, paired."""
    valid_lats, valid_lons = [], []
    for lat, lon in zip(lats, lons):
        if not (math.isnan(lat) or math.isnan(lon)):
            valid_lats.append(lat)
            valid_lons.append(lon)
    return valid_lats, valid_lons


def _fetch_tile_data(url: str) -> bytes | None:
    """Fetch tile bytes (runs in thread pool)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DataHawk/0.1"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read()
    except Exception:
        return None


class MapWidget(pg.PlotWidget):
    """Satellite map with GPS trajectory overlay and position markers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAspectLocked(True)
        self.hideAxis("left")
        self.hideAxis("bottom")
        self.setMouseEnabled(x=True, y=True)
        self.setMenuEnabled(False)
        self.getViewBox().setBackgroundColor("k")
        self._tile_cache: dict[tuple[int, int, int], QPixmap] = {}
        self._tile_items: list[QGraphicsPixmapItem] = []
        self._trajectory_items: list = []
        self._zoom = 17
        self._current_lap: Lap | None = None
        self._ref_lap: Lap | None = None
        self._track: Track | None = None
        self._session: Session | None = None
        self._cur_marker = None
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._pending_futures: list[tuple] = []  # [(future, tx, ty, z), ...]
        self._tile_timer = QTimer()
        self._tile_timer.setInterval(50)
        self._tile_timer.timeout.connect(self._check_tile_futures)

    def set_laps(self, current_lap: Lap | None, ref_lap: Lap | None):
        """Update trajectories without clearing tiles."""
        self._current_lap = current_lap
        self._ref_lap = ref_lap
        self._update_trajectories()

    def _update_trajectories(self):
        """Redraw only trajectory lines and track lines, preserving tiles."""
        import sys; print("    _update_trajectories START", file=sys.stderr, flush=True)
        # Remove old trajectories and track lines (not tiles)
        for item in list(self._trajectory_items):
            self.removeItem(item)
        self._trajectory_items.clear()
        if self._cur_marker:
            self.removeItem(self._cur_marker)
            self._cur_marker = None
        print("    _update_trajectories cleared old items", file=sys.stderr, flush=True)

        if self._current_lap is None:
            return

        cur_lats, cur_lons = self._get_raw_coords(self._current_lap)
        print(f"    got cur coords: {len(cur_lats)} pts", file=sys.stderr, flush=True)
        all_lats, all_lons = list(cur_lats), list(cur_lons)
        ref_lats, ref_lons = [], []
        if self._ref_lap:
            ref_lats, ref_lons = self._get_raw_coords(self._ref_lap)
            all_lats.extend(ref_lats)
            all_lons.extend(ref_lons)

        if len(all_lats) < 2:
            return

        # Check if we need new tiles (bounding box changed significantly)
        min_lat, max_lat = min(all_lats), max(all_lats)
        min_lon, max_lon = min(all_lons), max(all_lons)
        lat_range = max_lat - min_lat or 0.001
        lon_range = max_lon - min_lon or 0.001
        min_lat -= lat_range * 0.15
        max_lat += lat_range * 0.15
        min_lon -= lon_range * 0.15
        max_lon += lon_range * 0.15

        new_zoom = self._fit_zoom(min_lat, max_lat, min_lon, max_lon)
        print(f"    zoom: current={self._zoom} new={new_zoom} tiles={len(self._tile_items)}", file=sys.stderr, flush=True)
        if new_zoom != self._zoom and not self._tile_items:
            # Only change zoom on first load (no tiles yet)
            self._zoom = new_zoom
            self._load_tiles_async(min_lat, max_lat, min_lon, max_lon)
        elif not self._tile_items:
            self._load_tiles_async(min_lat, max_lat, min_lon, max_lon)

        # Plot trajectories
        if cur_lats:
            print(f"    plotting {len(cur_lats)} pts", file=sys.stderr, flush=True)
            cur_pts = [self._to_plot(lat, lon) for lat, lon in zip(cur_lats, cur_lons)]
            item = self.plot([p[0] for p in cur_pts], [p[1] for p in cur_pts], pen=pg.mkPen("y", width=2))
            self._trajectory_items.append(item)
            print("    plot done", file=sys.stderr, flush=True)

        if ref_lats:
            ref_pts = [self._to_plot(lat, lon) for lat, lon in zip(ref_lats, ref_lons)]
            item = self.plot([p[0] for p in ref_pts], [p[1] for p in ref_pts], pen=pg.mkPen("r", width=2))
            self._trajectory_items.append(item)

        # Draw SF line and sector split lines
        if self._track:
            sf = self._track.sf_line
            ax, ay = self._to_plot(sf.a.lat, sf.a.lon)
            bx, by = self._to_plot(sf.b.lat, sf.b.lon)
            item = self.plot([ax, bx], [ay, by], pen=pg.mkPen("g", width=2))
            self._trajectory_items.append(item)
            for line in self._track.sector_split_lines:
                ax, ay = self._to_plot(line.a.lat, line.a.lon)
                bx, by = self._to_plot(line.b.lat, line.b.lon)
                item = self.plot([ax, bx], [ay, by], pen=pg.mkPen("w", width=2))
                self._trajectory_items.append(item)

    def _reload_tiles(self, min_lat, max_lat, min_lon, max_lon):
        """Clear tile items and reload. Only called when zoom/bbox changes."""
        for item in self._tile_items:
            self.getPlotItem().scene().removeItem(item)
        self._tile_items.clear()
        self.getViewBox().autoRange(padding=0)
        self._load_tiles_async(min_lat, max_lat, min_lon, max_lon)

    def set_session(self, session: Session):
        """Set session for temporal index lookups."""
        self._session = session

    def set_track(self, track: Track | None):
        """Update track reference. Actual redraw happens in set_laps."""
        self._track = track

    def update_position(self, session_time: float):
        """Update the current position marker for a given session time."""
        sample_idx = get_sample_index_for_session_time(self._session, session_time) if self._session else 0
        if self._cur_marker:
            self.removeItem(self._cur_marker)
            self._cur_marker = None

        if self._current_lap is None:
            return

        lat_ch = self._current_lap.channels.get(GPS_LATITUDE)
        lon_ch = self._current_lap.channels.get(GPS_LONGITUDE)
        if not lat_ch or not lon_ch:
            return

        # Use raw_values for position (works for incomplete laps too)
        lats = lat_ch.raw_values if lat_ch.raw_values else lat_ch.samples
        lons = lon_ch.raw_values if lon_ch.raw_values else lon_ch.samples

        if 0 <= sample_idx < len(lats):
            clat, clon = lats[sample_idx], lons[sample_idx]
            if not (math.isnan(clat) or math.isnan(clon)):
                x, y = self._to_plot(clat, clon)
                self._cur_marker = self.plot(
                    [x], [y], pen=None, symbol="o", symbolSize=8,
                    symbolBrush="y", symbolPen="w")

    def _to_plot(self, lat: float, lon: float) -> tuple[float, float]:
        """Convert lat/lon to plot coordinates."""
        px, py = _lat_lon_to_pixel(lat, lon, self._zoom)
        return px, -py

    def _get_raw_coords(self, lap: Lap) -> tuple[list[float], list[float]]:
        """Get GPS coordinates from a lap. Prefers raw_values, falls back to samples."""
        lat_ch = lap.channels.get(GPS_LATITUDE)
        lon_ch = lap.channels.get(GPS_LONGITUDE)
        if not lat_ch or not lon_ch:
            return [], []
        # Try raw_values first (works for incomplete laps)
        lats = lat_ch.raw_values if lat_ch.raw_values else lat_ch.samples
        lons = lon_ch.raw_values if lon_ch.raw_values else lon_ch.samples
        valid_lats, valid_lons = _filter_nan(lats, lons)
        # Need at least 2 points to draw a line
        if len(valid_lats) < 2:
            return [], []
        return valid_lats, valid_lons

    def _full_redraw(self):
        """Full clear and redraw. Only for initial load or zoom change."""
        self.clear()
        self._cur_marker = None
        self._tile_items.clear()
        self._trajectory_items.clear()

        if self._current_lap is None:
            return

        cur_lats, cur_lons = self._get_raw_coords(self._current_lap)

        # Collect all points for bounding box
        all_lats, all_lons = list(cur_lats), list(cur_lons)
        ref_lats, ref_lons = [], []
        if self._ref_lap:
            ref_lats, ref_lons = self._get_raw_coords(self._ref_lap)
            all_lats.extend(ref_lats)
            all_lons.extend(ref_lons)

        # Nothing to show at all
        if len(all_lats) < 2:
            return

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

        # Plot trajectories immediately (no network needed)
        if cur_lats:
            cur_pts = [self._to_plot(lat, lon) for lat, lon in zip(cur_lats, cur_lons)]
            self.plot([p[0] for p in cur_pts], [p[1] for p in cur_pts], pen=pg.mkPen("y", width=2))

        if ref_lats:
            ref_pts = [self._to_plot(lat, lon) for lat, lon in zip(ref_lats, ref_lons)]
            self.plot([p[0] for p in ref_pts], [p[1] for p in ref_pts], pen=pg.mkPen("r", width=2))

        # Draw SF line and sector split lines
        if self._track:
            # S/F line in green
            sf = self._track.sf_line
            ax, ay = self._to_plot(sf.a.lat, sf.a.lon)
            bx, by = self._to_plot(sf.b.lat, sf.b.lon)
            self.plot([ax, bx], [ay, by], pen=pg.mkPen("g", width=2))
            # Sector splits in white
            for line in self._track.sector_split_lines:
                ax, ay = self._to_plot(line.a.lat, line.a.lon)
                bx, by = self._to_plot(line.b.lat, line.b.lon)
                self.plot([ax, bx], [ay, by], pen=pg.mkPen("w", width=2))

        self.getViewBox().autoRange(padding=0)

        # Load tiles asynchronously
        self._load_tiles_async(min_lat, max_lat, min_lon, max_lon)

    def _load_tiles_async(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float):
        """Submit tile fetches to thread pool, poll for results via timer."""
        z = self._zoom
        px_left, px_top = _lat_lon_to_pixel(max_lat, min_lon, z)
        px_right, px_bottom = _lat_lon_to_pixel(min_lat, max_lon, z)

        tx_min = int(px_left // TILE_SIZE)
        tx_max = int(px_right // TILE_SIZE)
        ty_min = int(px_top // TILE_SIZE)
        ty_max = int(px_bottom // TILE_SIZE)

        self._pending_futures.clear()
        for ty in range(ty_min, ty_max + 1):
            for tx in range(tx_min, tx_max + 1):
                key = (tx, ty, z)
                if key in self._tile_cache:
                    self._place_tile(tx, ty, self._tile_cache[key])
                else:
                    url = TILE_URL.format(z=z, x=tx, y=ty)
                    future = self._executor.submit(_fetch_tile_data, url)
                    self._pending_futures.append((future, tx, ty, z))

        if self._pending_futures:
            self._tile_timer.start()

    def _check_tile_futures(self):
        """Poll pending tile futures from main thread (called by timer)."""
        still_pending = []
        for future, tx, ty, z in self._pending_futures:
            if future.done():
                data = future.result()
                if data and z == self._zoom:
                    img = QImage()
                    img.loadFromData(data)
                    pixmap = QPixmap.fromImage(img)
                    self._tile_cache[(tx, ty, z)] = pixmap
                    self._place_tile(tx, ty, pixmap)
            else:
                still_pending.append((future, tx, ty, z))
        self._pending_futures = still_pending
        if not self._pending_futures:
            self._tile_timer.stop()

    def _place_tile(self, tx: int, ty: int, pixmap: QPixmap):
        """Place a tile pixmap at the correct position."""
        flipped = pixmap.transformed(QTransform().scale(1, -1))
        item = QGraphicsPixmapItem(flipped)
        item.setPos(tx * TILE_SIZE, -((ty + 1) * TILE_SIZE))
        item.setZValue(-1)
        self.addItem(item)
        self._tile_items.append(item)

    def _fit_zoom(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> int:
        """Find zoom where bounding box fits in ~4x4 tiles."""
        for z in range(18, 12, -1):
            px0, py0 = _lat_lon_to_pixel(max_lat, min_lon, z)
            px1, py1 = _lat_lon_to_pixel(min_lat, max_lon, z)
            if (px1 - px0) <= 4 * TILE_SIZE and (py1 - py0) <= 4 * TILE_SIZE:
                return z
        return 13

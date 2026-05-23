"""Session viewer window with laps table, telemetry plot, video player, and sync cursor."""

from __future__ import annotations

import math
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QWidget, QComboBox, QLabel,
    QHBoxLayout, QTableWidget, QTableWidgetItem, QSplitter,
    QPushButton, QFileDialog, QSlider, QMessageBox,
)
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QBrush, QColor
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

from datahawk.xrz_parser import parse_xrz
from datahawk.session_processing import process_session
from datahawk.types import Session, Point
from datahawk.gps_utils import create_perpendecular_line
from datahawk.constants import CROSSING_LINE_LENGTH
from datahawk.sector_detection import populate_sectors
from datahawk.storage import save_track


class SessionViewer(QMainWindow):
    def __init__(self, xrz_path: Path, parent=None):
        super().__init__(parent)
        parsed = parse_xrz(xrz_path)
        self._session: Session = process_session(parsed)
        populate_sectors(self._session)
        self._xrz_path = xrz_path
        self._video_offset: float | None = None  # None = no sync

        meta_time = self._session.start_time
        self.setWindowTitle(f"DataHawk — {self._session.track} {self._session.date} {meta_time}")
        self.setMinimumSize(1200, 700)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # === Top section (2/3 height): lap table + video side by side ===
        top_splitter = QSplitter(Qt.Horizontal)

        # Lap table (fixed width 300)
        self._table = QTableWidget()
        self._table.setSelectionBehavior(QTableWidget.SelectItems)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setFixedWidth(400)
        font = self._table.font()
        font.setPointSize(font.pointSize() - 1)
        self._table.setFont(font)
        self._table.setCursor(Qt.PointingHandCursor)
        self._rebuild_lap_table()
        top_splitter.addWidget(self._table)

        # Video player
        video_container = QWidget()
        video_layout = QVBoxLayout(video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)

        self._video_widget = QVideoWidget()
        video_layout.addWidget(self._video_widget, 1)  # video takes all space

        # Video controls (overlay at bottom, minimal height)
        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(0, 0, 0, 0)
        self._btn_load = QPushButton("Load Video")
        self._btn_load.setFixedHeight(24)
        self._btn_play = QPushButton("▶")
        self._btn_play.setFixedSize(30, 24)
        self._btn_play.setEnabled(False)
        self._video_slider = QSlider(Qt.Horizontal)
        self._video_slider.setEnabled(False)
        self._video_slider.setFixedHeight(20)
        self._lbl_time = QLabel("--:-- / --:--")
        self._btn_sync = QPushButton("🔗")
        self._btn_sync.setFixedSize(30, 24)
        self._btn_sync.setCheckable(True)
        self._btn_sync.setEnabled(False)
        self._btn_sync.setToolTip("Sync video ↔ graph")
        self._btn_sync.clicked.connect(self._toggle_sync)
        ctrl_row.addWidget(self._btn_load)
        ctrl_row.addWidget(self._btn_play)
        ctrl_row.addWidget(self._btn_sync)
        ctrl_row.addWidget(self._video_slider)
        ctrl_row.addWidget(self._lbl_time)
        video_layout.addLayout(ctrl_row)

        # Hidden sync label (still computed, just not shown)
        self._lbl_offset = QLabel()
        self._lbl_offset.setVisible(False)

        top_splitter.addWidget(video_container)
        top_splitter.setStretchFactor(0, 0)  # table doesn't stretch
        top_splitter.setStretchFactor(1, 1)  # video takes remaining space

        # === Bottom section (1/3 height): channel selector + graph ===
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        # Channel selector and reference lap
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Channel:"))
        self._combo = QComboBox()
        self._combo.setMinimumWidth(250)
        top_row.addWidget(self._combo)
        top_row.addWidget(QLabel("Reference:"))
        self._ref_combo = QComboBox()
        self._ref_combo.addItem("None")
        for i, lap in enumerate(self._session.laps):
            self._ref_combo.addItem(f"Lap {i + 1} ({lap.lap_time:.2f}s)")
        top_row.addWidget(self._ref_combo)
        self._btn_sector = QPushButton("+ Sector")
        self._btn_sector.clicked.connect(self._add_sector_split)
        top_row.addWidget(self._btn_sector)
        self._btn_rm_sector = QPushButton("- Sector")
        self._btn_rm_sector.clicked.connect(self._remove_sector_split)
        top_row.addWidget(self._btn_rm_sector)
        top_row.addStretch()
        bottom_layout.addLayout(top_row)

        # Plot widget
        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Time", units="s")
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.scene().sigMouseClicked.connect(self._on_plot_click)
        bottom_layout.addWidget(self._plot)

        # Cursor line on plot
        self._cursor = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen("r", width=2))
        self._cursor.setVisible(False)
        self._plot.addItem(self._cursor)

        # === Main vertical splitter: top (2/3) + bottom (1/3) ===
        vsplitter = QSplitter(Qt.Vertical)
        vsplitter.addWidget(top_splitter)
        vsplitter.addWidget(bottom)
        vsplitter.setSizes([500, 250])
        main_layout.addWidget(vsplitter)

        # Media player
        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video_widget)

        # Populate channel dropdown
        self._channel_names: list[str] = []
        if self._session.laps:
            for name in sorted(self._session.laps[0].channels.keys()):
                self._combo.addItem(name)
                self._channel_names.append(name)
            if "GPS Speed" in self._channel_names:
                self._combo.setCurrentIndex(self._channel_names.index("GPS Speed"))

        # Connections
        self._combo.currentIndexChanged.connect(self._update_plot)
        self._ref_combo.currentIndexChanged.connect(self._update_plot)
        self._table.cellClicked.connect(self._on_table_cell_clicked)
        self._btn_load.clicked.connect(self._load_video)
        self._btn_play.clicked.connect(self._toggle_play)
        self._player.durationChanged.connect(self._on_duration)
        self._player.positionChanged.connect(self._on_position)
        self._video_slider.sliderMoved.connect(self._seek_video)

        # Sync timer: update cursor from video position at 50Hz
        self._sync_timer = QTimer()
        self._sync_timer.setInterval(20)  # 50Hz
        self._sync_timer.timeout.connect(self._sync_cursor)

        # Select first lap
        self._active_lap_idx = 0
        self._current_session_time = 0.0
        if self._session.laps:
            self.jump_to_time(self._session.laps[0].lap_start_time)
            self._update_plot()


    def _rebuild_lap_table(self):
        """Rebuild the laps table with current sector columns."""
        self._table.blockSignals(True)
        n_sectors = len(self._session.laps[0].sector_times) if self._session.laps else 0
        headers = ["Lap", "Time"] + [f"S{i+1}" for i in range(n_sectors)]
        self._table.setColumnCount(len(headers))
        self._table.setRowCount(len(self._session.laps))
        self._table.setHorizontalHeaderLabels(headers)
        purple = QBrush(QColor(128, 0, 128))
        best_lap_idx = self._session.reference_lap_index
        # Find fastest sector times
        best_sectors = [float('inf')] * n_sectors
        for lap in self._session.laps:
            for s, st in enumerate(lap.sector_times):
                if not math.isnan(st) and st < best_sectors[s]:
                    best_sectors[s] = st
        for i, lap in enumerate(self._session.laps):
            self._table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            item = QTableWidgetItem(f"{lap.lap_time:.2f}")
            if i == best_lap_idx:
                item.setForeground(purple)
            self._table.setItem(i, 1, item)
            for s, st in enumerate(lap.sector_times):
                text = f"{st:.2f}" if not math.isnan(st) else "—"
                item = QTableWidgetItem(text)
                if not math.isnan(st) and st == best_sectors[s]:
                    item.setForeground(purple)
                self._table.setItem(i, 2 + s, item)
        self._table.resizeColumnsToContents()
        self._table.blockSignals(False)

    def _load_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Video", str(self._xrz_path.parent), "Video (*.mp4 *.MP4 *.mov *.avi)")
        if not path:
            return

        self._player.setSource(QUrl.fromLocalFile(path))
        self._btn_play.setEnabled(True)
        self._video_slider.setEnabled(True)
        self._lbl_offset.setText("Sync: computing...")

        # Run sync in background (takes ~0.4s)
        QTimer.singleShot(100, lambda: self._compute_sync(path))

    def _compute_sync(self, video_path: str):
        try:
            from datahawk.video_sync import sync_by_acceleration, sync_by_timestamp
            from datahawk.xrz_parser import parse_xrz as _parse
            parsed = _parse(self._xrz_path)

            result = sync_by_acceleration(video_path, parsed)
            if result.confidence == "low":
                ts_result = sync_by_timestamp(video_path, parsed)
                if abs(ts_result.offset_seconds) < 86400:  # clock seems valid
                    result = ts_result

            self._video_offset = result.offset_seconds
            label = f"Sync: {result.offset_seconds:+.2f}s ({result.method}"
            if result.method == "accel":
                label += f", r={result.correlation:.2f}, {result.confidence})"
            else:
                label += ")"
            self._lbl_offset.setText(label)
            self._btn_sync.setEnabled(True)
            self._btn_sync.setChecked(True)
            self._btn_sync.setStyleSheet("background-color: green;")
            self._cursor.setVisible(True)
            self._sync_timer.start()
        except Exception as e:
            self._lbl_offset.setText(f"Sync failed: {e}")
            self._video_offset = None
            self._btn_sync.setEnabled(True)
            self._btn_sync.setChecked(False)

    def _toggle_play(self):
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
            self._btn_play.setText("▶")
            if self._video_offset is not None:
                self._sync_timer.stop()
        else:
            self._player.play()
            self._btn_play.setText("⏸")
            if self._video_offset is not None:
                self._sync_timer.start()

    def _toggle_sync(self):
        """Toggle sync between video and graph. When enabling, set offset from current positions."""
        if self._btn_sync.isChecked():
            video_s = self._player.position() / 1000.0
            self._video_offset = video_s - self._current_session_time
            self._cursor.setVisible(True)
            self._btn_sync.setStyleSheet("background-color: green;")
            if self._player.playbackState() == QMediaPlayer.PlayingState:
                self._sync_timer.start()
        else:
            self._video_offset = None
            self._btn_sync.setStyleSheet("")
            self._sync_timer.stop()

    def _on_duration(self, ms):
        self._video_slider.setRange(0, ms)

    def _on_position(self, ms):
        if not self._video_slider.isSliderDown():
            self._video_slider.setValue(ms)
        dur = self._player.duration()
        self._lbl_time.setText(f"{ms // 60000}:{(ms // 1000) % 60:02d} / {dur // 60000}:{(dur // 1000) % 60:02d}")

    def _seek_video(self, ms):
        self._player.setPosition(ms)

    def jump_to_time(self, session_time: float):
        """Jump to a given session time: select the active lap and place the cursor."""
        if not self._session.laps:
            return

        self._current_session_time = session_time

        # Find active lap by comparing against lap start times
        lap_idx = 0
        for i, lap in enumerate(self._session.laps):
            if session_time >= lap.lap_start_time:
                lap_idx = i
            else:
                break

        # Select lap if changed
        if lap_idx != self._active_lap_idx:
            self._active_lap_idx = lap_idx
            self._update_plot()

        # Position cursor: X axis is time relative to lap start
        cursor_x = session_time - self._session.laps[lap_idx].lap_start_time
        self._cursor.setVisible(True)
        self._cursor.setPos(cursor_x)

        self._select_active_table_cell()

    def _select_active_table_cell(self):
        """Highlight the current sector cell of the current lap in the table."""
        lap = self._session.laps[self._active_lap_idx]
        session_time = self._current_session_time

        # Determine which sector we're in
        sector_idx = 0
        for i, split_time in enumerate(lap.sector_split_times):
            if not math.isnan(split_time) and session_time >= split_time:
                sector_idx = i + 1
            else:
                break

        col = 2 + sector_idx  # columns: Lap, Time, S1, S2, ...
        self._table.blockSignals(True)
        self._table.setCurrentCell(self._active_lap_idx, col)
        self._table.blockSignals(False)

    def get_sample_index_for_session_time(self, session_time: float) -> int:
        """Get the reindexed sample index for a given session time using the temporal index."""
        start = self._session.laps[0].lap_start_time
        idx = int((session_time - start) / self._session.time_resolution)
        if idx < 0:
            return 0
        if idx >= len(self._session.temporal_index):
            return self._session.temporal_index[-1].sample_index if self._session.temporal_index else 0
        return self._session.temporal_index[idx].sample_index

    def jump_video_to_time(self, session_time: float):
        """Seek video to match a given session time (only if sync is on)."""
        if self._video_offset is None:
            return
        video_s = session_time + self._video_offset
        self._player.setPosition(int(video_s * 1000))

    def jump_to_lap(self, lap_idx: int):
        """Jump to target lap at the same spatial position as current cursor."""
        if lap_idx == self._active_lap_idx:
            return
        if lap_idx < 0 or lap_idx >= len(self._session.laps):
            return

        # Find spatial position (sample index) at current cursor
        sample_idx = self.get_sample_index_for_session_time(self._current_session_time)

        # Look up the same spatial position in target lap's Master Clk
        target_lap = self._session.laps[lap_idx]
        target_mc = target_lap.channels.get("Master Clk")
        if target_mc and sample_idx < len(target_mc.samples):
            t = target_mc.samples[sample_idx]
            if not math.isnan(t):
                session_time = t
            else:
                session_time = target_lap.lap_start_time
        else:
            session_time = target_lap.lap_start_time

        self.jump_to_time(session_time)
        self.jump_video_to_time(session_time)

    def jump_to_sector(self, lap_idx: int, sector_idx: int):
        """Jump to the beginning of a sector in a given lap."""
        if lap_idx < 0 or lap_idx >= len(self._session.laps):
            return
        lap = self._session.laps[lap_idx]
        if sector_idx == 0:
            session_time = lap.lap_start_time
        elif sector_idx - 1 < len(lap.sector_split_times):
            session_time = lap.sector_split_times[sector_idx - 1]
            if math.isnan(session_time):
                return
        else:
            return
        session_time += 0.01
        self.jump_to_time(session_time)
        self.jump_video_to_time(session_time)
    def _on_table_cell_clicked(self, row: int, col: int):
        """Handle click on a table cell — jump to lap or sector."""
        if col < 2:  # Lap or Time column
            if row != self._active_lap_idx:
                self.jump_to_lap(row)
        else:  # Sector columns
            sector_idx = col - 2
            self.jump_to_sector(row, sector_idx)

    def _on_plot_click(self, event):
        """Handle click on the plot to seek to that time."""
        pos = event.scenePos()
        if not self._plot.sceneBoundingRect().contains(pos):
            return
        mouse_point = self._plot.plotItem.vb.mapSceneToView(pos)
        session_time = self._session.laps[self._active_lap_idx].lap_start_time + mouse_point.x()
        self.jump_to_time(session_time)
        self.jump_video_to_time(session_time)

    def _save_track(self):
        """Persist track SF line and sector splits to DB."""
        save_track(self._session.track)

    def _add_sector_split(self):
        """Create a sector split line at the current cursor position."""
        session_time = self._current_session_time
        sample_idx = self.get_sample_index_for_session_time(session_time)

        # Check if within track limits using Master Clk continuity on current lap
        current_lap = self._session.laps[self._active_lap_idx]
        mc_ch = current_lap.channels.get("Master Clk")
        if mc_ch and sample_idx + 1 < len(mc_ch.samples):
            if math.isnan(mc_ch.samples[sample_idx]) or math.isnan(mc_ch.samples[sample_idx + 1]):
                QMessageBox.warning(self, "Error", "Can't split sector here - outside track limits")
                return

        # Get reference lap's lat/lon/heading at this spatial position
        ref_lap = self._session.laps[self._session.reference_lap_index]
        lat_ch = ref_lap.channels.get("GPS Latitude")
        lon_ch = ref_lap.channels.get("GPS Longitude")
        heading_ch = ref_lap.channels.get("GPS Heading")

        if not (lat_ch and lon_ch and heading_ch):
            QMessageBox.warning(self, "Error", "Can't split sector here - missing GPS channels")
            return

        lat = lat_ch.samples[sample_idx]
        lon = lon_ch.samples[sample_idx]
        heading = heading_ch.samples[sample_idx]

        if math.isnan(lat) or math.isnan(lon) or math.isnan(heading):
            QMessageBox.warning(self, "Error", "Can't split sector here - outside track limits")
            return

        split_line = create_perpendecular_line(Point(lat, lon), heading, CROSSING_LINE_LENGTH)
        self._session.track.sector_split_lines.append(split_line)
        populate_sectors(self._session)
        self._rebuild_lap_table()
        self._update_plot()
        self._save_track()
        print(f"Sector split added at t={session_time:.3f}s, lat={lat:.6f}, lon={lon:.6f}, heading={heading:.1f}°")

    def _remove_sector_split(self):
        """Remove all sector splits within ±2s of current time."""
        session_time = self._current_session_time
        lap = self._session.laps[self._active_lap_idx]

        to_remove = []
        for i, split_time in enumerate(lap.sector_split_times):
            if not math.isnan(split_time) and abs(split_time - session_time) < 2.0:
                to_remove.append(i)

        if not to_remove:
            return

        for i in reversed(to_remove):
            del self._session.track.sector_split_lines[i]
        populate_sectors(self._session)
        self._rebuild_lap_table()
        self._update_plot()
        self._save_track()

    def _sync_cursor(self):
        """Update plot cursor and active lap from video position."""
        if not self._session.laps or self._video_offset is None:
            return

        video_ms = self._player.position()
        video_s = video_ms / 1000.0
        session_time = video_s - self._video_offset
        self.jump_to_time(session_time)

    def closeEvent(self, event):
        self._sync_timer.stop()
        self._player.stop()
        super().closeEvent(event)

    def _update_plot(self, *_args):
        self._plot.clear()
        self._plot.addItem(self._cursor)

        if not self._channel_names or self._active_lap_idx >= len(self._session.laps):
            return

        lap_idx = self._active_lap_idx
        ch_name = self._channel_names[self._combo.currentIndex()]
        lap = self._session.laps[lap_idx]

        if ch_name not in lap.channels:
            return

        ch = lap.channels[ch_name]

        # Current lap: use raw (uninterpolated) data for full coverage
        times = ch.raw_timestamps
        samples = ch.raw_values

        self._plot.setLabel("left", ch_name)
        self._plot.plot(times, samples, pen=pg.mkPen("y", width=1), name=f"Lap {lap_idx + 1}")

        # Reference lap overlay: use current lap's reindexed time axis + ref's reindexed values
        ref_sel = self._ref_combo.currentIndex() - 1
        if ref_sel >= 0 and ref_sel != lap_idx:
            ref_lap = self._session.laps[ref_sel]
            if ch_name in ref_lap.channels:
                mc = lap.channels.get("Master Clk")
                if mc:
                    t0 = lap.lap_start_time
                    ref_times = []
                    ref_samples = []
                    for t, v in zip(mc.samples, ref_lap.channels[ch_name].samples):
                        if t == t and v == v:  # skip NaN
                            ref_times.append(t - t0)
                            ref_samples.append(v)
                    if ref_times:
                        self._plot.plot(ref_times, ref_samples, pen=pg.mkPen("g", width=1), name=f"Lap {ref_sel + 1}")

        # Sector split lines
        s1_line = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen("w", width=1),
                                  label="S1", labelOpts={"position": 0.95, "color": "w"})
        self._plot.addItem(s1_line)
        for i, split_time in enumerate(lap.sector_split_times):
            if not math.isnan(split_time):
                x = split_time - lap.lap_start_time
                line = pg.InfiniteLine(pos=x, angle=90, pen=pg.mkPen("w", width=1),
                                       label=f"S{i+2}", labelOpts={"position": 0.95, "color": "w"})
                self._plot.addItem(line)

        self._plot.enableAutoRange()

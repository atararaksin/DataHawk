"""Session viewer window with laps table, telemetry plot, video player, and sync cursor."""

from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QWidget, QComboBox, QLabel,
    QHBoxLayout, QTableWidget, QTableWidgetItem, QSplitter,
    QPushButton, QFileDialog, QSlider,
)
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

from datahawk.xrz_parser import parse_xrz
from datahawk.session_processing import process_session, Session


class SessionViewer(QMainWindow):
    def __init__(self, xrz_path: Path, parent=None):
        super().__init__(parent)
        parsed = parse_xrz(xrz_path)
        self._session: Session = process_session(parsed)
        self._xrz_path = xrz_path
        self._video_offset: float = 0.0  # video_time = session_time + offset

        meta_time = self._session.start_time
        self.setWindowTitle(f"DataHawk — {self._session.track} {self._session.date} {meta_time}")
        self.setMinimumSize(1200, 700)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # Left side: laps + plot
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # Laps table
        self._table = QTableWidget(len(self._session.laps), 2)
        self._table.setHorizontalHeaderLabels(["Lap", "Time"])
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setMaximumHeight(200)
        for i, lap in enumerate(self._session.laps):
            self._table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self._table.setItem(i, 1, QTableWidgetItem(f"{lap.lap_time:.3f}s"))
        self._table.resizeColumnsToContents()
        left_layout.addWidget(self._table)

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
            self._ref_combo.addItem(f"Lap {i + 1} ({lap.lap_time:.3f}s)")
        top_row.addWidget(self._ref_combo)
        top_row.addStretch()
        left_layout.addLayout(top_row)

        # Plot widget
        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Time", units="s")
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        left_layout.addWidget(self._plot)

        # Cursor line on plot
        self._cursor = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen("r", width=2))
        self._cursor.setVisible(False)
        self._plot.addItem(self._cursor)

        # Right side: video player
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._video_widget = QVideoWidget()
        self._video_widget.setMinimumWidth(400)
        right_layout.addWidget(self._video_widget)

        # Video controls
        ctrl_row = QHBoxLayout()
        self._btn_load = QPushButton("Load Video")
        self._btn_play = QPushButton("▶")
        self._btn_play.setFixedWidth(40)
        self._btn_play.setEnabled(False)
        self._video_slider = QSlider(Qt.Horizontal)
        self._video_slider.setEnabled(False)
        self._lbl_time = QLabel("--:-- / --:--")
        ctrl_row.addWidget(self._btn_load)
        ctrl_row.addWidget(self._btn_play)
        ctrl_row.addWidget(self._video_slider)
        ctrl_row.addWidget(self._lbl_time)
        right_layout.addLayout(ctrl_row)

        # Offset display
        self._lbl_offset = QLabel("Sync: no video loaded")
        right_layout.addWidget(self._lbl_offset)

        # Splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        main_layout.addWidget(splitter)

        # Media player
        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video_widget)

        # Populate channel dropdown
        self._channel_names: list[str] = []
        if self._session.laps:
            for name in sorted(self._session.laps[0].channels.keys()):
                if name != "Master Clk":
                    self._combo.addItem(name)
                    self._channel_names.append(name)

        # Connections
        self._combo.currentIndexChanged.connect(self._update_plot)
        self._ref_combo.currentIndexChanged.connect(self._update_plot)
        self._table.selectionModel().selectionChanged.connect(self._update_plot)
        self._btn_load.clicked.connect(self._load_video)
        self._btn_play.clicked.connect(self._toggle_play)
        self._player.durationChanged.connect(self._on_duration)
        self._player.positionChanged.connect(self._on_position)
        self._video_slider.sliderMoved.connect(self._seek_video)

        # Sync timer: update cursor from video position at 25Hz
        self._sync_timer = QTimer()
        self._sync_timer.setInterval(40)  # 25Hz
        self._sync_timer.timeout.connect(self._sync_cursor)

        # Select first lap
        if self._session.laps:
            self._table.selectRow(0)

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
            self._cursor.setVisible(True)
            self._sync_timer.start()
        except Exception as e:
            self._lbl_offset.setText(f"Sync failed: {e}")
            self._video_offset = 0.0

    def _toggle_play(self):
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
            self._btn_play.setText("▶")
            self._sync_timer.stop()
        else:
            self._player.play()
            self._btn_play.setText("⏸")
            self._sync_timer.start()

    def _on_duration(self, ms):
        self._video_slider.setRange(0, ms)

    def _on_position(self, ms):
        if not self._video_slider.isSliderDown():
            self._video_slider.setValue(ms)
        dur = self._player.duration()
        self._lbl_time.setText(f"{ms // 60000}:{(ms // 1000) % 60:02d} / {dur // 60000}:{(dur // 1000) % 60:02d}")

    def _seek_video(self, ms):
        self._player.setPosition(ms)

    def _sync_cursor(self):
        """Update plot cursor and active lap from video position."""
        if not self._session.temporal_index:
            return

        video_ms = self._player.position()
        video_s = video_ms / 1000.0
        session_time = video_s - self._video_offset

        if not self._session.laps:
            return

        first_mc = self._session.laps[0].channels.get("Master Clk")
        if not first_mc or not first_mc.samples:
            return
        t_start = first_mc.samples[0] or 0

        idx = int((session_time - t_start) / 0.04)
        if idx < 0 or idx >= len(self._session.temporal_index):
            self._cursor.setVisible(False)
            return

        self._cursor.setVisible(True)
        entry = self._session.temporal_index[idx]

        # Switch lap only when it actually changes
        current_rows = self._table.selectionModel().selectedRows()
        current_lap = current_rows[0].row() if current_rows else -1
        if entry.lap_index != current_lap:
            self._table.selectionModel().blockSignals(True)
            self._table.selectRow(entry.lap_index)
            self._table.selectionModel().blockSignals(False)
            self._update_plot()

        # Position cursor
        lap = self._session.laps[entry.lap_index]
        mc = lap.channels.get("Master Clk")
        if mc and entry.sample_index < len(mc.samples):
            t0 = mc.samples[0] or 0
            cursor_x = (mc.samples[entry.sample_index] or 0) - t0
            self._cursor.setPos(cursor_x)

    def _update_plot(self, *_args):
        self._plot.clear()
        self._plot.addItem(self._cursor)

        rows = self._table.selectionModel().selectedRows()
        if not rows or not self._channel_names:
            return

        lap_idx = rows[0].row()
        ch_name = self._channel_names[self._combo.currentIndex()]
        lap = self._session.laps[lap_idx]

        if ch_name not in lap.channels:
            return

        mc = lap.channels.get("Master Clk")
        if not mc:
            return

        t0 = mc.samples[0] if mc.samples else 0
        times = [t - t0 for t in mc.samples]
        samples = lap.channels[ch_name].samples

        self._plot.setLabel("left", ch_name)
        self._plot.plot(times, samples, pen=pg.mkPen("y", width=1), name=f"Lap {lap_idx + 1}")

        # Reference lap overlay
        ref_sel = self._ref_combo.currentIndex() - 1
        if ref_sel >= 0 and ref_sel != lap_idx:
            ref_lap = self._session.laps[ref_sel]
            if ch_name in ref_lap.channels:
                ref_samples = ref_lap.channels[ch_name].samples
                self._plot.plot(times, ref_samples, pen=pg.mkPen("c", width=1, style=Qt.DashLine), name=f"Lap {ref_sel + 1}")

        self._plot.enableAutoRange()

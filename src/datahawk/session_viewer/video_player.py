"""Video player widget with telemetry sync."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider, QLabel, QFileDialog,
)
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

from datahawk.source.types import SourceSession

log = logging.getLogger("datahawk.video_player")


class VideoPlayer(QWidget):
    """Video player with telemetry sync support."""

    # Emitted at 50Hz during synced playback with the current session time
    session_time_changed = Signal(float)
    # Emitted when video offset changes (auto-sync or manual resync)
    video_offset_changed = Signal(object)  # float or None
    # Emitted when user loads a new video file via the Load button
    video_path_changed = Signal(object)  # Path

    def __init__(self, parent=None):
        super().__init__(parent)
        self._video_offset: float | None = None
        self._current_session_time = 0.0
        self._source_session: SourceSession | None = None
        self._is_mychron_session = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._video_widget = QVideoWidget()
        layout.addWidget(self._video_widget, 1)

        # Controls
        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(0, 0, 0, 0)
        self._btn_load = QPushButton("Load Video")
        self._btn_load.setFixedHeight(24)
        self._btn_play = QPushButton("▶")
        self._btn_play.setFixedSize(30, 24)
        self._btn_play.setEnabled(False)
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setEnabled(False)
        self._slider.setFixedHeight(20)
        self._lbl_time = QLabel("--:-- / --:--")
        self._btn_sync = QPushButton("🔗")
        self._btn_sync.setFixedSize(30, 24)
        self._btn_sync.setCheckable(True)
        self._btn_sync.setEnabled(False)
        self._btn_sync.setToolTip("Sync video ↔ graph")
        ctrl_row.addWidget(self._btn_load)
        ctrl_row.addWidget(self._btn_play)
        ctrl_row.addWidget(self._btn_sync)
        ctrl_row.addWidget(self._slider)
        ctrl_row.addWidget(self._lbl_time)
        layout.addLayout(ctrl_row)

        # Media player
        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video_widget)

        # Connections
        self._btn_load.clicked.connect(self._on_load)
        self._btn_play.clicked.connect(self._toggle_play)
        self._btn_sync.clicked.connect(self._toggle_sync)
        self._player.durationChanged.connect(self._on_duration)
        self._player.positionChanged.connect(self._on_position)
        self._slider.sliderMoved.connect(self._player.setPosition)

        # Sync timer: emit session_time_changed at 50Hz during playback
        self._sync_timer = QTimer()
        self._sync_timer.setInterval(20)
        self._sync_timer.timeout.connect(self._emit_sync)

    def set_source_session(self, source_session: SourceSession):
        """Set the source session for sync computation."""
        self._source_session = source_session

    def load_video(self, path: Path, *, is_mychron_session: bool = False):
        """Load a video file. If is_mychron_session, run auto-sync. Otherwise offset is 0."""
        self._is_mychron_session = is_mychron_session
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._btn_play.setEnabled(True)
        self._slider.setEnabled(True)
        self._player.setPosition(1)
        if is_mychron_session:
            QTimer.singleShot(100, lambda: self._compute_sync(str(path)))
        else:
            self._video_offset = 0.0
            self._activate_sync()
            self.video_offset_changed.emit(0.0)

    def load_video_with_offset(self, path: Path, offset: float, *, is_mychron_session: bool = False):
        """Load a video file with a known offset (from DB)."""
        self._is_mychron_session = is_mychron_session
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._btn_play.setEnabled(True)
        self._slider.setEnabled(True)
        self._player.setPosition(1)
        self._video_offset = offset
        self._activate_sync()

    def seek_to_session_time(self, session_time: float):
        """Seek video to match a given session time (only if sync is active)."""
        self._current_session_time = session_time
        if self._video_offset is None:
            return
        video_s = session_time + self._video_offset
        pos_ms = int(video_s * 1000)
        state = self._player.playbackState()
        log.info(f"SEEK session_time={session_time:.3f} video_s={video_s:.3f} pos_ms={pos_ms} state={state} mediaStatus={self._player.mediaStatus()}")
        t0 = time.perf_counter()
        self._player.setPosition(pos_ms)
        elapsed = (time.perf_counter() - t0) * 1000
        log.info(f"SEEK setPosition returned in {elapsed:.1f}ms")

    def update_session_time(self, session_time: float):
        """Update the current session time (for sync toggle reference)."""
        self._current_session_time = session_time

    def stop(self):
        """Stop playback and timer."""
        self._sync_timer.stop()
        self._player.stop()

    def toggle_play(self):
        """Toggle play/pause."""
        self._toggle_play()

    def _activate_sync(self):
        self._btn_sync.setEnabled(True)
        self._btn_sync.setChecked(True)
        self._btn_sync.setStyleSheet("background-color: green;")
        self._sync_timer.start()

    def _on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Video", "", "Video (*.mp4 *.MP4 *.mov *.avi)")
        if path:
            self.video_path_changed.emit(Path(path))
            self.load_video(Path(path), is_mychron_session=self._is_mychron_session)

    def _compute_sync(self, video_path: str):
        try:
            from datahawk.source.gopro.gopro_video_sync import is_gopro_video
            from datahawk.source.gopro.gopro_video_sync import sync_by_acceleration as gopro_sync_accel
            from datahawk.source.gopro.gopro_video_sync import sync_by_timestamp as gopro_sync_ts
            from datahawk.source.insta360.insta360_video_sync import is_insta360_video
            from datahawk.source.insta360.insta360_video_sync import sync_by_acceleration as insta360_sync_accel

            if not self._is_mychron_session:
                self._video_offset = 0.0
                self._activate_sync()
                return

            parsed = self._source_session
            if is_insta360_video(video_path):
                result = insta360_sync_accel(video_path, parsed)
            elif is_gopro_video(video_path):
                result = gopro_sync_accel(video_path, parsed)
                if result.confidence == "low":
                    ts_result = gopro_sync_ts(video_path, parsed)
                    if abs(ts_result.offset_seconds) < 86400:
                        result = ts_result
            else:
                result = gopro_sync_accel(video_path, parsed)
                if result.confidence == "low":
                    ts_result = gopro_sync_ts(video_path, parsed)
                    if abs(ts_result.offset_seconds) < 86400:
                        result = ts_result

            self._video_offset = result.offset_seconds
            self._activate_sync()
            self.video_offset_changed.emit(self._video_offset)
        except Exception:
            self._video_offset = None
            self._btn_sync.setEnabled(True)
            self._btn_sync.setChecked(False)

    def _toggle_play(self):
        state = self._player.playbackState()
        log.info(f"TOGGLE_PLAY current_state={state} mediaStatus={self._player.mediaStatus()}")
        if state == QMediaPlayer.PlayingState:
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
        if self._btn_sync.isChecked():
            video_s = self._player.position() / 1000.0
            self._video_offset = video_s - self._current_session_time
            self._btn_sync.setStyleSheet("background-color: green;")
            if self._player.playbackState() == QMediaPlayer.PlayingState:
                self._sync_timer.start()
            self.video_offset_changed.emit(self._video_offset)
        else:
            self._video_offset = None
            self._btn_sync.setStyleSheet("")
            self._sync_timer.stop()
            self.video_offset_changed.emit(None)

    def _on_duration(self, ms):
        self._slider.setRange(0, ms)

    def _on_position(self, ms):
        if not self._slider.isSliderDown():
            self._slider.setValue(ms)
        dur = self._player.duration()
        self._lbl_time.setText(f"{ms // 60000}:{(ms // 1000) % 60:02d} / {dur // 60000}:{(dur // 1000) % 60:02d}")

    def _emit_sync(self):
        if self._video_offset is None:
            return
        video_s = self._player.position() / 1000.0
        session_time = video_s - self._video_offset
        state = self._player.playbackState()
        media_status = self._player.mediaStatus()
        if state != QMediaPlayer.PlayingState:
            log.warning(f"SYNC_TIMER firing but player not playing: state={state} mediaStatus={media_status}")
        self.session_time_changed.emit(session_time)

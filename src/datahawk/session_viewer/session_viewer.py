"""Session viewer widget with laps table, telemetry plot, video player, and sync cursor."""

from __future__ import annotations

import math
from pathlib import Path
from PySide6.QtWidgets import (
    QVBoxLayout, QWidget,
    QHBoxLayout, QSplitter,
    QPushButton,
    QTabWidget, QScrollArea,
)
from PySide6.QtCore import Qt, QEvent, Signal
from PySide6.QtMultimedia import QMediaPlayer

from datahawk.source.channel_constants import GPS_SPEED
from datahawk.session_processing import build_session
from datahawk.session_utils import get_channel_value_in_another_lap_with_interpolation, get_sample_index_for_session_time, create_perpendicular_line_at_time, get_lap_idx_by_session_time
from datahawk.session_processing import populate_sectors
from datahawk.storage import delete_track
from datahawk.session_viewer.map_widget import MapWidget
from datahawk.session_viewer.lap_table import LapTable, LapTableLapClicked, LapTableSectorClicked
from datahawk.session_viewer.telemetry_graph import GraphClicked
from datahawk.session_viewer.graph_panel import GraphPanel
from datahawk.session_viewer.video_player import VideoPlayer
from datahawk.session_viewer.delta_bar import DeltaBar


class SessionViewer(QWidget):
    """Session viewer tab widget. Emits track_changed when track is mutated."""

    track_changed = Signal(object)  # emits Track
    ref_selected = Signal(object)  # emits Lap (the reference lap object)

    def __init__(self, source_session, session, parent=None, *, video_path: Path | None = None, session_id: str = "", source_type: str = ""):
        super().__init__(parent)
        self._source_session = source_session
        self._session = session
        self._session_id = session_id or getattr(source_session.metadata, 'filename', '') or ''
        self._video_path = video_path
        self._source_type = source_type
        populate_sectors(self._session)

        from PySide6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

        main_layout = QVBoxLayout(self)

        # === Top section (2/3 height): lap table + video side by side ===
        top_splitter = QSplitter(Qt.Horizontal)

        # Lap table + controls
        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self._table = LapTable()
        self._table.rebuild(self._session)
        self._table.lap_clicked.connect(self._on_lap_clicked)
        self._table.sector_clicked.connect(self._on_sector_clicked)
        table_layout.addWidget(self._table)
        self._btn_set_ref = QPushButton("Set as Reference")
        self._btn_set_ref.clicked.connect(self._on_set_ref_clicked)
        table_layout.addWidget(self._btn_set_ref)
        table_container.setFixedWidth(400)
        top_splitter.addWidget(table_container)

        # Video player + delta bar
        video_container = QWidget()
        video_layout = QVBoxLayout(video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)
        self._video = VideoPlayer()
        self._video.set_source_session(source_session)
        self._video.session_time_changed.connect(self.jump_to_time)
        self._video.video_offset_changed.connect(self._on_video_offset_changed)
        self._video.video_path_changed.connect(self._on_video_path_changed)
        video_layout.addWidget(self._video, 1)
        self._delta_bar = DeltaBar()
        video_layout.addWidget(self._delta_bar)

        top_splitter.addWidget(video_container)
        top_splitter.setStretchFactor(0, 0)  # table doesn't stretch
        top_splitter.setStretchFactor(1, 1)  # video takes remaining space

        # === Bottom section (1/3 height): graphs + map ===
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        # Track controls row
        controls_row = QHBoxLayout()
        self._btn_sector = QPushButton("+ Sector")
        self._btn_sector.clicked.connect(self._add_sector_split)
        controls_row.addWidget(self._btn_sector)
        self._btn_rm_sector = QPushButton("- Sector")
        self._btn_rm_sector.clicked.connect(self._remove_sector_split)
        controls_row.addWidget(self._btn_rm_sector)
        self._btn_clear_track = QPushButton("Clear")
        self._btn_clear_track.clicked.connect(self._clear_track)
        controls_row.addWidget(self._btn_clear_track)
        self._btn_replace_sf = QPushButton("Replace SF")
        self._btn_replace_sf.clicked.connect(self._replace_sf_line)
        controls_row.addWidget(self._btn_replace_sf)
        self._btn_add_graph = QPushButton("+ Graph")
        self._btn_add_graph.clicked.connect(self._add_graph_panel)
        controls_row.addWidget(self._btn_add_graph)
        controls_row.addStretch()
        bottom_layout.addLayout(controls_row)

        # Graph and Map in tabs
        self._bottom_tabs = QTabWidget()
        self._bottom_tabs.setTabPosition(QTabWidget.South)

        # Multi-graph container (scrollable)
        self._graph_scroll = QScrollArea()
        self._graph_scroll.setWidgetResizable(True)
        self._graph_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._graph_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self._graph_scroll.setStyleSheet("""
            QScrollArea { background: transparent; }
            QScrollBar:vertical {
                width: 14px;
                background: #2b2b2b;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #666;
                min-height: 20px;
                border-radius: 4px;
                margin: 2px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)
        self._graph_container = QWidget()
        self._graph_layout = QVBoxLayout(self._graph_container)
        self._graph_layout.setContentsMargins(0, 0, 0, 0)
        self._graph_layout.setSpacing(4)
        self._graph_scroll.setWidget(self._graph_container)
        self._graph_panels: list[GraphPanel] = []
        self._bottom_tabs.addTab(self._graph_scroll, "Graph")

        # Satellite map widget
        self._map = MapWidget()
        self._map.set_session(self._session)
        self._bottom_tabs.addTab(self._map, "Map")

        bottom_layout.addWidget(self._bottom_tabs)

        # === Main vertical splitter: top (2/3) + bottom (1/3) ===
        vsplitter = QSplitter(Qt.Vertical)
        vsplitter.addWidget(top_splitter)
        vsplitter.addWidget(bottom)
        vsplitter.setSizes([500, 250])
        main_layout.addWidget(vsplitter)

        # Populate channel list and create default graph panel
        self._channel_names: list[str] = []
        if self._session.laps:
            self._channel_names = sorted(self._session.laps[0].channels.keys())

        # Reference lap (set externally by AnalysisWindow)
        self._ref_lap = None
        self._jumping = False

        # Select first lap
        self._active_lap_idx = 0
        self._current_session_time = 0.0

        # Default graph: GPS Speed
        self._add_graph_panel(default_channel=GPS_SPEED)

        if self._session.laps:
            self.jump_to_time(self._session.laps[0].lap_start_time)
            self._update_plot()
            self._update_map_full()

        # Video auto-load is handled by the caller (main.py) after construction
        # to correctly distinguish between load-with-offset vs auto-sync cases.


    def _rebuild_lap_table(self):
        """Rebuild the laps table with current sector columns."""
        self._table.rebuild(self._session)

    def jump_to_time(self, session_time: float):
        """Jump to a given session time: select the active lap and place the cursor."""
        if not self._session.laps:
            return
        if self._jumping:
            return
        self._jumping = True
        self._video._sync_timer.stop()

        self._current_session_time = session_time
        self._video.update_session_time(session_time)

        lap_idx = get_lap_idx_by_session_time(self._session, session_time)

        # Select lap if changed
        if lap_idx != self._active_lap_idx:
            self._active_lap_idx = lap_idx
            self._update_plot()
            self._update_map_full()

        # Position cursor on all graphs
        for panel in self._graph_panels:
            panel.set_cursor_session_time(session_time)

        # Update delta bar
        if self._active_lap_idx < len(self._session.laps):
            self._delta_bar.update_delta(
                self._session, session_time,
                self._session.laps[self._active_lap_idx], self._ref_lap)

        self._select_active_table_cell()
        self._update_map()

        self._jumping = False
        if self._video._video_offset is not None and self._video._player.playbackState() == QMediaPlayer.PlayingState:
            self._video._sync_timer.start()

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

        self._table.select_sector(self._active_lap_idx, sector_idx)

    def _update_map(self):
        """Update the satellite map position marker."""
        self._map.update_position(self._current_session_time)

    def _update_map_full(self):
        """Full map redraw (tiles + trajectories). Call on lap/reference change."""
        current_lap = self._session.laps[self._active_lap_idx] if self._session.laps else None
        self._map.set_track(self._session.track)
        self._map.set_laps(current_lap, self._ref_lap)

    def jump_to_lap(self, lap_idx: int):
        """Jump to target lap at the same spatial position as current cursor."""
        if lap_idx == self._active_lap_idx:
            return
        if lap_idx < 0 or lap_idx >= len(self._session.laps):
            return

        # Find spatial position (sample index) at current cursor
        sample_idx = get_sample_index_for_session_time(self._session, self._current_session_time)

        # Look up the same spatial position in target lap's Master Clk
        target_lap = self._session.laps[lap_idx]
        target_mc = target_lap.master_clk
        if target_mc and sample_idx < len(target_mc.samples):
            t = target_mc.samples[sample_idx]
            if not math.isnan(t):
                session_time = t
            else:
                session_time = target_lap.lap_start_time
        else:
            session_time = target_lap.lap_start_time

        self.jump_to_time(session_time)
        self._video.seek_to_session_time(session_time)

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
        self._video.seek_to_session_time(session_time)
    def _on_lap_clicked(self, event: LapTableLapClicked):
        """Handle lap click from table."""
        if event.lap_idx != self._active_lap_idx:
            self.jump_to_lap(event.lap_idx)

    def _on_sector_clicked(self, event: LapTableSectorClicked):
        """Handle sector click from table."""
        self.jump_to_sector(event.lap_idx, event.sector_idx)

    def _on_graph_click(self, event: GraphClicked):
        """Handle click on any graph to seek to that time."""
        self.jump_to_time(event.session_time)
        self._video.seek_to_session_time(event.session_time)

    def _add_graph_panel(self, default_channel: str = ""):
        """Add a new graph panel to the scroll area."""
        if not default_channel and self._channel_names:
            default_channel = self._channel_names[0]
        panel = GraphPanel(self._channel_names, default_channel)
        panel.setMinimumHeight(200)
        panel.clicked.connect(self._on_graph_click)
        panel.remove_requested.connect(self._remove_graph_panel)
        self._graph_panels.append(panel)
        self._graph_layout.addWidget(panel)
        self._update_remove_buttons()
        # Draw initial plot
        if self._session.laps:
            panel.update_plot(session=self._session, lap_idx=self._active_lap_idx, ref_lap=self._ref_lap)

    def _remove_graph_panel(self, panel: GraphPanel):
        """Remove a graph panel (minimum 1 must remain)."""
        if len(self._graph_panels) <= 1:
            return
        self._graph_panels.remove(panel)
        self._graph_layout.removeWidget(panel)
        panel.deleteLater()
        self._update_remove_buttons()

    def _update_remove_buttons(self):
        """Show/hide remove buttons based on panel count."""
        single = len(self._graph_panels) == 1
        for p in self._graph_panels:
            p.set_remove_visible(not single)

    def _on_set_ref_clicked(self):
        """Set current lap as the reference lap."""
        if self._active_lap_idx < len(self._session.laps):
            self.ref_selected.emit(self._session.laps[self._active_lap_idx])

    def set_reference_lap(self, lap):
        """Set the reference lap (from any session) and refresh display."""
        self._ref_lap = lap
        # Highlight matching row in this table if ref belongs to this session
        ref_row = None
        if lap is not None:
            for i, l in enumerate(self._session.laps):
                if l is lap:
                    ref_row = i
                    break
        self._table.set_ref_row(ref_row)
        self._update_plot()
        self._update_map_full()

    def _add_sector_split(self):
        """Create a sector split line at the current cursor position."""
        split_line = create_perpendicular_line_at_time(self._session, self._current_session_time)
        if split_line is None:
            return

        track = self._session.track
        track.sector_split_lines.append(split_line)
        self.track_changed.emit(track)

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

        track = self._session.track
        for i in reversed(to_remove):
            del track.sector_split_lines[i]
        self.track_changed.emit(track)

    def _clear_track(self):
        """Evict track from DB and reload session with auto-detected SF."""
        from datahawk.session_processing import detect_sf_line, detect_master_lap
        from datahawk.types import Track

        delete_track(self._session.track.name)
        sf_line = detect_sf_line(self._source_session)
        master_lap = detect_master_lap(self._source_session, sf_line)
        track = Track(name=self._session.track.name, sf_line=sf_line, master_lap=master_lap)
        self.track_changed.emit(track)

    def _replace_sf_line(self):
        """Replace the S/F line with a perpendicular line at the current position."""
        from datahawk.session_processing import detect_master_lap
        from datahawk.types import Track

        new_sf = create_perpendicular_line_at_time(self._session, self._current_session_time)
        if new_sf is None:
            return

        master_lap = detect_master_lap(self._source_session, new_sf)
        track = Track(
            name=self._session.track.name,
            sf_line=new_sf,
            master_lap=master_lap,
            sector_split_lines=self._session.track.sector_split_lines,
        )
        self.track_changed.emit(track)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Down:
                next_idx = min(self._active_lap_idx + 1, len(self._session.laps) - 1)
                self.jump_to_lap(next_idx)
                return True
            elif key == Qt.Key_Up:
                prev_idx = max(self._active_lap_idx - 1, 0)
                self.jump_to_lap(prev_idx)
                return True
            elif key == Qt.Key_Space:
                self._video.toggle_play()
                return True
            elif key == Qt.Key_Left:
                self.jump_to_time(self._current_session_time - 5.0)
                return True
            elif key == Qt.Key_Right:
                self.jump_to_time(self._current_session_time + 5.0)
                return True
        return super().eventFilter(obj, event)

    def _on_video_path_changed(self, path):
        """Update stored video path when user loads a new video."""
        self._video_path = path

    def _on_video_offset_changed(self, offset):
        """Persist video path and offset when sync changes."""
        if self._session_id and self._video_path:
            from datahawk.storage import save_session_video
            save_session_video(self._session_id, str(self._video_path), offset)

    def closeEvent(self, event):
        self._video.stop()
        super().closeEvent(event)

    @property
    def session_id(self) -> str:
        return self._session_id

    def rebuild_with_track(self, track):
        """Rebuild session with a new track. Called by AnalysisWindow on track changes."""
        prev_time = self._current_session_time
        self._session = build_session(self._source_session, track)
        populate_sectors(self._session)
        self._map.set_session(self._session)
        self._active_lap_idx = 0
        self._rebuild_lap_table()
        self.jump_to_time(prev_time)

    def _update_plot(self, *_args):
        if not self._channel_names or self._active_lap_idx >= len(self._session.laps):
            return
        for panel in self._graph_panels:
            panel.update_plot(
                session=self._session,
                lap_idx=self._active_lap_idx,
                ref_lap=self._ref_lap,
            )

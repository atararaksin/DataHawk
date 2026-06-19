"""Multi-session analysis window -- groups SessionViewer tabs by track."""

from __future__ import annotations

from PySide6.QtWidgets import QMainWindow, QTabWidget

from datahawk.session_viewer.session_viewer import SessionViewer
from datahawk.storage import save_track


class AnalysisWindow(QMainWindow):
    """Window holding multiple SessionViewer tabs for the same track."""

    def __init__(self, track_name: str, parent=None):
        super().__init__(parent)
        self._track_name = track_name
        self.setWindowTitle(f"DataHawk — {track_name}")
        self.setMinimumSize(1200, 700)

        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self._tabs)
        self._ref_lap = None

    @property
    def track_name(self) -> str:
        return self._track_name

    def add_session(self, source_session, session, *, video_path=None, label: str = "", session_id: str = "", source_type: str = ""):
        """Add a new SessionViewer tab. Returns the viewer."""
        viewer = SessionViewer(source_session, session, video_path=video_path, session_id=session_id, source_type=source_type)
        viewer.track_changed.connect(self._on_track_changed)
        viewer.ref_selected.connect(self._on_ref_selected)
        tab_label = label or f"{session.date} {session.start_time}"
        self._tabs.addTab(viewer, tab_label)
        self._tabs.setCurrentWidget(viewer)
        # Apply current reference if one is already set
        if self._ref_lap is not None:
            viewer.set_reference_lap(self._ref_lap)
        return viewer

    def has_session(self, session_id: str) -> bool:
        """Check if a session is already open (by matching source filename)."""
        for i in range(self._tabs.count()):
            viewer: SessionViewer = self._tabs.widget(i)
            if viewer.session_id == session_id:
                self._tabs.setCurrentIndex(i)
                return True
        return False

    def _on_track_changed(self, track: Track):
        """Rebuild all tabs with the new track."""
        save_track(track)
        for i in range(self._tabs.count()):
            viewer: SessionViewer = self._tabs.widget(i)
            viewer.rebuild_with_track(track)

    def _on_ref_selected(self, lap):
        """Set reference lap across all tabs."""
        self._ref_lap = lap
        for i in range(self._tabs.count()):
            viewer: SessionViewer = self._tabs.widget(i)
            viewer.set_reference_lap(lap)

    def _on_tab_changed(self, index: int):
        """Pause video on all non-active tabs."""
        for i in range(self._tabs.count()):
            if i != index:
                viewer: SessionViewer = self._tabs.widget(i)
                viewer._video._player.pause()
                viewer._video._sync_timer.stop()

    def _close_tab(self, index: int):
        viewer: SessionViewer = self._tabs.widget(index)
        viewer.close()
        self._tabs.removeTab(index)
        if self._tabs.count() == 0:
            self.close()

    def closeEvent(self, event):
        for i in range(self._tabs.count()):
            self._tabs.widget(i).close()
        super().closeEvent(event)

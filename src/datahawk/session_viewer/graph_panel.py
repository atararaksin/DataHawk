"""Single graph panel with its own channel selector."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QPushButton, QCheckBox
from PySide6.QtCore import Signal

from datahawk.session_viewer.telemetry_graph import TelemetryGraph, GraphClicked
from datahawk.types import Session


class GraphPanel(QWidget):
    """A single graph with its own channel combo. Embeddable in a multi-graph container."""

    clicked = Signal(object)  # GraphClicked
    remove_requested = Signal(object)  # self

    def __init__(self, channel_names: list[str], default_channel: str = "", parent=None):
        super().__init__(parent)
        self._channel_names = list(channel_names)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Header row: channel combo + diff + remove button
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._combo = QComboBox()
        self._combo.setMinimumWidth(150)
        for name in self._channel_names:
            self._combo.addItem(name)
        if default_channel in self._channel_names:
            self._combo.setCurrentIndex(self._channel_names.index(default_channel))
        self._combo.currentIndexChanged.connect(self._on_channel_changed)
        header.addWidget(self._combo)

        self._diff_cb = QCheckBox("Diff")
        self._diff_cb.stateChanged.connect(self._on_channel_changed)
        header.addWidget(self._diff_cb)

        header.addStretch()

        self._btn_remove = QPushButton("×")
        self._btn_remove.setFixedSize(24, 24)
        self._btn_remove.clicked.connect(lambda: self.remove_requested.emit(self))
        header.addWidget(self._btn_remove)

        layout.addLayout(header)

        self._graph = TelemetryGraph()
        self._graph.clicked.connect(self.clicked.emit)
        layout.addWidget(self._graph)

        # Plot state
        self._session: Session | None = None
        self._lap_idx = 0
        self._ref_lap = None

    @property
    def channel_name(self) -> str:
        if self._channel_names:
            return self._channel_names[self._combo.currentIndex()]
        return ""

    def set_remove_visible(self, visible: bool):
        self._btn_remove.setVisible(visible)

    def set_cursor_session_time(self, session_time: float):
        self._graph.set_cursor_session_time(session_time)

    def update_plot(self, *, session: Session, lap_idx: int, ref_lap=None):
        self._session = session
        self._lap_idx = lap_idx
        self._ref_lap = ref_lap
        self._redraw()

    def _redraw(self):
        if not self._session or not self._channel_names:
            return
        self._graph.update_plot(
            session=self._session,
            lap_idx=self._lap_idx,
            channel_name=self.channel_name,
            ref_lap=self._ref_lap,
            diff_mode=self._diff_cb.isChecked(),
        )

    def _on_channel_changed(self, *_):
        self._redraw()

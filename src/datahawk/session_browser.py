"""Session browser widget with event grouping."""

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QMenu, QMessageBox, QPushButton, QSplitter, QLabel, QToolBar,
)

from datahawk.storage import (
    list_events, list_sessions_for_event, delete_session, delete_event,
    create_event, get_event_track,
)


class SessionBrowser(QWidget):
    session_opened = Signal(str)  # emits session_id
    import_mychron_requested = Signal()  # user clicked import mychron
    import_video_requested = Signal()  # user clicked import video

    def __init__(self, parent=None):
        super().__init__(parent)
        self._events: list[dict] = []
        self._sessions: list[dict] = []
        self._selected_event_id: str = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # Left panel: events
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        from PySide6.QtWidgets import QSizePolicy
        event_tb = QToolBar()
        event_tb.setMovable(False)
        event_tb.addWidget(QLabel(" Events"))
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        event_tb.addWidget(spacer)
        add_act = event_tb.addAction("+")
        add_act.triggered.connect(self._on_add_event)
        del_act = event_tb.addAction("−")
        del_act.triggered.connect(self._on_delete_event)
        left_layout.addWidget(event_tb)

        self._event_table = QTableWidget()
        self._event_table.setColumnCount(2)
        self._event_table.setHorizontalHeaderLabels(["Event", "Date"])
        self._event_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._event_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._event_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._event_table.currentCellChanged.connect(self._on_event_selected)
        left_layout.addWidget(self._event_table)

        splitter.addWidget(left)

        # Right panel: sessions
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        session_tb = QToolBar()
        session_tb.setMovable(False)
        session_tb.addWidget(QLabel(" Sessions"))
        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        session_tb.addWidget(spacer2)
        mc_act = session_tb.addAction("Import from MyChron")
        mc_act.triggered.connect(self.import_mychron_requested.emit)
        vid_act = session_tb.addAction("Import from Video")
        vid_act.triggered.connect(self.import_video_requested.emit)
        right_layout.addWidget(session_tb)

        self._session_table = QTableWidget()
        self._session_table.setColumnCount(8)
        self._session_table.setHorizontalHeaderLabels(["Name", "Date", "Time", "Laps", "Track", "Best Lap", "Driver", "Type"])
        self._session_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._session_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._session_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._session_table.doubleClicked.connect(self._on_session_double_click)
        self._session_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._session_table.customContextMenuRequested.connect(self._on_session_context_menu)
        right_layout.addWidget(self._session_table)

        splitter.addWidget(right)
        splitter.setSizes([250, 750])

        self.refresh()

    @property
    def selected_event_id(self) -> str:
        return self._selected_event_id

    def refresh(self):
        self._events = list_events()
        self._event_table.setRowCount(len(self._events))
        for i, e in enumerate(self._events):
            self._event_table.setItem(i, 0, QTableWidgetItem(e["name"]))
            self._event_table.setItem(i, 1, QTableWidgetItem(e["date"] or ""))
        # Re-select same event or first
        if self._events:
            row = 0
            for i, e in enumerate(self._events):
                if e["id"] == self._selected_event_id:
                    row = i
                    break
            self._event_table.selectRow(row)
            self._select_event(row)
        else:
            self._sessions = []
            self._session_table.setRowCount(0)

    def _on_event_selected(self, row, _col, _prev_row, _prev_col):
        self._select_event(row)

    def _select_event(self, row):
        if row < 0 or row >= len(self._events):
            return
        self._selected_event_id = self._events[row]["id"]
        self._sessions = list_sessions_for_event(self._selected_event_id)
        self._session_table.setRowCount(len(self._sessions))
        for i, s in enumerate(self._sessions):
            self._session_table.setItem(i, 0, QTableWidgetItem(s["filename"]))
            self._session_table.setItem(i, 1, QTableWidgetItem(s["date"] or ""))
            self._session_table.setItem(i, 2, QTableWidgetItem(s["time"] or ""))
            self._session_table.setItem(i, 3, QTableWidgetItem(s["laps"] or ""))
            self._session_table.setItem(i, 4, QTableWidgetItem(s["track"] or ""))
            blt = s.get("best_lap_time")
            blt_str = f"{blt:.3f}s" if blt else ""
            self._session_table.setItem(i, 5, QTableWidgetItem(blt_str))
            self._session_table.setItem(i, 6, QTableWidgetItem(s["driver"] or ""))
            self._session_table.setItem(i, 7, QTableWidgetItem(s.get("source_type") or ""))

    def _on_session_double_click(self, index):
        row = index.row()
        if 0 <= row < len(self._sessions):
            self.session_opened.emit(self._sessions[row]["id"])

    def _on_session_context_menu(self, pos):
        row = self._session_table.rowAt(pos.y())
        if row < 0 or row >= len(self._sessions):
            return
        menu = QMenu(self)
        delete_action = menu.addAction("Delete")
        action = menu.exec(self._session_table.viewport().mapToGlobal(pos))
        if action == delete_action:
            session = self._sessions[row]
            reply = QMessageBox.question(
                self, "Delete Session",
                f"Delete '{session['filename']}'?",
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                delete_session(session["id"])
                self.refresh()

    def _on_delete_event(self):
        row = self._event_table.currentRow()
        if row < 0 or row >= len(self._events):
            return
        event = self._events[row]
        reply = QMessageBox.question(
            self, "Delete Event",
            f"Delete event '{event['name']}' and all its sessions?",
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            delete_event(event["id"])
            self._selected_event_id = ""
            self.refresh()

    def _on_add_event(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QDateEdit, QFormLayout
        from PySide6.QtCore import QDate
        dlg = QDialog(self)
        dlg.setWindowTitle("New Event")
        layout = QFormLayout(dlg)
        name_input = QLineEdit()
        layout.addRow("Name:", name_input)
        date_edit = QDateEdit(QDate.currentDate())
        date_edit.setCalendarPopup(True)
        date_edit.setDisplayFormat("yyyy-MM-dd")
        layout.addRow("Date:", date_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addRow(buttons)
        if dlg.exec() and name_input.text().strip():
            eid = create_event(name_input.text().strip(), date_edit.date().toString("yyyy-MM-dd"))
            self._selected_event_id = eid
            self.refresh()

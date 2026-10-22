"""Anonymous personal totals and an explicitly refreshed public leaderboard."""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHeaderView, QHBoxLayout, QInputDialog,
                              QLabel, QLineEdit, QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget)

from mnmparse.app.widgets import FlowLayout
COLUMNS = (("alias", "Anonymous name"), ("max_health", "Max HP"), ("max_mana", "Max Mana"),
           ("personal_kills", "Kills"), ("personal_deaths", "Deaths"), ("high_hit", "Highest hit"),
           ("coin_looted", "Coin (copper)"), ("captured_at", "Captured (UTC)"))


class LeaderboardModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.records = []
        self._column, self._order = 1, Qt.SortOrder.DescendingOrder

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][1]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.records[index.row()].get(COLUMNS[index.column()][0])
        if role == Qt.ItemDataRole.DisplayRole:
            if value is None:
                return "—"
            if type(value) is int:
                return f"{value:,}"
            return str(value).replace("T", " ").removesuffix("Z") if index.column() == 7 else str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole and 1 <= index.column() <= 6:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def set_records(self, records):
        self.beginResetModel()
        self.records = list(records)
        self._sort()
        self.endResetModel()

    def _sort(self):
        key = COLUMNS[self._column][0]
        # Missing Mana remains last in either direction; UTC ISO timestamps sort chronologically.
        present = [entry for entry in self.records if entry.get(key) is not None]
        absent = [entry for entry in self.records if entry.get(key) is None]
        present.sort(key=lambda entry: entry[key], reverse=self._order == Qt.SortOrder.DescendingOrder)
        self.records = present + absent

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.layoutAboutToBeChanged.emit()
        self._column, self._order = column, order
        self._sort()
        self.layoutChanged.emit()


class LeaderboardPage(QWidget):
    def __init__(self, engine, cfg, settings, parent=None):
        super().__init__(parent)
        self.engine, self.ledger, self.client = engine, None, None
        self._demo = self._loaded = False
        self._status = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        title = QLabel("Leaderboard")
        title.setObjectName("Heading")
        root.addWidget(title)
        intro = QLabel("Share an anonymous daily snapshot of your mini stats and lifetime personal totals. "
                       "Copy stays on your clipboard. Submit publishes to the public leaderboard.")
        intro.setWordWrap(True)
        root.addWidget(intro)
        identity = QHBoxLayout()
        self.alias = QLabel("Anonymous name unavailable")
        self.alias.setObjectName("Heading")
        identity.addWidget(self.alias, 1)
        self.phrase_button = QPushButton("Set secret phrase…")
        self.phrase_button.setObjectName("Chip")
        self.phrase_button.clicked.connect(self.set_phrase)
        identity.addWidget(self.phrase_button)
        root.addLayout(identity)
        note = QLabel("An optional secret phrase restores the same three-word name. Keep it private; changing identities starts new local totals.")
        note.setWordWrap(True)
        root.addWidget(note)
        self.totals = {}
        totals = FlowLayout(h_spacing=16, v_spacing=8)
        for field, text in COLUMNS[1:7]:
            label = QLabel(text + ": —")
            self.totals[field] = label
            totals.addWidget(label)
        root.addLayout(totals)
        controls = QHBoxLayout()
        self.copy_button = QPushButton("Copy")
        self.submit_button = QPushButton("Submit")
        self.refresh_button = QPushButton("Refresh public entries")
        for button in (self.copy_button, self.submit_button, self.refresh_button):
            button.setObjectName("Chip")
            controls.addWidget(button)
        controls.addStretch(1)
        root.addLayout(controls)
        self.copy_button.clicked.connect(self.copy_stats)
        self.submit_button.clicked.connect(self.submit_stats)
        self.refresh_button.clicked.connect(self.refresh_public)
        self.eligibility = QLabel()
        self.eligibility.setWordWrap(True)
        root.addWidget(self.eligibility)
        self.message = QLabel("Refresh public entries to check the leaderboard.")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        self.model = LeaderboardModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_status)
        self.refresh_status()

    def bind(self, ledger, client):
        self.ledger, self.client = ledger, client
        client.records_updated.connect(self._records)
        client.busy_changed.connect(self._busy_changed)
        client.finished.connect(self.message.setText)
        client.submitted.connect(self._submitted)
        self.refresh_status()
        if self.isVisible() and not self._loaded:
            self._load_cached()

    def set_demo_active(self, active):
        self._demo = bool(active)
        self.refresh_status()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_status()
        self._timer.start()
        if self.client is not None and not self._loaded:
            self._load_cached()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def refresh_status(self):
        method = getattr(self.engine, "leaderboard_status", None)
        self._status = method() if callable(method) and self.ledger is not None else {}
        snapshot = getattr(self.engine, "player_stat_snapshot", None)
        stats = snapshot() if callable(snapshot) and self.ledger is not None and not self._demo else None
        self.alias.setText(self._status.get("alias", "Anonymous name unavailable"))
        for field, label in self.totals.items():
            value = (stats or {}).get(field) if field in {"max_health", "max_mana"} else self._status.get(field)
            title = dict(COLUMNS)[field]
            label.setText(f"{title}: {value:,}" if type(value) is int else title + ": —")
        reason = self.action_reason()
        copy_reason = self.action_reason(copy=True)
        service = self.client.submission_unavailable if self.client else "Leaderboard is unavailable in this mode."
        self.eligibility.setText((reason + (" Copy is available locally." if not copy_reason else "")) if reason else ("Already submitted today (UTC). Copy remains available." if self.submitted_today()
                                 else "Ready to copy. " + service if service else "Ready to copy or submit once per UTC day."))
        busy = self.client is not None and self.client.busy
        self.copy_button.setEnabled(not copy_reason)
        self.copy_button.setToolTip(copy_reason or "Copy locally without submitting.")
        self.submit_button.setEnabled(not reason and not service and not busy and not self.submitted_today())
        self.submit_button.setToolTip(reason or service or ("Already submitted today (UTC)." if self.submitted_today() else "Publish this anonymous snapshot."))
        self.phrase_button.setEnabled(self.ledger is not None and not busy and not self._demo)
        self.refresh_button.setEnabled(self.client is not None and not busy and not self._demo)

    def action_reason(self, *, copy=False):
        if self._demo or self.ledger is None:
            return "Leaderboard actions are unavailable in demo and self-test modes."
        if self._status.get("error"):
            return "Stats history is unavailable."
        if copy:
            return "" if self._status.get("copy_eligible") else "Read fresh mini stats before copying."
        if not self._status.get("eligible"):
            return self._status.get("reason") or "Read fresh mini stats using Window Capture to submit. Desktop capture is not eligible."
        return ""

    def submitted_today(self):
        return self._status.get("submitted_day") == datetime.now(timezone.utc).date().isoformat()

    def _snapshot(self):
        self.refresh_status()
        if self.action_reason():
            raise ValueError(self.action_reason())
        return self.engine.leaderboard_submission()

    def copy_stats(self):
        try:
            self.refresh_status()
            if self.action_reason(copy=True):
                raise ValueError
            text = self.engine.leaderboard_copy_text()
        except ValueError:
            self.message.setText("Read fresh mini stats before copying.")
            return ""
        QApplication.clipboard().setText(text)
        self.message.setText("Copied locally. Nothing was submitted.")
        return text

    def submit_stats(self):
        self._restore_history()
        try:
            record = self._snapshot()
        except ValueError:
            self.message.setText("Read fresh mini stats using Window Capture before submitting.")
            return False
        if self.submitted_today():
            self.message.setText("Already submitted today. You can submit again on the next UTC day.")
            return False
        if self.client is None or not self.client.submit(record):
            return False
        self.message.setText("Submitting anonymous snapshot…")
        return True

    def refresh_public(self):
        if self.client is not None and not self._demo and self.client.refresh():
            self._loaded = True
            self.message.setText("Loading public entries; cached entries appear first…")

    def _load_cached(self):
        if self.client.load_cached():
            self._loaded = True

    def _records(self, records, message):
        self.model.set_records(records)
        self._restore_history()
        self.message.setText(f"{message} · {len(records):,} daily snapshots")

    def _restore_history(self):
        restore = getattr(self.ledger, "restore_history", None)
        if callable(restore):
            try:
                restore(self.model.records)
            except (ValueError, OSError):
                self.message.setText("Stats history is unavailable.")
        self.refresh_status()

    def _busy_changed(self, _busy):
        self.refresh_status()

    def _submitted(self, record):
        try:
            self.ledger.mark_submitted(record)
        except (ValueError, OSError):
            self.message.setText("Submission was accepted, but local history could not be updated.")
        self.refresh_status()

    def set_phrase(self):
        if self.ledger is None or self._demo or self.client and self.client.busy:
            return
        phrase, accepted = QInputDialog.getText(self, "Anonymous identity", "Secret phrase (8–1024 characters):",
                                               QLineEdit.EchoMode.Password)
        if not accepted:
            return
        answer = QMessageBox.question(self, "Anonymous identity", "Use this identity?\n\n"
                                      "Changing to a different phrase starts new local personal totals.",
                                      QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        try:
            self.ledger.set_phrase(phrase)
        except (ValueError, OSError):
            self.message.setText("Could not set identity. Use a secret phrase of 8–1024 characters.")
        finally:
            phrase = ""
        self._restore_history()

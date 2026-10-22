"""Anonymous personal totals and an explicitly refreshed public leaderboard."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRect, QSize, Qt, QTextBoundaryFinder, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QHBoxLayout, QInputDialog,
                              QLabel, QLineEdit, QListView, QMessageBox, QPushButton, QScrollArea,
                              QStyle, QStyledItemDelegate, QTabWidget, QVBoxLayout, QWidget)

from mnmparse.app import theme
from mnmparse.app.widgets import FlowLayout
from mnmparse.leaderboard_messages import MAX_MESSAGE_CHARS, normalize_message
COLUMNS = (("alias", "Anonymous name"), ("max_health", "Max HP"), ("max_mana", "Max Mana"),
           ("personal_kills", "Kills"), ("personal_deaths", "Deaths"), ("high_hit", "Highest hit"),
           ("coin_looted", "Coin (copper)"), ("captured_at", "Captured (UTC)"),
           ("strength", "STR"), ("stamina", "STA"), ("agility", "AGI"), ("dexterity", "DEX"),
           ("intelligence", "INT"), ("wisdom", "WIS"), ("charisma", "CHA"), ("armor_class", "AC"),
           ("resist_cold", "Cold"), ("resist_corruption", "Corruption"), ("resist_disease", "Disease"),
           ("resist_electricity", "Electricity"), ("resist_fire", "Fire"), ("resist_holy", "Holy"),
           ("resist_magic", "Magic"), ("resist_poison", "Poison"),
           ("stat_total", "Stat Total"), ("resist_total", "Resist Total"))
LABELS = dict(COLUMNS)
CARD_LABELS = {**LABELS, "max_health": "HP", "max_mana": "Mana", "coin_looted": "Copper",
               "resist_corruption": "Corr", "resist_disease": "Dis", "resist_electricity": "Elec",
               "resist_magic": "Mag", "resist_poison": "Poi"}
RECORD_ROWS = (
    ("max_health", "max_mana", "armor_class", "personal_kills", "personal_deaths", "high_hit", "coin_looted"),
    ("strength", "stamina", "agility", "dexterity", "intelligence", "wisdom", "charisma", "stat_total"),
    ("resist_cold", "resist_corruption", "resist_disease", "resist_electricity", "resist_fire", "resist_holy",
     "resist_magic", "resist_poison", "resist_total"),
)
RECORD_ROLE = Qt.ItemDataRole.UserRole + 1
FULL_LABELS = {**LABELS, "strength": "Strength", "stamina": "Stamina", "agility": "Agility",
               "dexterity": "Dexterity", "intelligence": "Intelligence", "wisdom": "Wisdom",
               "charisma": "Charisma", "armor_class": "Armor class"}


def _value(value):
    return f"{value:,}" if type(value) is int else "—"


def _record_key(record):
    return (record.get("public_key", ""), record.get("submission_id", ""), record.get("captured_at", ""))


def record_lines(record):
    """Three accessible lines; missing older fields remain unavailable."""
    alias = record.get("alias", "Anonymous name unavailable")
    return tuple(alias + " · " + " · ".join(f"{LABELS[field]}: {_value(record.get(field))}" for field in row)
                 for row in RECORD_ROWS)


def record_tooltip(record):
    stamp = record.get("captured_at")
    title = "Personal bests as of (UTC): " if record.get("schema_version") == 3 else "Captured (UTC): "
    timestamp = title + stamp.replace("T", " ").removesuffix("Z") if stamp else "Snapshot time unavailable"
    message = record.get("message")
    if not message:
        return "<qt>" + escape(timestamp) + "</qt>"
    # Add display-only wrap opportunities without breaking emoji/combining clusters.
    # Qt otherwise lets a single 160-character word expand beyond the screen.
    encoded = message.encode("utf-16-le")
    boundaries = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, message)
    parts, start, run = [], 0, 0
    while (end := boundaries.toNextBoundary()) != -1:
        cluster = encoded[start * 2:end * 2].decode("utf-16-le")
        parts.append(escape(cluster))
        run = 0 if cluster.isspace() else run + 1
        if run == 12:
            parts.append("\u200b")
            run = 0
        start = end
    # Qt's rich-text subset renders a small speech bubble; user text stays inert.
    return (f'<qt><table width="300" cellspacing="0" cellpadding="0"><tr><td '
            f'style="padding:8px; background-color:{theme.BG1}; border:1px solid {theme.ACCENT};">'
            + "".join(parts) + '</td></tr><tr><td style="padding-left:12px; color:' + theme.ACCENT
            + ';">&#9662;</td></tr></table><span style="color:' + theme.MUTED + ';">'
            + escape(timestamp) + '</span></qt>')


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
        if not index.isValid() or not 0 <= index.row() < len(self.records) or not 0 <= index.column() < len(COLUMNS):
            return None
        record = self.records[index.row()]
        value = record.get(COLUMNS[index.column()][0])
        if role == RECORD_ROLE:
            return record
        if role == Qt.ItemDataRole.ToolTipRole:
            return record_tooltip(record)
        if role in (Qt.ItemDataRole.AccessibleTextRole, Qt.ItemDataRole.AccessibleDescriptionRole):
            return "\n".join(record_lines(record))
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
        # A deterministic tie order keeps complete cards together in either direction.
        self.records.sort(key=_record_key)
        present = [entry for entry in self.records if entry.get(key) is not None]
        absent = [entry for entry in self.records if entry.get(key) is None]
        present.sort(key=lambda entry: entry[key], reverse=self._order == Qt.SortOrder.DescendingOrder)
        self.records = present + absent

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if not 0 <= column < len(COLUMNS):
            return
        old_indexes = self.persistentIndexList()
        old_records = [self.records[index.row()] for index in old_indexes]
        self.layoutAboutToBeChanged.emit()
        self._column, self._order = column, order
        self._sort()
        positions = {id(record): row for row, record in enumerate(self.records)}
        self.changePersistentIndexList(old_indexes, [self.index(positions[id(record)], index.column())
                                                   for record, index in zip(old_records, old_indexes)])
        self.layoutChanged.emit()


class LeaderboardCardDelegate(QStyledItemDelegate):
    """One selectable record, painted as exactly three lines with repeated aliases."""

    def _layout(self, record, font):
        metrics = QFontMetrics(font)
        bold = QFont(font)
        bold.setBold(True)
        alias_width = QFontMetrics(bold).horizontalAdvance(record.get("alias", "Anonymous name unavailable")) + 24
        lines = tuple(tuple(f"{CARD_LABELS[field]}: {_value(record.get(field))}" for field in row) for row in RECORD_ROWS)
        # Each line grows naturally; large values remain reachable by horizontal scrolling.
        width = alias_width + max(sum(metrics.horizontalAdvance(text) + 18 for text in row) for row in lines) + 24
        return bold, alias_width, lines, max(26, metrics.lineSpacing() + 10), width

    def sizeHint(self, option, index):
        _bold, _alias_width, _lines, line_height, width = self._layout(index.data(RECORD_ROLE) or {}, option.font)
        return QSize(width, line_height * 3 + 16)

    def paint(self, painter, option, index):
        record = index.data(RECORD_ROLE) or {}
        bold, alias_width, lines, line_height, _width = self._layout(record, option.font)
        painter.save()
        painter.setClipRect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.fillRect(option.rect, theme.qcolor(theme.ACCENT, .12) if selected else QColor(theme.BG1))
        painter.setPen(QPen(theme.qcolor(theme.ACCENT, .65) if option.state & QStyle.StateFlag.State_HasFocus
                            else theme.qcolor(theme.TEXT, .12), 1))
        painter.drawRect(option.rect.adjusted(0, 0, -1, -1))
        x, top = option.rect.left() + 12, option.rect.top() + 8
        metrics = QFontMetrics(option.font)
        for line, values in enumerate(lines):
            y = top + line * line_height
            painter.setFont(bold)
            painter.setPen(QColor(theme.ACCENT))
            painter.drawText(QRect(x, y, alias_width - 12, line_height), Qt.AlignmentFlag.AlignVCenter,
                             record.get("alias", "Anonymous name unavailable"))
            painter.setFont(option.font)
            painter.setPen(QColor(theme.TEXT))
            cursor = x + alias_width
            for text in values:
                width = metrics.horizontalAdvance(text) + 18
                painter.drawText(QRect(cursor, y, width, line_height), Qt.AlignmentFlag.AlignVCenter, text)
                cursor += width
        painter.restore()


class LeaderboardPage(QWidget):
    def __init__(self, engine, cfg, settings, parent=None):
        super().__init__(parent)
        self.engine, self.ledger, self.client = engine, None, None
        self._demo = self._loaded = False
        self._status = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(8)
        title = QLabel("Leaderboard")
        title.setObjectName("Heading")
        root.addWidget(title)
        intro = QLabel("Copy your current mini stats locally. Submit your personal best stats and lifetime personal "
                       "totals to the anonymous public leaderboard once per UTC day.")
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
        self.local_tabs = QTabWidget()
        fresh, self.totals = self._summary()
        best, self.best_totals = self._summary()
        self.local_tabs.addTab(fresh, "Fresh reading")
        self.local_tabs.addTab(best, "Local best")
        self.local_tabs.setCurrentIndex(1)
        self.local_tabs.setTabToolTip(0, "Current mini stats and lifetime personal counters. Copy uses this reading.")
        self.local_tabs.setTabToolTip(1, "Submit shares these personal bests. Totals use one complete reading, not a sum of individual records.")
        self.local_tabs.setMinimumHeight(100)
        self.local_tabs.setMaximumHeight(145)
        root.addWidget(self.local_tabs)
        message_row = QHBoxLayout()
        message_label = QLabel("Message (optional)")
        self.message_input = QLineEdit()
        self.message_input.setAccessibleName("Message (optional)")
        self.message_input.setPlaceholderText("Share a short message…")
        self.message_input.setToolTip("Included only when you Submit. A submitted daily snapshot cannot be edited.")
        # QLineEdit counts UTF-16 units; the protocol counts Unicode code points.
        self.message_input.setMaxLength(MAX_MESSAGE_CHARS * 2)
        self.message_input.setClearButtonEnabled(True)
        message_label.setBuddy(self.message_input)
        message_row.addWidget(message_label)
        message_row.addWidget(self.message_input, 1)
        root.addLayout(message_row)
        controls = QHBoxLayout()
        self.copy_button = QPushButton("Copy current")
        self.submit_button = QPushButton("Submit personal bests")
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
        sort_controls = QHBoxLayout()
        sort_controls.addWidget(QLabel("Sort by"))
        self.sort_field = QComboBox()
        self.sort_field.setAccessibleName("Leaderboard sort field")
        for column, (field, label) in enumerate(COLUMNS):
            if field != "captured_at":
                self.sort_field.addItem(FULL_LABELS[field], column)
        self.sort_field.setCurrentIndex(self.sort_field.findData(1))
        self.sort_order = QComboBox()
        self.sort_order.setAccessibleName("Leaderboard sort direction")
        self.sort_order.addItem("Descending", Qt.SortOrder.DescendingOrder)
        self.sort_order.addItem("Ascending", Qt.SortOrder.AscendingOrder)
        sort_controls.addWidget(self.sort_field)
        sort_controls.addWidget(self.sort_order)
        sort_controls.addStretch(1)
        root.addLayout(sort_controls)
        self.model = LeaderboardModel(self)
        self.table = QListView()
        self.table.setAccessibleName("Public leaderboard snapshots")
        self.table.setModel(self.model)
        self.table.setModelColumn(0)
        self.table.setItemDelegate(LeaderboardCardDelegate(self.table))
        self.table.setSpacing(4)
        self.table.setResizeMode(QListView.ResizeMode.Adjust)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sort_field.currentIndexChanged.connect(self._sort_public)
        self.sort_order.currentIndexChanged.connect(self._sort_public)
        root.addWidget(self.table, 1)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_status)
        self.refresh_status()

    def _summary(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(5)
        labels = {}
        for row in RECORD_ROWS:
            flow = FlowLayout(h_spacing=14, v_spacing=5)
            for field in row:
                label = QLabel(LABELS[field] + ": —")
                label.setAccessibleName(FULL_LABELS[field])
                labels[field] = label
                flow.addWidget(label)
            layout.addLayout(flow)
        layout.addStretch(1)
        scroll.setWidget(body)
        return scroll, labels

    def _sort_public(self):
        self.model.sort(self.sort_field.currentData(), self.sort_order.currentData())

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
        self.alias.setStyleSheet("color: " + theme.ACCENT + "; font-weight: bold;")
        for field, label in self.totals.items():
            value = self._status.get(field) if field in {"personal_kills", "personal_deaths", "high_hit", "coin_looted"} else (stats or {}).get(field)
            label.setText(f"{LABELS[field]}: {_value(value)}")
            label.setToolTip(FULL_LABELS[field] + (" · Captured (UTC): " + stats["captured_at"] if stats and stats.get("captured_at") else ""))
        records = self._status.get("records") or {}
        for field, label in self.best_totals.items():
            entry = records.get(field) or {}
            value = self._status.get(field) if field in {"personal_kills", "personal_deaths", "high_hit", "coin_looted"} else entry.get("value")
            label.setText(f"{LABELS[field]}: {_value(value)}")
            label.setToolTip(FULL_LABELS[field] + (" · Recorded (UTC): " + entry["captured_at"] if entry.get("captured_at") else ""))
        reason = self.action_reason()
        copy_reason = self.action_reason(copy=True)
        service = self.client.submission_unavailable if self.client else "Leaderboard is unavailable in this mode."
        self.eligibility.setText((reason + (" Copy current is available locally." if not copy_reason else "")) if reason else ("Already submitted today (UTC). Copy current remains available." if self.submitted_today()
                                 else "Ready to copy current values. " + service if service else "Ready to copy current values or submit personal bests once per UTC day."))
        busy = self.client is not None and self.client.busy
        self.copy_button.setEnabled(not copy_reason)
        self.copy_button.setToolTip(copy_reason or "Copy current mini stats locally without submitting.")
        self.submit_button.setEnabled(not reason and not service and not busy and not self.submitted_today())
        self.submit_button.setToolTip(reason or service or ("Already submitted today (UTC)." if self.submitted_today() else "Publish your personal best stats and lifetime totals. Requires fresh Window Capture."))
        self.phrase_button.setEnabled(self.ledger is not None and not busy and not self._demo)
        self.message_input.setEnabled(self.ledger is not None and not busy and not self._demo)
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

    def _snapshot(self, message=""):
        self.refresh_status()
        if self.action_reason():
            raise ValueError(self.action_reason())
        return self.engine.leaderboard_submission(message=message) if message else self.engine.leaderboard_submission()

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
        self.message.setText("Copied current values locally. Nothing was submitted.")
        return text

    def submit_stats(self):
        self._restore_history()
        try:
            message = normalize_message(self.message_input.text())
        except ValueError:
            self.message.setText(f"Use a message of up to {MAX_MESSAGE_CHARS} characters.")
            return False
        self.message_input.setText(message)
        try:
            record = self._snapshot(message)
        except ValueError:
            self.message.setText("Read fresh mini stats using Window Capture before submitting.")
            return False
        if self.submitted_today():
            self.message.setText("Already submitted today. You can submit again on the next UTC day.")
            return False
        if self.client is None or not self.client.submit(record):
            return False
        self.message.setText("Submitting personal bests…")
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

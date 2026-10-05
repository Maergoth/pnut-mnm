"""The Triggers page: match chat text, then play a cue, speak, and/or start a timer.

Left: every trigger (tick to enable, search, New / Duplicate / Delete, Import / Export).
Right: the selected trigger's editor.  Edits are saved to ``triggers.json`` as you type.
The pattern box completes from recent chat lines and from the names and abilities seen so
far; the Test box shows at once whether a line would match.  The audio settings (master
volume, voice, speech rate, output device) apply to every trigger.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QSettings, QSize, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCompleter,
    QComboBox,
    QColorDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mnmparse.app.pages import _label, _page_qss, _panel
from mnmparse.app.widgets import ElidedLabel, SliderRow, ToggleSwitch, token
from mnmparse.config import Config, project_path
from mnmparse.trigger_exchange import export_trigger_file, external_sound_files, merge_triggers, read_trigger_file
from mnmparse.triggers import BUILTIN_SOUNDS, MODES, Trigger, fill_placeholders, match_trigger

if TYPE_CHECKING:
    from mnmparse.app.engine import Engine
    from mnmparse.app.triggers_runtime import TriggerRunner
    from mnmparse.trigger_chat import ReceivedShare
    from mnmparse.app.trigger_share_dialog import TriggerChatExportDialog, TriggerSharePrompt

log = logging.getLogger(__name__)

__all__ = ["TriggersPage"]

MODE_TITLES = {"contains": "Contains", "starts": "Starts with", "exact": "Whole line", "regex": "Regular expression"}
ACTION_TITLES = {"none": "Nothing", "sound": "Built-in sound", "file": "Sound file", "speak": "Speak text"}
ALERT_TITLES = {"none": "Nothing", "sound": "Built-in sound", "speak": "Speak text"}
TIMER_MODE_TITLES = {"replace": "Replace", "retain": "Retain", "stack": "Add another timer"}
TIMER_COLOR_TOKENS = {"timer_color": "SUCCESS", "timer_warn_color": "ACCENT", "timer_low_color": "DANGER"}
RECENT_LINES = 500
SAVE_DELAY_MS = 400
MAX_PENDING_SHARES = 20


def _combo(items: dict[str, str], width: int = 200) -> QComboBox:
    combo = QComboBox()
    for key, title in items.items():
        combo.addItem(title, key)
    combo.setMinimumWidth(width)
    return combo


def _select(combo: QComboBox, key: str) -> None:
    index = combo.findData(key)
    combo.setCurrentIndex(index if index >= 0 else 0)


class _FitScroll(QScrollArea):
    """A vertical scroll area that is never narrower than its content's minimum width, so
    the window's minimum size keeps the editor rows from being squeezed."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        hint = super().minimumSizeHint()
        content = self.widget()
        if content is not None:
            need = content.minimumSizeHint().width() + self.verticalScrollBar().sizeHint().width() + 2 * self.frameWidth()
            hint.setWidth(max(hint.width(), need))
        return hint


class TriggersPage(QWidget):
    """The trigger list and editor (see module docstring)."""

    chat_share_pending = Signal(str)

    def __init__(self, engine: "Engine", cfg: Config, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._cfg = cfg
        self._settings = settings
        self.runner: "TriggerRunner | None" = None
        self._current: Trigger | None = None
        self._loading = False
        self._recent: deque[str] = deque(maxlen=RECENT_LINES)
        self._pending_shares: deque[ReceivedShare] = deque()
        self._seen_shares: deque[tuple[str, str]] = deque(maxlen=128)
        self._chat_dialog: TriggerSharePrompt | None = None
        self._chat_export_dialog: TriggerChatExportDialog | None = None
        self.setStyleSheet(_page_qss())

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._save_now)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)
        outer.addWidget(self._build_audio_bar())

        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(12)
        split.addWidget(self._build_list())
        split.addWidget(self._build_editor())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([320, 760])
        outer.addWidget(split, 1)
        self._set_editor_enabled(False)

    # ================================================================ building
    def _build_audio_bar(self) -> QWidget:
        bar = _panel()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(14)
        lay.addWidget(_label("Triggers", "Heading"))
        self.volume = SliderRow("Volume", 0, 100, 80, formatter=lambda v: f"{v}%")
        self.volume.setMinimumWidth(160)
        self.volume.value_changed.connect(self._on_audio_changed)
        lay.addWidget(self.volume, 1)
        lay.addWidget(_label("Voice", "Muted"))
        self.voice = QComboBox()
        # long device / voice names must not widen the bar: show what fits, the list shows all
        self.voice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.voice.setMinimumContentsLength(14)
        self.voice.currentIndexChanged.connect(self._on_audio_changed)
        lay.addWidget(self.voice)
        lay.addWidget(_label("Speed", "Muted"))
        self.rate = QDoubleSpinBox()
        self.rate.setRange(-1.0, 1.0)
        self.rate.setSingleStep(0.1)
        self.rate.setDecimals(1)
        self.rate.setToolTip("Speech rate: -1 slow, 0 normal, 1 fast")
        self.rate.valueChanged.connect(self._on_audio_changed)
        lay.addWidget(self.rate)
        lay.addWidget(_label("Output", "Muted"))
        self.device = QComboBox()
        # long device / voice names must not widen the bar: show what fits, the list shows all
        self.device.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device.setMinimumContentsLength(14)
        self.device.currentIndexChanged.connect(self._on_audio_changed)
        lay.addWidget(self.device)
        return bar

    def _build_list(self) -> QWidget:
        panel = _panel()
        panel.setMinimumWidth(260)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search triggers…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refill_list)
        lay.addWidget(self.search)
        self.import_timers = QPushButton("Import timers…")
        self.import_timers.setObjectName("Chip")
        self.import_timers.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_timers.setToolTip("Add shared timers or triggers from a JSON file, including older PNUT files")
        self.import_timers.clicked.connect(self._on_import)
        lay.addWidget(self.import_timers)
        self.export_timers = QPushButton("Export timers…")
        self.export_timers.setObjectName("Chip")
        self.export_timers.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_timers.setToolTip("Share the selected timer or all timers and triggers")
        menu = QMenu(self.export_timers)
        self.export_selected = menu.addAction("Selected timer")
        self.export_selected.triggered.connect(lambda: self._on_export(selected=True))
        self.export_all = menu.addAction("All timers")
        self.export_all.triggered.connect(lambda: self._on_export())
        menu.addSeparator()
        self.export_chat = menu.addAction("Selected timer for game chat…")
        self.export_chat.triggered.connect(self._on_export_chat)
        self.export_timers.setMenu(menu)
        self.export_selected.setEnabled(False)
        self.export_all.setEnabled(False)
        self.export_chat.setEnabled(False)
        lay.addWidget(self.export_timers)
        self.sharing_status = QLabel()
        self.sharing_status.setTextFormat(Qt.TextFormat.PlainText)
        self.sharing_status.setWordWrap(True)
        self.sharing_status.hide()
        lay.addWidget(self.sharing_status)
        self.chat_notice = QPushButton("Review shared timer…")
        self.chat_notice.setObjectName("Chip")
        self.chat_notice.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_notice.clicked.connect(self.review_chat_shares)
        self.chat_notice.hide()
        lay.addWidget(self.chat_notice)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        # long patterns are elided (full text in the tooltip), never scrolled sideways
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setStyleSheet("QListWidget { background: transparent; } QListWidget::item { padding: 6px 4px; }")
        self.list.currentItemChanged.connect(self._on_select)
        self.list.itemChanged.connect(self._on_item_checked)
        lay.addWidget(self.list, 1)
        self.empty = _label("No triggers yet. Press New, type the chat text to watch for, and pick what happens.", "Muted")
        self.empty.setWordWrap(True)
        lay.addWidget(self.empty)
        row = QHBoxLayout()
        row.setSpacing(6)
        for text, slot, tip in (
            ("New", self._on_new, "Add a trigger"),
            ("Duplicate", self._on_duplicate, "Copy the selected trigger"),
            ("Delete", self._on_delete, "Delete the selected trigger"),
        ):
            b = QPushButton(text)
            b.setObjectName("Chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)
        starters = QPushButton("Restore starter triggers")
        starters.setObjectName("Chip")
        starters.setToolTip("Add missing Gatekick, Healkick and Invis Break starters; keep existing trigger settings")
        starters.clicked.connect(self._restore_presets)
        lay.addWidget(starters)
        return panel

    def _section(self, parent_layout: QVBoxLayout, title: str, hint: str = "") -> QFormLayout:
        panel = _panel()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)
        lay.addWidget(_label(title, "Heading"))
        if hint:
            h = _label(hint, "Muted")
            h.setWordWrap(True)
            lay.addWidget(h)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(8)
        lay.addLayout(form)
        parent_layout.addWidget(panel)
        return form

    @staticmethod
    def _hbox(*widgets: QWidget, stretch: bool = True) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        for w in widgets:
            lay.addWidget(w)
        if stretch:
            lay.addStretch(1)
        return box

    def _play_button(self, slot: Any, tip: str = "Play it now") -> QPushButton:
        b = QPushButton("▶")
        b.setObjectName("Chip")
        b.setFixedWidth(36)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def _build_editor(self) -> QWidget:
        scroll = _FitScroll()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(12)
        scroll.setWidget(content)
        self.editor = content

        # -- match --------------------------------------------------------------------
        form = self._section(
            lay, "When the chat says",
            "Type part of a chat line. Case, punctuation and spacing are ignored, and with "
            "“forgive OCR typos” each word may be a letter or two off. Regular expressions can "
            "capture text to speak back: (?P<who>\\w+) is spoken as {who}.",
        )
        self.name = QLineEdit()
        self.name.setPlaceholderText("Name shown in the list and on the timer")
        self.name.textEdited.connect(self._on_edit)
        form.addRow("Name", self.name)
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("e.g.  begins casting Mesmerize")
        self.pattern.textEdited.connect(self._on_edit)
        self._completer_model = QStringListModel(self)
        completer = QCompleter(self._completer_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(12)
        self.pattern.setCompleter(completer)
        form.addRow("Text", self.pattern)
        self.mode = _combo(MODE_TITLES)
        self.mode.currentIndexChanged.connect(self._on_edit)
        self.fuzzy = ToggleSwitch()
        self.fuzzy.toggled.connect(self._on_edit)
        form.addRow("Match", self._hbox(self.mode, QLabel("forgive OCR typos"), self.fuzzy))
        self.test_line = QLineEdit()
        self.test_line.setPlaceholderText("Paste a chat line to test the match")
        self.test_line.textChanged.connect(self._update_test)
        self.test_result = ElidedLabel("")
        self.test_result.setObjectName("Muted")
        form.addRow("Test", self.test_line)
        form.addRow("", self.test_result)

        # -- action -------------------------------------------------------------------
        form = self._section(lay, "Then", "Speech can say {line} (the whole line), {match} (the matched text) or a captured group.")
        self._then_form = form
        self.action = _combo(ACTION_TITLES)
        self.action.currentIndexChanged.connect(self._on_edit)
        form.addRow("Do", self.action)
        self.sound = QComboBox()
        self.sound.addItems(list(BUILTIN_SOUNDS))
        self.sound.currentIndexChanged.connect(self._on_edit)
        self.sound_row = self._hbox(self.sound, self._play_button(lambda: self._preview("sound")))
        form.addRow("Sound", self.sound_row)
        self.file = QLineEdit()
        self.file.setPlaceholderText("A .wav, .mp3 or .ogg file")
        self.file.textEdited.connect(self._on_edit)
        browse = QPushButton("Browse…")
        browse.setObjectName("Chip")
        browse.clicked.connect(self._browse_file)
        self.file_row = self._hbox(self.file, browse, self._play_button(lambda: self._preview("file")), stretch=False)
        form.addRow("File", self.file_row)
        self.speech = QLineEdit()
        self.speech.setPlaceholderText("e.g.  mez on {who}")
        self.speech.textEdited.connect(self._on_edit)
        self.speech_row = self._hbox(self.speech, self._play_button(lambda: self._preview("speak")), stretch=False)
        form.addRow("Say", self.speech_row)
        self.trigger_volume = SliderRow("", 0, 100, 80, formatter=lambda v: f"{v}%")
        self.trigger_volume.value_changed.connect(self._on_edit)
        form.addRow("Volume", self.trigger_volume)
        self.cooldown = QDoubleSpinBox()
        self.cooldown.setRange(0.0, 600.0)
        self.cooldown.setDecimals(1)
        self.cooldown.setSuffix(" s")
        self.cooldown.setToolTip("Ignore repeats of this trigger for this long (0: every time)")
        self.cooldown.valueChanged.connect(self._on_edit)
        form.addRow("Quiet for", self._hbox(self.cooldown, QLabel("after firing")))

        # -- timer --------------------------------------------------------------------
        form = self._section(
            lay, "Timer",
            "Optionally start a countdown on the overlay's timer panel (under the meter, above the "
            "auto-attack bar). It can warn before it ends and alert when it does.",
        )
        self.timer = ToggleSwitch()
        self.timer.toggled.connect(self._on_edit)
        form.addRow("Start a timer", self.timer)
        self.minutes = QSpinBox()
        self.minutes.setRange(0, 600)
        self.minutes.setSuffix(" min")
        self.seconds = QSpinBox()
        self.seconds.setRange(0, 59)
        self.seconds.setSuffix(" s")
        for spin in (self.minutes, self.seconds):
            spin.valueChanged.connect(self._on_edit)
        self.duration_row = self._hbox(self.minutes, self.seconds)
        form.addRow("Length", self.duration_row)
        self.timer_label = QLineEdit()
        self.timer_label.setPlaceholderText("Label (default: the trigger's name; {who} etc. allowed)")
        self.timer_label.textEdited.connect(self._on_edit)
        form.addRow("Label", self.timer_label)
        self.timer_mode = _combo(TIMER_MODE_TITLES)
        self.timer_mode.currentIndexChanged.connect(self._on_edit)
        self.timer_mode.setToolTip("Replace starts a fresh timer and cancels old speech. Retain ignores the entire "
                                   "trigger while its timer is running. Add another timer keeps both.")
        form.addRow("If already running", self.timer_mode)
        self.timer_colors: dict[str, QPushButton] = {}
        for field, title in (("timer_color", "Normal"), ("timer_warn_color", "Warning"), ("timer_low_color", "Low / ended")):
            button = QPushButton(title)
            button.setObjectName("Chip")
            button.setProperty("timerColor", "")
            button.setProperty("colorTitle", title)
            button.clicked.connect(lambda _checked=False, key=field: self._choose_timer_color(key))
            self.timer_colors[field] = button
        reset_colors = QPushButton("Reset")
        reset_colors.setObjectName("Chip")
        reset_colors.setToolTip("Restore green, amber and red timer colors")
        reset_colors.clicked.connect(self._reset_timer_colors)
        self.color_row = self._hbox(*self.timer_colors.values(), reset_colors)
        form.addRow("Colors", self.color_row)
        self.low_s = QDoubleSpinBox()
        self.low_s.setRange(0.0, 3600.0)
        self.low_s.setDecimals(1)
        self.low_s.setSuffix(" s remaining")
        self.low_s.setToolTip("Switch to the low color at this duration; 0 uses it only after the timer ends")
        self.low_s.valueChanged.connect(self._on_edit)
        form.addRow("Low duration", self._hbox(self.low_s))
        self.warn_s = QSpinBox()
        self.warn_s.setRange(0, 3600)
        self.warn_s.setSuffix(" s before")
        self.warn_s.setToolTip("0: no warning")
        self.warn_s.valueChanged.connect(self._on_edit)
        self.warn_action = _combo(ALERT_TITLES, 150)
        self.warn_action.currentIndexChanged.connect(self._on_edit)
        self.warn_sound = QComboBox()
        self.warn_sound.addItems(list(BUILTIN_SOUNDS))
        self.warn_sound.currentIndexChanged.connect(self._on_edit)
        self.warn_speech = QLineEdit()
        self.warn_speech.setPlaceholderText("{label} soon")
        self.warn_speech.textEdited.connect(self._on_edit)
        form.addRow("Warn", self._hbox(self.warn_s, self.warn_action, self.warn_sound, self.warn_speech, stretch=False))
        self.end_action = _combo(ALERT_TITLES, 150)
        self.end_action.currentIndexChanged.connect(self._on_edit)
        self.end_sound = QComboBox()
        self.end_sound.addItems(list(BUILTIN_SOUNDS))
        self.end_sound.currentIndexChanged.connect(self._on_edit)
        self.end_speech = QLineEdit()
        self.end_speech.setPlaceholderText("{label}")
        self.end_speech.textEdited.connect(self._on_edit)
        form.addRow("When it ends", self._hbox(self.end_action, self.end_sound, self.end_speech, stretch=False))
        test = QPushButton("Fire this trigger now")
        test.setObjectName("Chip")
        test.setCursor(Qt.CursorShape.PointingHandCursor)
        test.setToolTip("Play the action and start the timer as if the text had been read")
        test.clicked.connect(self._fire_now)
        form.addRow("", self._hbox(test))

        # -- recent -------------------------------------------------------------------
        panel = _panel()
        plo = QVBoxLayout(panel)
        plo.setContentsMargins(16, 12, 16, 12)
        plo.addWidget(_label("Recently fired", "Heading"))
        self.recent = QListWidget()
        self.recent.setFrameShape(QFrame.Shape.NoFrame)
        self.recent.setMinimumHeight(120)
        self.recent.setStyleSheet("QListWidget { background: transparent; }")
        plo.addWidget(self.recent)
        lay.addWidget(panel)
        lay.addStretch(1)
        return scroll

    # ================================================================ data in
    def set_runner(self, runner: "TriggerRunner") -> None:
        self.runner = runner
        runner.fired.connect(self._on_fired)
        store = runner.store
        self._loading = True
        try:
            self.volume.set_value(store.volume)
            self.voice.clear()
            self.voice.addItem("System default", "")
            for name in runner.audio.voices():
                self.voice.addItem(name, name)
            self.voice.setCurrentIndex(max(0, self.voice.findData(store.voice)))
            self.rate.setValue(store.rate)
            self.device.clear()
            self.device.addItem("System default", "")
            for name in runner.audio.devices():
                self.device.addItem(name, name)
            self.device.setCurrentIndex(max(0, self.device.findData(store.output_device)))
        finally:
            self._loading = False
        self._refill_list()

    def add_recent_line(self, text: str) -> None:
        """A chat line was read (feeds the pattern box's completion)."""
        text = (text or "").strip()
        if text and (not self._recent or self._recent[-1] != text):
            self._recent.append(text)

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_completions()

    def _refresh_completions(self) -> None:
        seen: dict[str, None] = {}
        for line in reversed(self._recent):
            seen.setdefault(line, None)
        try:
            from mnmparse.vocab import GLOBAL

            for cat in ("skill", "npc", "player", "item", "zone"):
                for name, _n in GLOBAL._names[cat].most_common(150):
                    seen.setdefault(GLOBAL.canonical(cat, name) or name, None)
        except Exception:  # noqa: BLE001 - completion is a nicety
            log.debug("vocabulary completions unavailable", exc_info=True)
        self._completer_model.setStringList(list(seen))

    # ================================================================ list
    def _store_triggers(self) -> list[Trigger]:
        return self.runner.store.triggers if self.runner is not None else []

    def _refill_list(self) -> None:
        needle = self.search.text().strip().lower()
        current_id = self._current.id if self._current is not None else None
        self.list.blockSignals(True)
        try:
            self.list.clear()
            select = None
            for trig in self._store_triggers():
                if needle and needle not in trig.name.lower() and needle not in trig.pattern.lower():
                    continue
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, trig.id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if trig.enabled else Qt.CheckState.Unchecked)
                self._fill_item(item, trig)
                self.list.addItem(item)
                if trig.id == current_id:
                    select = item
        finally:
            self.list.blockSignals(False)
        self.empty.setVisible(not self._store_triggers())
        self.export_all.setEnabled(bool(self._store_triggers()))
        self.export_selected.setEnabled(self._current is not None)
        self.export_chat.setEnabled(self._current is not None)
        if select is not None:
            self.list.setCurrentItem(select)
        elif self.list.count() and self._current is None:
            self.list.setCurrentRow(0)

    @staticmethod
    def _fill_item(item: QListWidgetItem, trig: Trigger) -> None:
        bits = [ACTION_TITLES.get(trig.action, trig.action).lower()]
        if trig.timer:
            m, s = divmod(int(trig.timer_seconds), 60)
            bits.append(f"timer {m}:{s:02d}")
        item.setText(f"{trig.name}\n    “{trig.pattern}”  ·  {', '.join(bits)}")
        item.setToolTip(f"{trig.name}\n{MODE_TITLES.get(trig.mode, trig.mode)}: {trig.pattern}")

    def _on_item_checked(self, item: QListWidgetItem) -> None:
        trig = self.runner.store.find(str(item.data(Qt.ItemDataRole.UserRole))) if self.runner else None
        if trig is not None:
            trig.enabled = item.checkState() == Qt.CheckState.Checked
            self._schedule_save()

    def _on_select(self, item: QListWidgetItem | None, _prev: Any = None) -> None:
        trig = self.runner.store.find(str(item.data(Qt.ItemDataRole.UserRole))) if (item and self.runner) else None
        self._current = trig
        self.export_selected.setEnabled(trig is not None)
        self.export_chat.setEnabled(trig is not None)
        self._load_editor(trig)

    def _on_new(self) -> None:
        if self.runner is None:
            return
        trig = Trigger(name=f"Trigger {len(self._store_triggers()) + 1}")
        self.runner.store.triggers.append(trig)
        self._current = trig
        self._refill_list()
        self._schedule_save()
        self.pattern.setFocus()

    def _on_duplicate(self) -> None:
        if self.runner is None or self._current is None:
            return
        copy = Trigger.from_dict({**self._current.to_dict(), "id": "", "name": f"{self._current.name} (copy)"})
        copy.id = Trigger().id
        self.runner.store.triggers.append(copy)
        self._current = copy
        self._refill_list()
        self._schedule_save()

    def _on_delete(self) -> None:
        if self.runner is None or self._current is None:
            return
        self.runner.store.triggers = [t for t in self.runner.store.triggers if t.id != self._current.id]
        self._current = None
        self._refill_list()
        self._load_editor(self._current)
        self._schedule_save()

    def _restore_presets(self) -> None:
        if self.runner is None:
            return
        self.runner.store.installed_presets.clear()
        self.runner.store.install_presets()
        self._refill_list()
        self._schedule_save()

    def _on_import(self) -> None:
        if self.runner is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import timers", str(project_path(".")), "Timers and triggers (*.json)")
        if not path:
            return
        try:
            incoming = read_trigger_file(path)
        except (OSError, ValueError) as exc:
            log.warning("import failed: %s", exc)
            self._sharing_feedback(f"Import failed: {exc}", error=True)
            return
        self._merge_incoming(incoming)

    def _merge_incoming(self, incoming: list[Trigger]) -> bool:
        """Validate and atomically persist either a file import or an accepted chat share."""
        if self.runner is None:
            self._sharing_feedback("Import failed: timers are unavailable.", error=True)
            return False
        try:
            result = merge_triggers(self.runner.store.triggers, incoming)
        except (OSError, ValueError) as exc:
            log.warning("import failed: %s", exc)
            self._sharing_feedback(f"Import failed: {exc}", error=True)
            return False
        if result.added:
            previous = self.runner.store.triggers
            pending_save = self._save_timer.isActive()
            self._save_timer.stop()
            self.runner.store.triggers = result.triggers
            try:
                self.runner.save()
            except (OSError, ValueError) as exc:
                self.runner.store.triggers = previous
                if pending_save:
                    self._schedule_save()
                log.warning("could not save imported timers: %s", exc)
                self._sharing_feedback(f"Import failed: could not save timers. {exc}", error=True)
                return False
            previous_ids = {trig.id for trig in previous}
            self._current = next((trig for trig in result.triggers if trig.id not in previous_ids), self._current)
            self.search.clear()
            self._refill_list()
        message = f"Imported {result.added}; skipped {result.skipped} duplicates."
        if result.conflicts:
            message += f" Conflicting versions kept as separate copies: {result.conflicts}."
        if external_sound_files(incoming):
            message += " Uses external sound files; check their locations in the editor."
        self._sharing_feedback(message)
        return True

    def _on_export(self, *, selected: bool = False) -> None:
        if self.runner is None:
            return
        triggers = [self._current] if selected and self._current is not None else self._store_triggers()
        if (selected and self._current is None) or not triggers:
            self._sharing_feedback("Select a timer to export." if selected else "There are no timers to export.", error=True)
            return
        filename = "PNUT-timer.json" if selected else "PNUT-timers.json"
        path, _ = QFileDialog.getSaveFileName(self, "Export timer" if selected else "Export all timers",
                                             str(project_path(filename)), "Timers and triggers (*.json)")
        if not path:
            return
        destination = Path(path)
        if not destination.suffix:
            destination = destination.with_suffix(".json")
        try:
            if self.runner.store.path is not None and destination.resolve() == self.runner.store.path.resolve():
                raise ValueError("Choose another filename; this is PNUT's active timer settings file.")
            export_trigger_file(destination, triggers)
        except (OSError, ValueError) as exc:
            log.warning("export failed: %s", exc)
            self._sharing_feedback(f"Export failed: {exc}", error=True)
            return
        message = f"Exported {len(triggers)} to {destination.name}."
        if external_sound_files(triggers):
            message += " Sound files are not included; share them separately and choose their locations after importing."
        self._sharing_feedback(message)

    def _on_export_chat(self) -> None:
        if self._current is None:
            self._sharing_feedback("Select a timer to share in game chat.", error=True)
            return
        from mnmparse.app.trigger_share_dialog import TriggerChatExportDialog
        from mnmparse.trigger_chat import encode_trigger

        try:
            code = encode_trigger(self._current)
        except ValueError as exc:
            self._sharing_feedback(f"Could not share this timer: {exc}", error=True)
            return
        if self._chat_export_dialog is not None:
            self._chat_export_dialog.close()
        dialog = TriggerChatExportDialog(self._current, code, self.window())
        self._chat_export_dialog = dialog
        dialog.finished.connect(lambda _result: self._finish_chat_export(dialog))
        dialog.show()

    def _finish_chat_export(self, dialog: TriggerChatExportDialog) -> None:
        if self._chat_export_dialog is dialog:
            self._chat_export_dialog = None
        dialog.deleteLater()

    def offer_chat_share(self, share: ReceivedShare) -> bool:
        """Queue a share without focusing a window; False means retry after the queue clears."""
        key = (share.sender.casefold(), share.share_id)
        if key in self._seen_shares or any((item.sender.casefold(), item.share_id) == key for item in self._pending_shares):
            return True
        try:
            result = merge_triggers(self._store_triggers(), [share.trigger])
        except ValueError:
            log.warning("ignoring invalid timer chat share", exc_info=True)
            return True
        if not result.added:
            self._seen_shares.append(key)
            return True
        if len(self._pending_shares) >= MAX_PENDING_SHARES:
            self._sharing_feedback("Shared timer queue is full. Review pending timers before receiving more.", error=True)
            return False
        self._seen_shares.append(key)
        self._pending_shares.append(share)
        self._update_chat_notice()
        return True

    def _update_chat_notice(self) -> None:
        count = len(self._pending_shares)
        self.chat_notice.setVisible(bool(count))
        if not count:
            self.chat_share_pending.emit("")
            return
        name = " ".join(self._pending_shares[0].trigger.name.split()) or "Unnamed timer"
        short_name = name[:77] + "…" if len(name) > 80 else name
        message = f'Shared timer “{short_name}” is ready to review.'
        if count > 1:
            message += f" {count} waiting."
        self.chat_notice.setText("Review shared timer…" if count == 1 else f"Review shared timers ({count})…")
        self.chat_notice.setToolTip(message)
        self.chat_share_pending.emit(message)

    def review_chat_shares(self) -> None:
        """Open a nonmodal review only in response to the user's Review action."""
        if self._chat_dialog is not None:
            self._chat_dialog.show()
            self._chat_dialog.raise_()
            self._chat_dialog.activateWindow()
            return
        if not self._pending_shares:
            return
        from mnmparse.app.trigger_share_dialog import TriggerSharePrompt

        share = self._pending_shares[0]
        dialog = TriggerSharePrompt(share.trigger, share.sender, self.window())
        self._chat_dialog = dialog
        dialog.import_requested.connect(lambda: self._import_chat_share(dialog, share))
        dialog.finished.connect(lambda _result: self._finish_chat_review(dialog, share))
        dialog.show()

    def _import_chat_share(self, dialog: TriggerSharePrompt, share: ReceivedShare) -> None:
        if self._merge_incoming([share.trigger]):
            dialog.accept()
        else:
            dialog.set_error(self.sharing_status.text())

    def _finish_chat_review(self, dialog: TriggerSharePrompt, share: ReceivedShare) -> None:
        if self._chat_dialog is dialog:
            self._chat_dialog = None
        if share in self._pending_shares:
            self._pending_shares.remove(share)
        self._update_chat_notice()
        dialog.deleteLater()

    def _sharing_feedback(self, message: str, *, error: bool = False) -> None:
        self.sharing_status.setText(message)
        self.sharing_status.setToolTip(message)
        self.sharing_status.setStyleSheet(f"color: {token('DANGER' if error else 'SUCCESS')};")
        self.sharing_status.show()

    # ================================================================ editor
    def _set_editor_enabled(self, on: bool) -> None:
        self.editor.setEnabled(on)

    def _load_editor(self, trig: Trigger | None) -> None:
        self._set_editor_enabled(trig is not None)
        if trig is None:
            return
        self._loading = True
        try:
            self.name.setText(trig.name)
            self.pattern.setText(trig.pattern)
            _select(self.mode, trig.mode)
            self.fuzzy.setChecked(trig.fuzzy)
            _select(self.action, trig.action)
            self.sound.setCurrentText(trig.sound)
            self.file.setText(trig.file)
            self.speech.setText(trig.speech)
            self.trigger_volume.set_value(trig.volume)
            self.cooldown.setMaximum(max(600.0, trig.cooldown_s))
            self.cooldown.setValue(trig.cooldown_s)
            self.timer.setChecked(trig.timer)
            m, s = divmod(int(round(trig.timer_seconds)), 60)
            self.minutes.setMaximum(max(600, m))
            self.minutes.setValue(m)
            self.seconds.setValue(s)
            self.timer_label.setText(trig.timer_label)
            _select(self.timer_mode, {"restart": "replace", "ignore": "retain"}.get(trig.timer_mode, trig.timer_mode))
            for field in self.timer_colors:
                self._set_timer_color(field, getattr(trig, field))
            self.low_s.setMaximum(max(3600.0, trig.timer_low_s))
            self.low_s.setValue(trig.timer_low_s)
            self.warn_s.setMaximum(max(3600, int(trig.timer_warn_s)))
            self.warn_s.setValue(int(trig.timer_warn_s))
            _select(self.warn_action, trig.timer_warn_action)
            self.warn_sound.setCurrentText(trig.timer_warn_sound)
            self.warn_speech.setText(trig.timer_warn_speech)
            _select(self.end_action, trig.timer_end_action)
            self.end_sound.setCurrentText(trig.timer_end_sound)
            self.end_speech.setText(trig.timer_end_speech)
        finally:
            self._loading = False
        self._update_visibility()
        self._update_test()

    def _on_edit(self, *_args: Any) -> None:
        if self._loading or self._current is None:
            return
        t = self._current
        t.name = self.name.text().strip() or "Trigger"
        t.pattern = self.pattern.text()
        t.mode = str(self.mode.currentData())
        t.fuzzy = self.fuzzy.isChecked()
        t.action = str(self.action.currentData())
        t.sound = self.sound.currentText()
        t.file = self.file.text().strip()
        t.speech = self.speech.text()
        t.volume = self.trigger_volume.value()
        t.cooldown_s = float(self.cooldown.value())
        t.timer = self.timer.isChecked()
        t.timer_seconds = max(1.0, float(self.minutes.value() * 60 + self.seconds.value()))
        t.timer_label = self.timer_label.text()
        t.timer_mode = str(self.timer_mode.currentData())
        for field, button in self.timer_colors.items():
            setattr(t, field, str(button.property("timerColor") or ""))
        t.timer_low_s = float(self.low_s.value())
        t.timer_warn_s = float(self.warn_s.value())
        t.timer_warn_action = str(self.warn_action.currentData())
        t.timer_warn_sound = self.warn_sound.currentText()
        t.timer_warn_speech = self.warn_speech.text()
        t.timer_end_action = str(self.end_action.currentData())
        t.timer_end_sound = self.end_sound.currentText()
        t.timer_end_speech = self.end_speech.text()
        item = self.list.currentItem()
        if item is not None:
            self.list.blockSignals(True)
            self._fill_item(item, t)
            self.list.blockSignals(False)
        self._update_visibility()
        self._update_test()
        self._schedule_save()

    def _update_visibility(self) -> None:
        action = str(self.action.currentData())
        # hide whole form rows (label too), not just the field
        self._then_form.setRowVisible(self.sound_row, action == "sound")
        self._then_form.setRowVisible(self.file_row, action == "file")
        self._then_form.setRowVisible(self.speech_row, action == "speak")
        self.trigger_volume.setEnabled(action != "none")
        on = self.timer.isChecked()
        for w in (self.duration_row, self.timer_label, self.timer_mode, self.color_row, self.low_s, self.warn_s, self.warn_action,
                  self.end_action):
            w.setEnabled(on)
        warn = str(self.warn_action.currentData())
        self.warn_sound.setVisible(warn == "sound")
        self.warn_speech.setVisible(warn == "speak")
        self.warn_sound.setEnabled(on and self.warn_s.value() > 0)
        self.warn_speech.setEnabled(on and self.warn_s.value() > 0)
        self.warn_action.setEnabled(on and self.warn_s.value() > 0)
        end = str(self.end_action.currentData())
        self.end_sound.setVisible(end == "sound")
        self.end_speech.setVisible(end == "speak")
        self.end_sound.setEnabled(on)
        self.end_speech.setEnabled(on)

    def _set_timer_color(self, field: str, color: str) -> None:
        button = self.timer_colors[field]
        button.setProperty("timerColor", color)
        resolved = color or token(TIMER_COLOR_TOKENS[field])
        button.setStyleSheet(f"QPushButton {{ border-bottom: 4px solid {resolved}; }}")
        button.setToolTip(f"{button.property('colorTitle')}: {resolved}" + (" (theme default)" if not color else ""))

    def _choose_timer_color(self, field: str) -> None:
        button = self.timer_colors[field]
        current = str(button.property("timerColor") or token(TIMER_COLOR_TOKENS[field]))
        color = QColorDialog.getColor(QColor(current), self, f"Timer color: {button.property('colorTitle')}")
        if color.isValid():
            self._set_timer_color(field, color.name())
            self._on_edit()

    def _reset_timer_colors(self) -> None:
        for field in self.timer_colors:
            self._set_timer_color(field, "")
        self.low_s.setValue(5.0)
        self._on_edit()

    def _update_test(self, *_args: Any) -> None:
        t = self._current
        line = self.test_line.text()
        if t is None:
            self.test_result.setText("")
            return
        problems = t.problems()
        if problems:
            self.test_result.setText("⚠ " + " ".join(problems))
            return
        if not line.strip():
            self.test_result.setText("Paste a line above to see whether it matches.")
            return
        probe = Trigger.from_dict({**t.to_dict(), "enabled": True})
        m = match_trigger(probe, line)
        if m is None:
            self.test_result.setText("✗ No match")
        else:
            said = fill_placeholders(t.speech, m.values()) if t.action == "speak" else ""
            extra = f"  ·  says “{said}”" if said else ""
            self.test_result.setText(f"✓ Matches “{m.text}”{extra}")

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a sound", str(project_path(".")),
                                              "Sounds (*.wav *.mp3 *.ogg *.flac *.m4a);;All files (*)")
        if path:
            self.file.setText(path)
            self._on_edit()

    def _preview(self, which: str) -> None:
        if self.runner is None or self._current is None:
            return
        t = self._current
        audio = self.runner.audio
        if which == "sound":
            audio.play_builtin(self.sound.currentText(), t.volume)
        elif which == "file":
            audio.play_file(self.file.text().strip(), t.volume)
        else:
            m = match_trigger(Trigger.from_dict({**t.to_dict(), "enabled": True}), self.test_line.text())
            values = m.values() if m else {"line": t.pattern, "match": t.pattern, "name": t.name}
            audio.speak(fill_placeholders(self.speech.text(), values), t.volume)

    def _fire_now(self) -> None:
        if self.runner is not None and self._current is not None:
            self.runner.test(self._current, self.test_line.text())

    # ================================================================ saving
    def _schedule_save(self) -> None:
        self._save_timer.start()

    def _save_now(self) -> None:
        if self.runner is not None:
            try:
                self.runner.save()
            except OSError as exc:
                log.warning("could not save the triggers: %s", exc)

    def _on_audio_changed(self, *_args: Any) -> None:
        if self._loading or self.runner is None:
            return
        store = self.runner.store
        store.volume = self.volume.value()
        store.voice = str(self.voice.currentData() or "")
        store.rate = float(self.rate.value())
        store.output_device = str(self.device.currentData() or "")
        self._schedule_save()

    def _on_fired(self, m: Any) -> None:
        stamp = time.strftime("%H:%M:%S")
        item = QListWidgetItem(f"{stamp}  {m.trigger.name}  ·  {m.line}")
        item.setToolTip(m.line)
        self.recent.insertItem(0, item)
        while self.recent.count() > 50:
            self.recent.takeItem(self.recent.count() - 1)

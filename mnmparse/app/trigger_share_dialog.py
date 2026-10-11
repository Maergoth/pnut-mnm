"""User-driven copying and review of timer definitions shared through game chat."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mnmparse.app import theme
from mnmparse.app.widgets import ElidedLabel, token
from mnmparse.app.window_identity import window_title
from mnmparse.trigger_exchange import MAX_FILE_BYTES, external_sound_files
from mnmparse.triggers import Trigger


class TriggerTextImportDialog(QDialog):
    """Paste definitions without changing the live store until Import succeeds."""

    import_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(window_title("trigger-text-import"))
        self.setObjectName("TimerTextImport")
        self.resize(680, 460)
        self.setMinimumSize(440, 340)
        self.setStyleSheet(f"""
            QDialog#TimerTextImport {{ background: {theme.BG0}; color: {theme.TEXT}; }}
            QDialog#TimerTextImport QLabel {{ color: {theme.TEXT}; background: transparent; }}
            QDialog#TimerTextImport QLabel#Heading {{ font-size: 22px; font-weight: 600; }}
            QDialog#TimerTextImport QPlainTextEdit {{
                background: {theme.BG1}; color: {theme.TEXT};
                border: 1px solid {theme.LINE}; border-radius: 6px; padding: 8px;
                selection-background-color: {theme.ACCENT}; selection-color: {theme.BG0};
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QLabel("Import timers", self)
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        hint = QLabel("Paste timer data or open a shared file, then choose Import.", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        sources = QHBoxLayout()
        self.from_clipboard = QPushButton("Paste from clipboard", self)
        self.from_clipboard.clicked.connect(self._paste)
        sources.addWidget(self.from_clipboard)
        self.open_file = QPushButton("Open file…", self)
        self.open_file.clicked.connect(self._open_file)
        sources.addWidget(self.open_file)
        sources.addStretch(1)
        layout.addLayout(sources)
        self.text = QPlainTextEdit(self)
        self.text.setPlaceholderText("Paste timer codes or JSON here")
        self.text.setAccessibleName("Timers to import")
        self.text.setTabChangesFocus(True)
        self.text.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.text, 1)
        self.error = QLabel(self)
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setStyleSheet(f"color: {token('DANGER')};")
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.import_button = buttons.addButton("Import", QDialogButtonBox.ButtonRole.ActionRole)
        self.import_button.setProperty("class", "primary")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(lambda: self.import_requested.emit(self.text.toPlainText()))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _paste(self) -> None:
        self.text.setPlainText(QApplication.clipboard().text())
        self.text.setFocus(Qt.FocusReason.OtherFocusReason)

    def _open_file(self) -> None:
        filename, _filter = QFileDialog.getOpenFileName(
            self, "Open timer data", "", "Timer data (*.pnut *.txt *.json);;All files (*)")
        if not filename:
            return
        try:
            with Path(filename).open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("The timer file is too large (maximum 8 MB).")
            text = raw.decode("utf-8-sig")
        except (OSError, ValueError) as exc:
            self.set_error(f"Could not open timer data: {exc}")
            return
        self.text.setPlainText(text)
        self.text.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_text_changed(self) -> None:
        self.import_button.setEnabled(bool(self.text.toPlainText().strip()))
        self.error.hide()

    def set_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()


class TriggerChatExportDialog(QDialog):
    """Copy one selected timer as one chat message; never type or send into the game."""

    def __init__(self, trigger: Trigger, code: str, parent: QWidget | None = None, *, cfg: object = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._trigger = trigger
        from mnmparse.trigger_chat import MAX_CHAT_LINE

        if (not isinstance(code, str) or not code or "\n" in code or "\r" in code
                or len(code) > MAX_CHAT_LINE):
            raise ValueError("A timer must be shared as one chat line of at most 255 characters.")
        self.code = code
        self.setWindowTitle(window_title("trigger-export"))
        self.setModal(False)
        self.resize(620, 380)
        self.setMinimumSize(440, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        self.title = ElidedLabel(trigger.name)
        self.title.setObjectName("Heading")
        self.title.setToolTip(trigger.name)
        layout.addWidget(self.title)
        hint = QLabel("Copy this timer and paste it into game chat as one message. "
                      "Recipients with PNUT capture running can review it before importing.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.line = QPlainTextEdit()
        self.line.setReadOnly(True)
        self.line.setMinimumHeight(80)
        self.line.setAccessibleName("Timer sharing chat line")
        self.line.setTabChangesFocus(True)
        self.line.setPlainText(self.code)
        layout.addWidget(self.line, 1)
        self.character_count = QLabel(f"{len(code)} characters · one chat message")
        self.character_count.setWordWrap(True)
        layout.addWidget(self.character_count)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setStyleSheet(f"color: {token('SUCCESS')};")
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.copy = buttons.addButton("Copy for game chat", QDialogButtonBox.ButtonRole.ActionRole)
        self.copy.clicked.connect(self._copy_line)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_config(self, cfg: object) -> None:
        self._cfg = cfg

    def _copy_line(self) -> None:
        if not self.code:
            return
        QApplication.clipboard().setText(self.code)
        self.status.setText("Timer copied. Paste it into game chat.")


def _action(action: str, sound: str, speech: str, file: str = "") -> str:
    if action == "sound":
        return f"Play {sound}"
    if action == "speak":
        return f"Say: {speech}"
    if action == "file":
        return f"Play sound file: {file}"
    return "Nothing"


def _summary(trigger: Trigger, sender: str, cfg: object = None) -> str:
    modes = {"contains": "Contains", "starts": "Starts with", "exact": "Whole line", "regex": "Regular expression"}
    overlaps = {"replace": "Replace", "retain": "Retain", "stack": "Add another timer"}
    lines = [
        f"Name: {trigger.name}",
        f"Shared by: {sender or 'Unknown player'}",
        f"Enabled after import: {'Yes' if trigger.enabled else 'No'}",
        f"Category: {trigger.category or 'None'}",
        "",
        f"Match: {modes.get(trigger.mode, trigger.mode)}",
        f"Chat text: {trigger.pattern}",
        f"Forgive OCR typos: {'Yes' if trigger.fuzzy else 'No'}",
        "",
        f"When matched: {_action(trigger.action, trigger.sound, trigger.speech, trigger.file)}",
        f"Volume: {trigger.volume}%",
        f"Ignore repeats for: {trigger.cooldown_s:g} seconds",
        f"Label: {trigger.timer_label or trigger.name}",
        "",
        f"Start a timer: {'Yes' if trigger.timer else 'No'}",
    ]
    if trigger.timer:
        lines.extend([
            f"Length: {trigger.timer_seconds:g} seconds",
            f"If already running: {overlaps.get(trigger.timer_mode, trigger.timer_mode)}",
            f"Normal color: {trigger.timer_color or 'Theme default'}",
            f"Warning color: {trigger.timer_warn_color or 'Theme default'}",
            f"Low / ended color: {trigger.timer_low_color or 'Theme default'}",
            f"Low duration: {trigger.timer_low_s:g} seconds remaining",
            f"Warning: {trigger.timer_warn_s:g} seconds before the end" if trigger.timer_warn_s else "Warning: Off",
        ])
        if trigger.timer_warn_s:
            lines.append(f"Warning action: {_action(trigger.timer_warn_action, trigger.timer_warn_sound, trigger.timer_warn_speech, trigger.file)}")
        lines.append(f"When it ends: {_action(trigger.timer_end_action, trigger.timer_end_sound, trigger.timer_end_speech, trigger.file)}")
    if external_sound_files([trigger]):
        lines.extend(["", "Sound files are not included. Choose their locations in the editor after importing."])
    return "\n".join(lines)


class TriggerSharePrompt(QDialog):
    """Review a received definition. The owner saves it only after Import is clicked."""

    import_requested = Signal()

    def __init__(self, trigger: Trigger, sender: str = "", parent: QWidget | None = None, *, cfg: object = None) -> None:
        super().__init__(parent)
        self._trigger, self._sender, self._cfg = trigger, sender, cfg
        self.setWindowTitle(window_title("trigger-import"))
        self.setModal(False)
        self.resize(600, 600)
        self.setMinimumSize(440, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QLabel("Review this shared timer before importing it.")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.details = QPlainTextEdit(_summary(trigger, sender, cfg))
        self.details.setReadOnly(True)
        self.details.setTabChangesFocus(True)
        self.details.setAccessibleName("Shared timer details")
        layout.addWidget(self.details, 1)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {token('DANGER')};")
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox()
        self.import_button = buttons.addButton("Import timer", QDialogButtonBox.ButtonRole.ActionRole)
        self.import_button.clicked.connect(lambda _checked=False: self.import_requested.emit())
        self.dismiss_button = buttons.addButton("Dismiss", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    def set_config(self, cfg: object) -> None:
        self._cfg = cfg
        self.details.setPlainText(_summary(self._trigger, self._sender, cfg))

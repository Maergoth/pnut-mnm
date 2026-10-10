"""User-driven copying and review of timer definitions shared through game chat."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from mnmparse.app.widgets import ElidedLabel, token
from mnmparse.app.window_identity import window_title
from mnmparse.privacy import casual_enabled, safe_trigger_definition, safe_trigger_label
from mnmparse.trigger_exchange import external_sound_files
from mnmparse.triggers import Trigger


class TriggerChatExportDialog(QDialog):
    """Copy one selected timer as one chat message; never type or send into the game."""

    def __init__(self, trigger: Trigger, code: str, parent: QWidget | None = None, *, cfg: object = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._trigger = trigger
        blocked = safe_trigger_definition(trigger, cfg) is None
        if not isinstance(code, str) or not code or "\n" in code or "\r" in code:
            raise ValueError("A timer must be shared as one chat line.")
        self.code = "" if blocked else code
        self.setWindowTitle(window_title("trigger-export"))
        self.setModal(False)
        self.resize(620, 380)
        self.setMinimumSize(440, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        self.title = ElidedLabel(safe_trigger_label(trigger.name, cfg))
        self.title.setObjectName("Heading")
        self.title.setToolTip(safe_trigger_label(trigger.name, cfg))
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
        self.character_count = QLabel("Definition hidden in Casual Mode" if blocked else f"{len(code)} characters · one chat message")
        self.character_count.setWordWrap(True)
        layout.addWidget(self.character_count)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setStyleSheet(f"color: {token('SUCCESS')};")
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.copy = buttons.addButton("Copy for game chat", QDialogButtonBox.ButtonRole.ActionRole)
        self.copy.setEnabled(not blocked)
        self.copy.clicked.connect(self._copy_line)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_config(self, cfg: object) -> None:
        self._cfg = cfg
        if safe_trigger_definition(self._trigger, cfg) is None:
            self.code = ""
            self.line.clear()
            self.title.setText(safe_trigger_label(self._trigger.name, cfg))
            self.title.setToolTip("")
            self.character_count.setText("Definition hidden in Casual Mode")
            self.copy.setEnabled(False)
            self.status.setText("Casual Mode hides timer definitions; copying is unavailable.")

    def _copy_line(self) -> None:
        if safe_trigger_definition(self._trigger, self._cfg) is None:
            self.set_config(self._cfg)
            return
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
    if casual_enabled(cfg):
        return (f"Name: {safe_trigger_label(trigger.name, cfg)}\n"
                "Sender, custom definition, captures and speech are hidden in Casual Mode.\n\n"
                f"Enabled after import: {'Yes' if trigger.enabled else 'No'}\n"
                f"Start a timer: {'Yes' if trigger.timer else 'No'}\n"
                + (f"Length: {trigger.timer_seconds:g} seconds\n" if trigger.timer else "")
                + "\nImport keeps the full definition locally. Casual Mode continues to hide custom text and plays only safe cues.")
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
        self.error.setText("The timer could not be imported. Check the timer save status and try again." if casual_enabled(self._cfg) else text)
        self.error.show()

    def set_config(self, cfg: object) -> None:
        self._cfg = cfg
        self.details.setPlainText(_summary(self._trigger, self._sender, cfg))
        if casual_enabled(cfg):
            self.error.clear()

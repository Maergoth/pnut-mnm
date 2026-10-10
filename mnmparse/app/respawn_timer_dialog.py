"""Choose a duration for a single NPC respawn countdown."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget

from mnmparse.app.widgets import ElidedLabel
from mnmparse.app.window_identity import window_title

DEFAULT_RESPAWN_SECONDS = 300
MAX_RESPAWN_SECONDS = 600 * 60 + 59


class RespawnTimerDialog(QDialog):
    def __init__(self, name: str, seconds: int, parent: QWidget | None = None,
                 *, remember_duration: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title("respawn-timer"))
        # Keep normal dialog flags so the duration editor can accept keyboard focus.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QLabel("Respawn timer")
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        mob = ElidedLabel(name)
        mob.setTextFormat(Qt.TextFormat.PlainText)
        mob.setToolTip(name)
        layout.addWidget(mob)
        hint = QLabel("Start one countdown now." +
                      (" This duration will be remembered for all mobs in this zone." if remember_duration else ""))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        duration = QHBoxLayout()
        duration.addWidget(QLabel("Duration"))
        self.minutes = QSpinBox()
        self.minutes.setRange(0, 600)
        self.minutes.setSuffix(" min")
        self.minutes.setAccessibleName("Respawn duration in minutes")
        self.seconds = QSpinBox()
        self.seconds.setRange(0, 59)
        self.seconds.setSuffix(" s")
        self.seconds.setAccessibleName("Respawn duration in seconds")
        self.minutes.setValue(seconds // 60)
        self.seconds.setValue(seconds % 60)
        duration.addWidget(self.minutes)
        duration.addWidget(self.seconds)
        layout.addLayout(duration)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.start_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.start_button.setText("Start timer")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.minutes.valueChanged.connect(self._validate)
        self.seconds.valueChanged.connect(self._validate)
        self._validate()
        self.resize(380, self.sizeHint().height())
        self.minutes.setFocus()
        self.minutes.selectAll()

    def duration(self) -> int:
        return self.minutes.value() * 60 + self.seconds.value()

    def _validate(self) -> None:
        self.start_button.setEnabled(self.duration() > 0)

    def accept(self) -> None:
        if self.duration() > 0:
            super().accept()

"""A short release announcement shown once when the main window is in use."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from mnmparse.app import APP_VERSION, theme
from mnmparse.app.window_identity import window_title

log = logging.getLogger(__name__)
SEEN_VERSION_KEY = "app/whats_new_version"


@dataclass(frozen=True)
class ReleaseNotes:
    changes: tuple[str, ...]
    settings_label: str
    feature_page: str = ""
    feature_label: str = ""
    settings_target: str = "revenge_enabled"


# Every release adds a record here. A regression check keeps a version bump from
# shipping without its announcement; old records can still describe a rollback.
RELEASE_NOTES: dict[str, ReleaseNotes] = {
    "1.0.7": ReleaseNotes(
        changes=(
            "The Revenge pop-out remembers whether it was open, its position and its size after restart.",
            "Removing a player or guild from the pop-out now asks for confirmation.",
            "Each list has clipboard copy and import icons. Preview an import, then choose Add.",
        ),
        settings_label="Revenge settings",
        settings_target="revenge_enabled",
    ),
    "1.0.6": ReleaseNotes(
        changes=(
            "Turn off Show other groups to hide italic names from your combat meters.",
            "Right-click a player and choose Add pet to assign a hidden italic name.",
            "Assigned pets combine with their owner in Live and overlay meters.",
        ),
        settings_label="Settings",
        settings_target="show_other_groups",
    ),
    "1.0.5": ReleaseNotes(
        changes=(
            "Players and Guilds now sit side by side in the Revenge List.",
            "Each list has Add, From clipboard, and Export CSV controls.",
            "Pop-out controls appear on hover. Resize it using the six-dot grip.",
            "Timers share compact codes. Paste text or open a file in the same Import dialog.",
        ),
        settings_label="Revenge settings",
        feature_page="triggers",
        feature_label="Timer sharing",
    ),
}


class WhatsNewDialog(QDialog):
    settings_requested = Signal()
    feature_requested = Signal(str)

    def __init__(self, version: str, notes: ReleaseNotes, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("PnutWhatsNew")
        self.setWindowTitle(window_title("whats-new"))
        self.setAccessibleName("What's New!")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(440)
        self.resize(580, 350)
        self.setStyleSheet(f"""
            QDialog#PnutWhatsNew {{ background: {theme.BG0}; color: {theme.TEXT}; }}
            QDialog#PnutWhatsNew QLabel {{ background: transparent; color: {theme.TEXT}; }}
            QDialog#PnutWhatsNew QLabel#Heading {{ font-size: 24px; font-weight: 600; }}
            QDialog#PnutWhatsNew QLabel#ReleaseVersion {{ color: {theme.MUTED}; }}
            QDialog#PnutWhatsNew QFrame#ReleaseChanges {{
                background: {theme.BG1}; border: 1px solid {theme.LINE}; border-radius: 10px;
            }}
            QDialog#PnutWhatsNew QPushButton {{
                color: {theme.TEXT}; background: {theme.BG2};
                border: 1px solid {theme.LINE}; border-radius: 8px; padding: 9px 16px;
            }}
            QDialog#PnutWhatsNew QPushButton:hover {{ border-color: {theme.ACCENT}; }}
            QDialog#PnutWhatsNew QPushButton#GotIt {{
                background: {theme.ACCENT}; color: {theme.BG0}; font-weight: 600;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        heading = QLabel("What's New!", self)
        heading.setObjectName("Heading")
        heading.setFont(theme.app_font(24, 600))
        layout.addWidget(heading)
        version_label = QLabel(f"PNUT {version}", self)
        version_label.setObjectName("ReleaseVersion")
        layout.addWidget(version_label)
        card = QFrame(self)
        card.setObjectName("ReleaseChanges")
        changes = QVBoxLayout(card)
        changes.setContentsMargins(16, 14, 16, 14)
        changes.setSpacing(12)
        self.change_labels = []
        for change in notes.changes:
            label = QLabel(change, card)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            changes.addWidget(label)
            self.change_labels.append(label)
        layout.addWidget(card, 1)
        buttons = QHBoxLayout()
        self.settings_button = QPushButton(notes.settings_label, self)
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(self._open_settings)
        buttons.addWidget(self.settings_button)
        self.feature_button = None
        if notes.feature_page and notes.feature_label:
            self.feature_button = QPushButton(notes.feature_label, self)
            self.feature_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.feature_button.clicked.connect(lambda: self._open_feature(notes.feature_page))
            buttons.addWidget(self.feature_button)
        buttons.addStretch(1)
        self.got_it_button = QPushButton("Got it", self)
        self.got_it_button.setObjectName("GotIt")
        self.got_it_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.got_it_button.setDefault(True)
        self.got_it_button.clicked.connect(self.accept)
        buttons.addWidget(self.got_it_button)
        layout.addLayout(buttons)

    def _open_settings(self) -> None:
        self.accept()
        self.settings_requested.emit()

    def _open_feature(self, page: str) -> None:
        self.accept()
        self.feature_requested.emit(page)


class WhatsNewController(QObject):
    """Wait for setup and an active main window; acknowledge only a shown dialog."""

    def __init__(self, settings: QSettings, window: QWidget, *,
                 eligible: Callable[[], bool], open_settings: Callable[[], None],
                 open_feature: Callable[[str], None] | None = None,
                 version: str = APP_VERSION):
        super().__init__(window)
        self.settings = settings
        self.window = window
        self.version = version
        self.dialog: WhatsNewDialog | None = None
        self._eligible = eligible
        self._open_settings = open_settings
        self._open_feature = open_feature
        self._stopped = False
        self._presented = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.show_if_needed)
        window.installEventFilter(self)

    def request(self) -> None:
        if not self._stopped and not self._timer.isActive():
            self._timer.start(0)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.window and event.type() in (
            QEvent.Type.Show, QEvent.Type.WindowActivate, QEvent.Type.WindowStateChange,
        ):
            self.request()
        return False

    def show_if_needed(self) -> bool:
        if (self._stopped or self.dialog is not None
                or self.settings.value(SEEN_VERSION_KEY, "", type=str) == self.version
                or not self._eligible() or not self.window.isVisible() or self.window.isMinimized()
                or not self.window.isActiveWindow() or QApplication.activeModalWidget() is not None):
            return False
        notes = RELEASE_NOTES.get(self.version)
        if notes is None:
            log.warning("What's New notes are missing for version %s", self.version)
            return False
        dialog = WhatsNewDialog(self.version, notes, self.window)
        self.dialog = dialog
        dialog.settings_requested.connect(self._open_settings)
        if self._open_feature is not None:
            dialog.feature_requested.connect(self._open_feature)
        elif dialog.feature_button is not None:
            dialog.feature_button.hide()
        dialog.finished.connect(self._dismissed)
        dialog.open()
        self._presented = dialog.isVisible()
        return self._presented

    def _dismissed(self, _result: int) -> None:
        if self._presented and not self._stopped:
            self.settings.setValue(SEEN_VERSION_KEY, self.version)
            self.settings.sync()
        dialog, self.dialog = self.dialog, None
        self._presented = False
        if dialog is not None:
            dialog.deleteLater()

    def stop(self) -> None:
        self._stopped = True
        self._timer.stop()
        if self.dialog is not None:
            self.dialog.reject()

"""A focus-preserving restart countdown, independent of the main window.

This notice only requests an action. The application owns update validation,
installation and shutdown, and can call ``show_failure`` to offer a manual retry.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLayout, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from mnmparse.app import theme
from mnmparse.app.window_identity import window_title


class UpdateRestartNotice(QDialog):
    """Offer a ten-second restart, with one decision per presentation.

    ``begin(version)`` never restarts a countdown already seen by this instance,
    including a version deferred with Later, close or Escape. ``show_failure``
    explicitly offers one new manual retry; it never schedules an automatic retry.
    ``stop()`` silently cancels the current presentation for application teardown.
    """

    restart_requested = Signal()
    postponed = Signal()

    def __init__(self, *, countdown_seconds: float = 10.0) -> None:
        # No main-window parent: minimizing/hiding the app must not hide this notice.
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
        self.setWindowTitle(window_title("update-restart"))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFont(theme.app_font())
        self.setStyleSheet(theme.main_qss())
        self.setAccessibleName("Update ready to restart")
        self._seconds = max(0.0, float(countdown_seconds))
        self._version = ""
        self._seen: set[str] = set()
        self._deadline: float | None = None
        self._resolved = True

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        self.heading = QLabel("Update ready", self)
        self.heading.setProperty("class", "h2")
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.heading)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body = QWidget(self._scroll)
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(10)
        self.version_label = QLabel(body)
        self.status_label = QLabel(body)
        for label in (self.version_label, self.status_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._body_layout.addWidget(label)
        self._body_layout.addStretch()
        self._scroll.setWidget(body)
        layout.addWidget(self._scroll, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()
        self.later_button = QPushButton("Later", self)
        self.restart_button = QPushButton("Restart now", self)
        self.restart_button.setProperty("class", "primary")
        for button in (self.later_button, self.restart_button):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.restart_button.clicked.connect(self._restart)
        self.later_button.clicked.connect(self._postpone)

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

    def begin(self, version: str) -> None:
        version = str(version or "").strip()
        if not version or version in self._seen:
            return
        self._timer.stop()
        self._seen.add(version)
        self._version = version
        self._resolved = False
        self.heading.setText("Update ready")
        self.version_label.setText(f"Version {version} is ready to install.")
        self.restart_button.setEnabled(True)
        self.later_button.setEnabled(True)
        self._deadline = time.monotonic() + self._seconds
        self._tick()
        if not self._resolved:
            self._timer.start()
            self._present()

    def stop(self) -> None:
        """Cancel silently; repeated ready notifications cannot re-arm this version."""
        self._timer.stop()
        self._deadline = None
        self._resolved = True
        self.hide()

    def show_failure(self, message: str) -> None:
        """Keep the failure visible until the user retries or chooses Later."""
        self._timer.stop()
        self._deadline = None
        self._resolved = False
        self.heading.setText("Restart could not finish")
        self.version_label.setText(f"Version {self._version} is ready to install." if self._version else "")
        self.status_label.setText(str(message or "Please try restarting again when you are ready."))
        self.restart_button.setEnabled(True)
        self.later_button.setEnabled(True)
        self._present()

    def _present(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            width = min(500, max(1, area.width() - 32))
            content_width = max(1, width - 40 - self._scroll.verticalScrollBar().sizeHint().width())
            body_height = sum(max(label.fontMetrics().height(), label.heightForWidth(content_width))
                              for label in (self.version_label, self.status_label)) + 14
            height = min(max(190, body_height + 112), max(1, area.height() - 32))
            self.resize(width, height)
        else:
            self.resize(500, 210)
        self.show()
        if screen is not None:
            # Use the native frame after show(), so title-bar borders also fit.
            frame = self.frameGeometry()
            if frame.width() > area.width() or frame.height() > area.height():
                border_width = max(0, frame.width() - self.width())
                border_height = max(0, frame.height() - self.height())
                self.resize(min(self.width(), max(1, area.width() - border_width)),
                            min(self.height(), max(1, area.height() - border_height)))
                frame = self.frameGeometry()
            x = area.left() + max(0, (area.width() - frame.width()) // 2)
            y = area.top() + max(0, (area.height() - frame.height()) // 2)
            self.move(x, y)

    def _tick(self) -> None:
        if self._resolved or self._deadline is None:
            return
        remaining = max(0, math.ceil(self._deadline - time.monotonic()))
        if remaining == 0:
            self._restart()
            return
        unit = "second" if remaining == 1 else "seconds"
        self.status_label.setText(f"Restarting in {remaining} {unit}. Choose Later to keep using PNUT.")

    def _restart(self) -> None:
        if self._resolved:
            return
        self.stop()
        self.restart_requested.emit()

    def _postpone(self) -> None:
        if self._resolved:
            return
        self.stop()
        self.postponed.emit()

    def reject(self) -> None:
        self._postpone()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self._postpone()
        event.accept()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.DeferredDelete:
            self.stop()
        return super().event(event)


__all__ = ["UpdateRestartNotice"]

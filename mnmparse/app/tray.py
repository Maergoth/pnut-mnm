"""System tray icon with the app's quick actions.

:class:`TrayIcon` only emits signals; the application (``main.py``) decides what they do.
The menu mirrors the top bar: show/hide the window, start/stop capture, overlay on/off,
lock/unlock the overlay, reset the encounter, quit.  A balloon is shown the first time the
window is minimised to the tray so the user knows where it went.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from mnmparse.app import APP_NAME

log = logging.getLogger(__name__)


class TrayIcon(QSystemTrayIcon):
    """The tray icon and its context menu.

    Signals:
        show_window_requested: "Show window" (or a double-click on the icon).
        hide_window_requested: "Hide window".
        toggle_capture_requested: "Start capture" / "Stop capture".
        toggle_overlay_requested: "Show overlay" / "Hide overlay".
        toggle_lock_requested: "Lock overlay" / "Unlock overlay".
        reset_requested: "Reset encounter".
        quit_requested: "Quit".

    The ``set_*`` methods keep the menu labels in sync with the real state; they never
    emit the request signals.
    """

    show_window_requested = Signal()
    hide_window_requested = Signal()
    toggle_capture_requested = Signal()
    toggle_overlay_requested = Signal()
    toggle_lock_requested = Signal()
    toggle_click_through_requested = Signal()
    reset_requested = Signal()
    quit_requested = Signal()

    BALLOON_MS = 4000

    def __init__(self, icon: QIcon, parent: QObject | None = None) -> None:
        super().__init__(icon, parent)
        self.setToolTip(APP_NAME)
        self._window_visible = True
        self._capture_running = False
        self._overlay_visible = False
        self._overlay_locked = True

        self._menu = QMenu()
        self._window_action = QAction("Hide window", self._menu)
        self._window_action.triggered.connect(self._on_window_action)
        self._capture_action = QAction("Start capture", self._menu)
        self._capture_action.triggered.connect(self.toggle_capture_requested)
        self._overlay_action = QAction("Show overlay", self._menu)
        self._overlay_action.triggered.connect(self.toggle_overlay_requested)
        self._lock_action = QAction("Unlock overlay", self._menu)
        self._lock_action.triggered.connect(self.toggle_lock_requested)
        self._click_through_action = QAction("Overlay ignores mouse", self._menu)
        self._click_through_action.setCheckable(True)
        self._click_through_action.setToolTip("Click-through: the overlay passes every click to the game (tabs and tooltips stop working)")
        self._click_through_action.triggered.connect(self.toggle_click_through_requested)
        self._reset_action = QAction("Reset encounter", self._menu)
        self._reset_action.triggered.connect(self.reset_requested)
        self._quit_action = QAction("Quit", self._menu)
        self._quit_action.triggered.connect(self.quit_requested)

        self._menu.addAction(self._window_action)
        self._menu.addSeparator()
        self._menu.addAction(self._capture_action)
        self._menu.addAction(self._reset_action)
        self._menu.addSeparator()
        self._menu.addAction(self._overlay_action)
        self._menu.addAction(self._lock_action)
        self._menu.addAction(self._click_through_action)
        self._menu.addSeparator()
        self._menu.addAction(self._quit_action)
        self.setContextMenu(self._menu)
        self.activated.connect(self._on_activated)
        self._refresh_labels()

    # -- state mirrors ---------------------------------------------------------------

    def set_window_visible(self, visible: bool) -> None:
        """Mirror the main window's visibility in the menu label."""
        self._window_visible = bool(visible)
        self._refresh_labels()

    def set_capture_running(self, running: bool) -> None:
        """Mirror the engine state ("Start capture" / "Stop capture")."""
        self._capture_running = bool(running)
        self._refresh_labels()

    def set_overlay_visible(self, visible: bool) -> None:
        """Mirror the overlay visibility ("Show overlay" / "Hide overlay")."""
        self._overlay_visible = bool(visible)
        self._refresh_labels()

    def set_overlay_locked(self, locked: bool) -> None:
        """Mirror the overlay lock state ("Lock overlay" / "Unlock overlay")."""
        self._overlay_locked = bool(locked)
        self._refresh_labels()

    def set_overlay_click_through(self, enabled: bool) -> None:
        """Mirror the overlay's click-through switch (checked = ignores the mouse)."""
        self._click_through_action.blockSignals(True)
        self._click_through_action.setChecked(bool(enabled))
        self._click_through_action.blockSignals(False)

    def set_overlay_available(self, available: bool, *, lock_available: bool | None = None) -> None:
        """Disable unavailable overlay actions; the map can still share the lock control."""
        self._overlay_action.setEnabled(available)
        self._lock_action.setEnabled(available if lock_available is None else lock_available)
        self._click_through_action.setEnabled(available)

    def set_status_text(self, text: str) -> None:
        """Set the hover tooltip (``APP_NAME - text``)."""
        self.setToolTip(f"{APP_NAME} - {text}" if text else APP_NAME)

    # -- notifications --------------------------------------------------------------

    def show_minimised_balloon(self) -> None:
        """Tell the user the window is still running in the tray."""
        if not self.supportsMessages():
            log.debug("tray balloons unsupported on this desktop")
            return
        self.showMessage(
            APP_NAME,
            "Still running here. Double-click the tray icon to open the window; "
            "right-click for Start/Stop, overlay and Quit.",
            QSystemTrayIcon.MessageIcon.Information,
            self.BALLOON_MS,
        )

    def notify(self, title: str, text: str, *, warning: bool = False) -> None:
        """Show a short balloon (used for errors such as 'game window not found')."""
        if not self.supportsMessages():
            return
        icon = QSystemTrayIcon.MessageIcon.Warning if warning else QSystemTrayIcon.MessageIcon.Information
        self.showMessage(title, text, icon, self.BALLOON_MS)

    # -- internals --------------------------------------------------------------------

    def _refresh_labels(self) -> None:
        self._window_action.setText("Hide window" if self._window_visible else "Show window")
        self._capture_action.setText("Stop capture" if self._capture_running else "Start capture")
        self._overlay_action.setText("Hide overlay" if self._overlay_visible else "Show overlay")
        self._lock_action.setText("Unlock overlay" if self._overlay_locked else "Lock overlay")

    def _on_window_action(self) -> None:
        if self._window_visible:
            self.hide_window_requested.emit()
        else:
            self.show_window_requested.emit()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window_requested.emit()


__all__ = ["TrayIcon"]

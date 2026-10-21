"""A considered NPC card's five-second lifetime, independent of its contents."""
from __future__ import annotations

from collections.abc import Callable
import math
import time
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer

from mnmparse.app.morality import reduced_motion

VISIBLE_SECONDS = 5.0
FADE_SECONDS = 0.35
FADE_TICK_MS = 40


class NpcPanelLifetime(QObject):
    """Only an explicit ``present`` can renew an expired card.

    The panel owns visibility and calls ``sync`` before its normal docking/input
    update. This helper only reduces whole-window opacity and hides an expired
    card; background data, style and layout changes cannot show it again.
    """

    def __init__(self, panel: Any, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(panel)
        self._panel = panel
        self._clock = clock
        self.expired = True
        self._deadline: float | None = None
        self._hovered = False
        self._interacting = False
        self._stopped = False
        self._opacity = 1.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    def present(self) -> None:
        """Renew after a consider/manual selection, including the same NPC."""
        if self._stopped:
            return
        self.expired = False
        if not self._accepts_interaction():
            self._hovered = self._interacting = False
        self._deadline = None if self._paused else self._clock() + VISIBLE_SECONDS
        self._set_opacity(1.0)
        self._tick()

    @property
    def _paused(self) -> bool:
        return self._hovered or self._interacting

    def _accepts_interaction(self) -> bool:
        panel = self._panel
        return (panel.isVisible() and bool(getattr(panel, "enabled", True))
                and panel._owner.isVisible() and not panel._owner.click_through)

    def set_hovered(self, hovered: bool) -> None:
        """Pause over the card or any child; leaving grants a fresh five seconds."""
        self._set_pause("_hovered", hovered)

    def set_interacting(self, interacting: bool) -> None:
        """Pause for a held drag/resize, scrollbar gesture or context menu."""
        self._set_pause("_interacting", interacting)

    def _set_pause(self, field: str, value: bool) -> None:
        if self._stopped or self.expired:
            return
        if self._deadline is not None and self._clock() >= self._deadline:
            self._tick()
            return  # A delayed enter/press cannot rescue an elapsed deadline.
        value = bool(value) and self._accepts_interaction()
        if bool(getattr(self, field)) == value:
            return
        was_paused = self._paused
        setattr(self, field, value)
        if self._paused:
            self._deadline = None
        elif was_paused:
            self._deadline = self._clock() + VISIBLE_SECONDS
        self._set_opacity(1.0)
        self._tick()

    def sync(self) -> None:
        """Recheck visibility/input changes without renewing the current card."""
        if self._stopped:
            return
        if not self._accepts_interaction():
            was_paused = self._paused
            self._hovered = self._interacting = False
            if was_paused and not self.expired:
                self._deadline = self._clock() + VISIBLE_SECONDS
        self._tick()

    def hidden(self) -> None:
        """Clear hover when hidden, continuing expiry while its owner is hidden."""
        if self._stopped:
            return
        was_paused = self._paused
        self._hovered = self._interacting = False
        self._set_opacity(1.0)
        if was_paused and not self.expired:
            self._deadline = self._clock() + VISIBLE_SECONDS
        self._tick()

    def stop(self) -> None:
        """Permanently stop on panel teardown, including any active fade."""
        self._stopped = True
        self.expired = True
        self._deadline = None
        self._hovered = self._interacting = False
        self._timer.stop()
        self._set_opacity(1.0)

    def _set_opacity(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        if abs(value - self._opacity) > 0.0001:
            self._opacity = value
            self._panel.setWindowOpacity(value)

    def _tick(self) -> None:
        if self._stopped:
            return
        if self.expired:
            self._timer.stop()
            self._set_opacity(1.0)
            return
        if self._paused:
            self._timer.stop()
            self._set_opacity(1.0)
            return
        # present() establishes a deadline before a not-yet-shown panel is
        # synchronized. No subsequent data/style update replaces that deadline.
        if self._deadline is None:
            return
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            self.expired = True
            self._deadline = None
            self._timer.stop()
            if self._panel.isVisible():
                self._panel.hide()
                self._panel._owner.panels_changed()
            self._set_opacity(1.0)
            return
        motion = not reduced_motion(getattr(self._panel._owner, "_cfg", None))
        if motion and remaining <= FADE_SECONDS:
            self._set_opacity(remaining / FADE_SECONDS if self._panel.isVisible() else 1.0)
            delay_ms = min(FADE_TICK_MS, math.ceil(remaining * 1000))
        else:
            self._set_opacity(1.0)
            # sync can run before DockedPanel shows the card. Schedule fade
            # start even while hidden so a first presentation still animates.
            delay_ms = math.ceil((remaining - FADE_SECONDS if motion else remaining) * 1000)
        # A precise single-shot schedules the actual deadline as the final tick;
        # fixed repeating intervals would unnecessarily extend it by a frame.
        self._timer.start(max(1, delay_ms))


__all__ = ["NpcPanelLifetime"]

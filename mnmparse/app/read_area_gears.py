"""Tiny passive settings shortcuts beside the actual game read rectangles.

Capture pixels stay physical until the final per-monitor Qt placement. All game
and monitor queries are read-only; display affinity affects only our own windows.
"""
from __future__ import annotations

import ctypes
import logging
import math
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QRegion
from PySide6.QtWidgets import QWidget

from mnmparse.app.overlay_settings import OverlaySettingsGear
from mnmparse.app.window_identity import window_title

log = logging.getLogger(__name__)
Rect = tuple[int, int, int, int]
TARGETS = ("crop_picker", "player_status_enabled")
_LABELS = {"crop_picker": "combat chat reader", "player_status_enabled": "mini-stat reader"}


def _rect(value: Any) -> Rect | None:
    if (not isinstance(value, (tuple, list)) or len(value) != 4
            or any(type(n) is not int for n in value)):
        return None
    left, top, right, bottom = value
    return tuple(value) if left < right and top < bottom else None


def _contains(rect: Rect, point: tuple[int, int]) -> bool:
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def _intersects(first: Rect, second: Rect) -> bool:
    return (max(first[0], second[0]) < min(first[2], second[2])
            and max(first[1], second[1]) < min(first[3], second[3]))


def _hover_contains(area: Rect, button: Rect, point: tuple[int, int]) -> bool:
    # Include the short gap to an adjacent button, so moving slowly onto it
    # doesn't make the shortcut disappear between the read area and its gear.
    bridge = (min(area[0], button[0]), min(area[1], button[1]),
              max(area[0], button[2]), max(area[1], button[3]))
    return _contains(area, point) or _contains(bridge, point)


def _window_rectangles(hwnd: int) -> dict[str, Rect]:
    """Current physical outer, composed-frame and client bounds, when visible."""
    if sys.platform != "win32":
        return {}
    import ctypes.wintypes as wt

    user = ctypes.windll.user32
    user.IsWindowVisible.argtypes = [wt.HWND]
    user.IsIconic.argtypes = [wt.HWND]
    if not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd):
        return {}
    result = {}
    outer, client = wt.RECT(), wt.RECT()
    user.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    if user.GetWindowRect(hwnd, ctypes.byref(outer)):
        result["outer"] = (outer.left, outer.top, outer.right, outer.bottom)
    user.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    if user.GetClientRect(hwnd, ctypes.byref(client)):
        origin = wt.POINT(client.left, client.top)
        if user.ClientToScreen(hwnd, ctypes.byref(origin)):
            result["client"] = (origin.x, origin.y, origin.x + client.right - client.left,
                                origin.y + client.bottom - client.top)
    frame = wt.RECT()
    try:
        get_frame = ctypes.windll.dwmapi.DwmGetWindowAttribute
        get_frame.argtypes = [wt.HWND, wt.DWORD, wt.LPVOID, wt.DWORD]
        get_frame.restype = ctypes.c_long
        if get_frame(hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame)) == 0:
            result["frame"] = (frame.left, frame.top, frame.right, frame.bottom)
    except (AttributeError, OSError):
        pass
    return {name: value for name, value in result.items() if _rect(value)}


def resolve_read_areas(snapshot: Mapping[str, Any], *,
                       window_rectangles: Mapping[str, Rect] | None = None) -> dict[str, Rect]:
    """Resolve engine metadata to physical screen rectangles without scaling guesses."""
    hwnd = snapshot.get("hwnd")
    dimensions = snapshot.get("frame_dimensions")
    if (type(hwnd) is not int or hwnd <= 0 or not isinstance(dimensions, (tuple, list))
            or len(dimensions) != 2 or any(type(n) is not int or n <= 0 for n in dimensions)):
        return {}
    bounds = dict(_window_rectangles(hwnd) if window_rectangles is None else window_rectangles)
    backend = str(snapshot.get("capture_backend", "")).lower()
    order = ("outer",) if backend == "mss" else ("frame", "client", "outer") if backend == "wgc" else ()
    frame = next((bounds[key] for key in order if _rect(bounds.get(key))
                  and (bounds[key][2] - bounds[key][0], bounds[key][3] - bounds[key][1]) == tuple(dimensions)), None)
    if frame is None:
        return {}
    result = {}
    for target, key in (("crop_picker", "chat_crop"), ("player_status_enabled", "stats_crop")):
        crop = _rect(snapshot.get(key))
        if crop is not None and 0 <= crop[0] < crop[2] <= dimensions[0] and 0 <= crop[1] < crop[3] <= dimensions[1]:
            result[target] = (frame[0] + crop[0], frame[1] + crop[1], frame[0] + crop[2], frame[1] + crop[3])
    return result


@dataclass(frozen=True)
class ScreenMapping:
    physical: Rect
    logical: Rect
    scale: float

    def qt_point(self, point: tuple[int, int]) -> QPoint:
        return QPoint(round(self.logical[0] + (point[0] - self.physical[0]) / self.scale),
                      round(self.logical[1] + (point[1] - self.physical[1]) / self.scale))


def _native_monitors() -> dict[str, Rect]:
    if sys.platform != "win32":
        return {}
    import ctypes.wintypes as wt

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wt.DWORD), ("monitor", wt.RECT), ("work", wt.RECT),
                    ("flags", wt.DWORD), ("device", wt.WCHAR * 32)]

    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    user.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MonitorInfo)]
    user.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), callback_type, wt.LPARAM]
    monitors = {}

    def found(monitor, _dc, _rect, _data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.monitor
            monitors[info.device.casefold()] = (r.left, r.top, r.right, r.bottom)
        return True

    user.EnumDisplayMonitors(None, None, callback_type(found), 0)
    return monitors


def _screen_mappings() -> tuple[ScreenMapping, ...]:
    monitors = _native_monitors()
    result = []
    for screen in QGuiApplication.screens():
        r = screen.geometry()
        logical = (r.x(), r.y(), r.x() + r.width(), r.y() + r.height())
        physical = monitors.get(screen.name().casefold())
        if physical is None and sys.platform != "win32":
            physical = logical
        scale = float(screen.devicePixelRatio())
        if physical is not None and math.isfinite(scale) and scale > 0:
            result.append(ScreenMapping(physical, logical, scale))
    return tuple(result)


def screen_at_physical(point: tuple[int, int]):
    """Find a Qt screen using native pixels, including mixed-DPI monitor origins."""
    monitors = _native_monitors()
    for screen in QGuiApplication.screens():
        bounds = monitors.get(screen.name().casefold())
        if bounds is not None and _contains(bounds, point):
            return screen
    return None


def _cursor_position() -> tuple[int, int] | None:
    if sys.platform != "win32":
        return None
    import ctypes.wintypes as wt
    point = wt.POINT()
    get = ctypes.windll.user32.GetCursorPos
    get.argtypes = [ctypes.POINTER(wt.POINT)]
    return (point.x, point.y) if get(ctypes.byref(point)) else None


def _is_foreground(hwnd: int) -> bool:
    if sys.platform != "win32":
        return False
    get = ctypes.windll.user32.GetForegroundWindow
    get.restype = ctypes.c_void_p
    return int(get() or 0) == hwnd


def _exclude_own_window(hwnd: int) -> bool:
    # Older Windows treats0x11 as WDA_MONITOR (black capture), so don't use it.
    if sys.platform != "win32" or sys.getwindowsversion().build < 19041:
        return False
    import ctypes.wintypes as wt
    user = ctypes.windll.user32
    user.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
    user.GetWindowDisplayAffinity.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    affinity = wt.DWORD()
    return bool(user.SetWindowDisplayAffinity(hwnd, 0x11)
                and user.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)) and affinity.value == 0x11)


class _AreaButton(OverlaySettingsGear):
    def __init__(self, host: QWidget, callback: Callable[[], None], allowed: Callable[[], bool], label: str):
        self._area_allowed = allowed
        super().__init__(host, callback, label=label)

    def _can_show(self) -> bool:
        return self._host.isVisible() and self._host.isEnabled() and bool(self._area_allowed())


class _GearWindow(QWidget):
    def __init__(self, callback: Callable[[], None], allowed: Callable[[], bool], label: str, role: str):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle(window_title("reader-settings-" + role))
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(30, 30)
        self.excluded: bool | None = None
        self.button = _AreaButton(self, callback, allowed, label)
        self.button.setToolTip("")
        self.setMask(QRegion(QRect(4, 4, 22, 22)))

    def ensure_excluded(self) -> bool:
        if self.excluded is None:
            self.excluded = _exclude_own_window(int(self.winId()))
        return self.excluded


class ReadAreaGears(QObject):
    """Poll read-only geometry and hover; only the tiny visible gear accepts input."""
    activated = Signal(str)

    def __init__(self, provider: Callable[[], Mapping[str, Any]], *,
                 input_enabled: Callable[[], bool] | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._provider = provider
        self._input_enabled = input_enabled or (lambda: True)
        self._running = self._closed = False
        self._hwnd = 0
        self._desktop_capture = False
        self._areas: dict[str, Rect] = {}
        self._screens: tuple[ScreenMapping, ...] = ()
        self._button_rects: dict[str, Rect] = {}
        self._revealed: set[str] = set()
        self._windows = {key: _GearWindow(lambda key=key: self._activate(key),
                                         lambda key=key: self._allowed(key), _LABELS[key], key) for key in TARGETS}
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setInterval(250)
        self._geometry_timer.timeout.connect(self.refresh)
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(100)
        self._hover_timer.timeout.connect(self._poll_hover)
        if parent is not None:
            parent.destroyed.connect(self.shutdown)

    def start(self) -> None:
        if self._closed:
            return
        if self._running:
            self.refresh()
            return
        self._running = True
        self.refresh()
        self._geometry_timer.start()
        self._hover_timer.start()

    def stop(self) -> None:
        self._running = False
        self._geometry_timer.stop()
        self._hover_timer.stop()
        self._areas.clear()
        self._screens = ()
        self._button_rects.clear()
        self._revealed.clear()
        for window in self._windows.values():
            window.hide()

    def shutdown(self) -> None:
        if self._closed:
            return
        self.stop()
        self._closed = True
        for window in self._windows.values():
            window.close()
            window.deleteLater()

    def refresh(self) -> None:
        if self._closed or not self._running:
            return
        try:
            snapshot = self._provider() or {}
            self._hwnd = int(snapshot.get("hwnd", 0))
            self._desktop_capture = snapshot.get("capture_backend") == "mss"
            self._areas = resolve_read_areas(snapshot)
            self._screens = _screen_mappings() if self._areas else ()
        except Exception:
            log.debug("Read-area shortcut geometry unavailable", exc_info=True)
            self._areas = {}
        self._poll_hover()

    def _allowed(self, key: str) -> bool:
        if self._closed or not self._running or key not in self._revealed or not self._input_enabled() or not _is_foreground(self._hwnd):
            return False
        point = _cursor_position()
        return (point is not None and key in self._areas and key in self._button_rects
                and _hover_contains(self._areas[key], self._button_rects[key], point))

    def _placement(self, area: Rect, screens: tuple[ScreenMapping, ...]) -> tuple[QPoint, Rect, bool] | None:
        screen = next((screen for screen in screens if _contains(screen.physical, area[:2])), None)
        if screen is None:
            return None
        dpr = screen.scale
        candidates = ((area[0] - round(30 * dpr), area[1]), (area[0], area[1] - round(30 * dpr))) if self._desktop_capture else ()
        for origin in (*candidates, area[:2]):
            button = (origin[0] + round(4 * dpr), origin[1] + round(4 * dpr),
                      origin[0] + round(26 * dpr), origin[1] + round(26 * dpr))
            if (screen.physical[0] <= button[0] < button[2] <= screen.physical[2]
                    and screen.physical[1] <= button[1] < button[3] <= screen.physical[3]):
                outside = not any(_intersects(button, crop) for crop in self._areas.values())
                if origin == area[:2] or outside:
                    return screen.qt_point(origin), button, outside
        return None

    def _poll_hover(self) -> None:
        if self._closed or not self._running:
            return
        point = _cursor_position()
        ready = point is not None and bool(self._input_enabled()) and _is_foreground(self._hwnd)
        screens = self._screens if ready else ()
        placements = {}
        for key, area in self._areas.items():
            placement = self._placement(area, screens)
            if placement is not None:
                placements[key] = placement
        # Keep a previously revealed outside-crop button reachable while moving
        # from the read rectangle onto its adjacent shortcut.
        wanted = {key for key in placements if ready and (_contains(self._areas[key], point)
                  or key in self._revealed and _hover_contains(self._areas[key], placements[key][1], point))}
        self._revealed = wanted
        self._button_rects = {key: value[1] for key, value in placements.items()}
        occupied = []
        for key in sorted(TARGETS, key=lambda target: (self._areas.get(target, (0, 0, 0, 0))[2]
                          - self._areas.get(target, (0, 0, 0, 0))[0]) * (self._areas.get(target, (0, 0, 0, 0))[3]
                          - self._areas.get(target, (0, 0, 0, 0))[1])):
            window = self._windows[key]
            if key not in wanted:
                window.hide()
                continue
            position, button, outside = placements[key]
            excluded = window.ensure_excluded()
            if (self._desktop_capture and not outside and not excluded) or any(_intersects(button, prior) for prior in occupied):
                self._revealed.discard(key)
                window.hide()
                continue
            occupied.append(button)
            window.move(position)
            window.show()
            window.button.sync()

    def _activate(self, key: str) -> None:
        self.refresh()  # Reject a queued click after capture/config/window state changed.
        if self._allowed(key):
            self.activated.emit(key)


__all__ = ["ReadAreaGears", "ScreenMapping", "resolve_read_areas", "screen_at_physical"]

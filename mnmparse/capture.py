"""Frame sources: passive, out-of-process capture of the game window.

Two backends are provided:

* :class:`WgcWindowSource` uses Windows Graphics Capture (via the
  ``windows-capture`` package).  The Desktop Window Manager composes the frame
  for us; nothing is loaded into, sent to, or read from the game process.
* :class:`MssRegionSource` grabs the window's rectangle from the desktop with
  ``mss`` (a plain GDI ``BitBlt`` of the screen).

The only Win32 calls made here are read-only window queries used to *find*
the game window: ``EnumWindows``, ``GetWindowTextW``, ``GetClassNameW``,
``GetWindowRect``, ``GetWindowThreadProcessId`` and ``IsWindowVisible``.  No
handle to any process is opened.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .config import Config

log = logging.getLogger(__name__)

#: ``(left, top, right, bottom)`` in screen pixels.
Rect = tuple[int, int, int, int]


class CaptureError(RuntimeError):
    """A frame source could not start or keep running."""


class WindowNotFoundError(CaptureError):
    """The game window is not open (the CLI maps this to exit code 2)."""


class FrameSource(Protocol):
    """Anything that produces full game-window frames (HxWx3 BGR, uint8)."""

    def start(self) -> None:
        """Begin producing frames.  May raise :class:`WindowNotFoundError`."""

    def latest(self) -> np.ndarray | None:
        """Return a private copy of the newest frame, or ``None`` if none yet."""

    def stop(self) -> None:
        """Stop producing frames and release resources."""


# ---------------------------------------------------------------------------
# Read-only window lookup
# ---------------------------------------------------------------------------

_TITLE_BUFFER_CHARS = 512
_CLASS_BUFFER_CHARS = 256

GAME_WINDOW_CLASSES: frozenset[str] = frozenset({"UnityWndClass"})
"""Window classes the game's main window can have (the game is a Unity player)."""

FOREIGN_WINDOW_CLASSES: tuple[str, ...] = (
    "MozillaWindowClass",  # Firefox, Thunderbird
    "MozillaDialogClass",
    "Chrome_WidgetWin_",  # Chrome, Edge, Brave, Opera, Vivaldi; Electron apps such as Discord and Slack
    "IEFrame",  # Internet Explorer
    "ApplicationFrameWindow",  # store apps (the old Edge, ...)
)
"""Class-name prefixes of browsers and chat apps.  Such a window is never captured, whatever
its title says: a browser tab or a chat about the game can carry the game's exact title."""

WINDOW_CHECK_S = 2.0
"""How often a running source re-checks that its window is still the best valid match
(the same interval as the app's window lookup, ``app.engine.WINDOW_RETRY_S``)."""

_REPORTED_LIMIT = 256
_reported: set[tuple[int, str]] = set()
"""``(hwnd, class)`` of title-matching windows already logged as not the game (logged once)."""


@dataclass(frozen=True)
class WindowInfo:
    """A top-level window as the read-only lookup saw it."""

    hwnd: int
    title: str
    class_name: str
    pid: int  #: owning process id (``GetWindowThreadProcessId``; no handle to the process is opened)
    rect: Rect
    visible: bool

    def describe(self) -> str:
        """``'<title>' (class <class>, pid <pid>, hwnd=<hwnd>)`` for the log."""
        return f"{self.title!r} (class {self.class_name or '?'}, pid {self.pid}, hwnd={self.hwnd})"


def _user32() -> Any:
    """Return ``user32`` with the argument types of the few calls we use set up."""
    import ctypes.wintypes as wt  # noqa: WPS433  (Windows-only import, on purpose)

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)  # type: ignore[attr-defined]
    user32.EnumWindows.argtypes = [enum_proc, wt.LPARAM]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetWindowRect.restype = ctypes.c_bool
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32._mnm_enum_proc = enum_proc  # keep the prototype alive alongside the handle
    return user32


def _enum_windows(title: str) -> list[WindowInfo]:
    """Top-level windows titled ``title`` in any case, from read-only Win32 queries.

    Windows with any other title are skipped before their class is read.  On
    non-Windows platforms always empty.
    """
    if sys.platform != "win32":
        return []
    import ctypes.wintypes as wt

    user32 = _user32()
    wanted = title.casefold()
    found: list[WindowInfo] = []
    text = ctypes.create_unicode_buffer(_TITLE_BUFFER_CHARS)
    cls = ctypes.create_unicode_buffer(_CLASS_BUFFER_CHARS)

    def _on_window(hwnd: int, _lparam: int) -> bool:
        length = user32.GetWindowTextW(hwnd, text, _TITLE_BUFFER_CHARS)
        if length <= 0 or text.value.casefold() != wanted:
            return True
        rect = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        class_name = cls.value if user32.GetClassNameW(hwnd, cls, _CLASS_BUFFER_CHARS) > 0 else ""
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(WindowInfo(
            int(hwnd), text.value, class_name, int(pid.value),
            (rect.left, rect.top, rect.right, rect.bottom), bool(user32.IsWindowVisible(hwnd)),
        ))
        return True

    user32.EnumWindows(user32._mnm_enum_proc(_on_window), 0)
    return found


def _title_match(title: str, wanted: str) -> int | None:
    """0 for the exact title, 1 for the same title in other case, else ``None``.

    A title that merely contains the wanted one (a browser tab "Monsters and Memories
    Wiki - Mozilla Firefox") does not match at all.
    """
    if title == wanted:
        return 0
    if title.casefold() == wanted.casefold():
        return 1
    return None


def _rejection(win: WindowInfo, classes: Collection[str]) -> str | None:
    """Why a window with the game's title is not the game window, or ``None`` when it is."""
    if win.class_name.startswith(FOREIGN_WINDOW_CLASSES):
        return "it is a browser or chat window"
    if win.class_name not in classes:
        return f"its class is not the game's ({', '.join(sorted(classes))})"
    return None


def _report_rejected(win: WindowInfo, why: str) -> None:
    """Log once per window why it is not captured (the app looks it up every few seconds)."""
    key = (win.hwnd, win.class_name)
    if key in _reported:
        return
    if len(_reported) >= _REPORTED_LIMIT:
        _reported.clear()
    _reported.add(key)
    foreign = win.class_name.startswith(FOREIGN_WINDOW_CLASSES)
    log.log(logging.INFO if foreign else logging.WARNING, "Not capturing window %s: %s", win.describe(), why)


def _tier(win: WindowInfo, wanted: str) -> tuple[int, int]:
    """Exact title before other case, then visible before hidden (lower is better; size
    is not part of it)."""
    match = _title_match(win.title, wanted)
    return (2 if match is None else match), (0 if win.visible else 1)


def _valid_windows(title: str, classes: Collection[str]) -> list[WindowInfo]:
    """The windows that pass :func:`find_game_window_info`'s checks, best first."""
    wanted = (title or "").strip()
    if not wanted:
        return []
    valid: list[tuple[tuple[int, int], int, int, WindowInfo]] = []
    for win in _enum_windows(wanted):
        if _title_match(win.title, wanted) is None:
            continue
        why = _rejection(win, classes)
        if why is not None:
            _report_rejected(win, why)
            continue
        left, top, right, bottom = win.rect
        area = max(0, right - left) * max(0, bottom - top)
        valid.append((_tier(win, wanted), -area, win.hwnd, win))
    valid.sort(key=lambda row: row[:3])
    return [row[3] for row in valid]


def find_game_window_info(title: str, *, classes: Collection[str] = GAME_WINDOW_CLASSES) -> WindowInfo | None:
    """Locate the game window using read-only Win32 queries.

    Only a window titled exactly ``title`` (or the same title in other case) whose
    class is one of ``classes`` counts.  The game is a Unity player
    (``UnityWndClass``); a browser or chat window (:data:`FOREIGN_WINDOW_CLASSES`)
    never counts, whatever its title.  A window with the right title that is turned
    down is logged once, with its class and process id.  Among the valid windows the
    exact title wins over other case, then visible windows, then the larger window.

    Args:
        title: The window title (normally ``Config.window_title``).
        classes: The window classes the game window may have.

    Returns:
        The best valid window, or ``None`` when there is none.  On non-Windows
        platforms always ``None``.
    """
    valid = _valid_windows(title, classes)
    if not valid:
        log.debug("No game window titled %r found", title)
        return None
    best = valid[0]
    log.debug("Found window %s rect=%s visible=%s (%d candidate(s))",
              best.describe(), best.rect, best.visible, len(valid))
    return best


def find_game_window(title: str) -> tuple[int, Rect] | None:
    """``(hwnd, (left, top, right, bottom))`` of the game window, or ``None``.

    See :func:`find_game_window_info` for which windows count.
    """
    win = find_game_window_info(title)
    return None if win is None else (win.hwnd, win.rect)


def window_switch_reason(hwnd: int, title: str, *, classes: Collection[str] = GAME_WINDOW_CLASSES) -> str | None:
    """Why capture should leave window ``hwnd``, or ``None`` while it is still the best valid match.

    Capture leaves a window that is gone or no longer passes the checks of
    :func:`find_game_window_info`, and one that another valid window beats on title
    or visibility.  Size does not count here, so minimising one of two game clients
    never moves the capture to the other.
    """
    valid = _valid_windows(title, classes)
    current = next((win for win in valid if win.hwnd == hwnd), None)
    if current is None:
        return "is gone or no longer a valid game window"
    best = valid[0]
    wanted = title.strip()
    if best.hwnd != hwnd and _tier(best, wanted) < _tier(current, wanted):
        return f"is no longer the best match ({best.describe()} is)"
    return None


# ---------------------------------------------------------------------------
# Windows Graphics Capture source
# ---------------------------------------------------------------------------


class WgcWindowSource:
    """Capture the game window with Windows Graphics Capture.

    ``windows_capture.WindowsCapture.start()`` blocks for the whole session, so
    it runs on a daemon thread.  Each delivered frame is copied out of the
    library's zero-copy buffer (as BGR) and kept under a lock; :meth:`latest`
    hands out a further copy so callers can never alias the shared buffer.

    Windows is asked for no more frames than ``max_fps`` (``minimum_update_interval``):
    on a 4K, 240 Hz screen every delivered frame is a 33 MB read-back from the GPU and a
    Python callback, even when the callback drops it, and those callbacks stalled the
    app's animations.  Once :meth:`latest_region` has been asked for a crop, each frame
    copies only that rectangle (about 2 MB instead of 25 MB); :meth:`latest` then fetches
    one whole frame on demand (the crop picker).

    The library is given the hwnd that :func:`find_game_window_info` validated, never a
    title (its own title lookup is a substring match).  Every :data:`WINDOW_CHECK_S`,
    :meth:`latest` and :meth:`latest_region` re-check that this window is still the best
    valid match; when it is not (gone, or a better game window appeared) the session ends
    like a closed window, so the caller restarts capture on the right one instead of
    staying on a stale window.

    Args:
        window_title: Title of the window to capture.
        max_fps: If given, frames arriving faster than this are dropped in the
            callback (cheap throttling; the compositor still delivers at its
            own rate but we skip the copy).
        start_timeout_s: How long :meth:`start` waits for either the first
            frame or an early failure before returning.
    """

    def __init__(
        self,
        window_title: str,
        *,
        max_fps: float | None = None,
        start_timeout_s: float = 1.5,
    ) -> None:
        self._title = window_title
        self._min_interval = (1.0 / max_fps) if max_fps and max_fps > 0 else 0.0
        self._start_timeout = start_timeout_s
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._region: tuple[int, int, int, int] | None = None  #: crop copied out of each frame
        self._region_frame: np.ndarray | None = None
        self._region_error: str | None = None  #: the crop does not fit the frame
        self._region_cut: tuple[int, int, int, int] | None = None  #: the crop _region_frame/_error are for
        self._frame_at = 0.0  #: when _frame (the last whole frame) was copied (monotonic)
        self._full_wanted = threading.Event()  #: the next frame is copied whole (region mode)
        self._full_ready = threading.Event()
        self._frame_count = 0
        self._last_kept = 0.0
        self._frame_event = threading.Event()
        self._control: Any | None = None  # InternalCaptureControl from the callback
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._stop_requested = False
        self._closed = False
        self._next_check = 0.0  #: when the window is next re-checked (monotonic)
        self.hwnd: int | None = None
        self.rect: Rect | None = None
        self.window: WindowInfo | None = None  #: the window being captured (title, class, pid)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Locate the window and start the capture thread.

        Raises:
            WindowNotFoundError: no game window with the configured title exists
                (see :func:`find_game_window_info`).
            CaptureError: the capture library is missing or failed to start.
        """
        if self._thread is not None and self._thread.is_alive():
            log.debug("WgcWindowSource already running")
            return
        try:
            from windows_capture import WindowsCapture
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise CaptureError("The 'windows-capture' package is not installed") from exc

        win = find_game_window_info(self._title)
        if win is None:
            raise WindowNotFoundError(f"Game window {self._title!r} not found (is the game running?)")
        self.window, self.hwnd, self.rect = win, win.hwnd, win.rect
        log.info("Capturing window %s rect=%s via Windows Graphics Capture", win.describe(), win.rect)

        self._stop_requested = False
        self._closed = False
        self._error = None
        self._next_check = time.monotonic() + WINDOW_CHECK_S
        self._frame_event.clear()
        # Only the validated hwnd: the library's own title lookup is a substring match.
        kwargs = self.capture_kwargs()
        try:
            cap = WindowsCapture(**kwargs)
        except TypeError:  # an older windows-capture without minimum_update_interval
            kwargs.pop("minimum_update_interval", None)
            cap = WindowsCapture(**kwargs)
        self._kwargs = kwargs

        @cap.event
        def on_frame_arrived(frame: Any, control: Any) -> None:  # noqa: ANN401
            self._on_frame(frame, control)

        @cap.event
        def on_closed() -> None:
            self._closed = True
            log.warning("Capture session closed (game window closed?)")

        self._thread = threading.Thread(target=self._run, args=(cap,), name="mnmparse-wgc", daemon=True)
        self._thread.start()
        # Fail fast: the native start() raises immediately when the window cannot be
        # captured, so wait briefly for either that error or the first frame.
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if self._error is not None:
                self._raise_thread_error()
            if self._frame_event.is_set() or not self._thread.is_alive():
                break
            time.sleep(0.02)
        if self._error is not None:
            self._raise_thread_error()

    def capture_kwargs(self) -> dict[str, Any]:
        """Arguments for ``windows_capture.WindowsCapture`` (see the class docstring)."""
        kwargs: dict[str, Any] = {"cursor_capture": False, "draw_border": False, "window_hwnd": self.hwnd}
        if self._min_interval and _supports_min_update_interval():
            kwargs["minimum_update_interval"] = max(1, int(self._min_interval * 1000))
        return kwargs

    def _run(self, cap: Any) -> None:  # noqa: ANN401
        """Thread body: blocks inside the library until the session stops."""
        try:
            try:
                cap.start()
            except Exception as exc:  # noqa: BLE001
                if "minimum update interval" not in str(exc).lower():
                    raise
                # Windows refused the frame-rate cap (older than 11 24H2): capture without it
                log.info("Capture without a minimum update interval: %s", exc)
                kwargs = dict(getattr(self, "_kwargs", {}) or self.capture_kwargs())
                kwargs.pop("minimum_update_interval", None)
                cap = self._rebuild(kwargs)
                cap.start()
            log.debug("Capture thread finished")
        except BaseException as exc:  # noqa: BLE001 - surface anything from native code
            self._error = exc
            log.error("Capture thread failed: %s", exc)
        finally:
            self._frame_event.set()  # wake any waiter so it can see the error/close

    def _rebuild(self, kwargs: dict[str, Any]) -> Any:  # noqa: ANN401
        """A new library capture object with ``kwargs`` and this source's handlers."""
        from windows_capture import WindowsCapture

        cap = WindowsCapture(**kwargs)

        @cap.event
        def on_frame_arrived(frame: Any, control: Any) -> None:  # noqa: ANN401
            self._on_frame(frame, control)

        @cap.event
        def on_closed() -> None:
            self._closed = True
            log.warning("Capture session closed (game window closed?)")

        return cap

    def _raise_thread_error(self) -> None:
        err = self._error
        text = str(err)
        if "find" in text.lower() and "window" in text.lower():
            raise WindowNotFoundError(f"Game window {self._title!r} could not be captured: {text}") from err
        raise CaptureError(f"Windows Graphics Capture failed: {text}") from err

    def _check_window(self) -> None:
        """Every :data:`WINDOW_CHECK_S`: end the session when its window is no longer the best match.

        The session then reads as closed (:attr:`is_running` false, no frames), which
        the app's capture loop treats as a lost window: it stops this source and looks
        the game window up again.
        """
        if self.hwnd is None or self._closed:
            return
        now = time.monotonic()
        if now < self._next_check:
            return
        self._next_check = now + WINDOW_CHECK_S
        try:
            reason = window_switch_reason(self.hwnd, self._title)
        except Exception as exc:  # noqa: BLE001 - a failed lookup must not end the capture
            log.debug("Window re-check failed: %s", exc)
            return
        if reason is None:
            return
        captured = self.window.describe() if self.window is not None else f"hwnd={self.hwnd}"
        log.warning("Captured window %s %s; ending this capture session", captured, reason)
        self._closed = True
        self._stop_requested = True  # the next frame callback stops the library session

    def _on_frame(self, frame: Any, control: Any) -> None:  # noqa: ANN401
        """Library callback: copy the newest frame out as BGR."""
        if self._control is None:
            self._control = control
        if self._stop_requested:
            control.stop()
            return
        now = time.monotonic()
        wanted = self._full_wanted.is_set()  # a caller waits for one whole frame: no throttle
        full = wanted or self._region is None
        if not wanted and self._min_interval and self._frame_count and (now - self._last_kept) < self._min_interval:
            return  # throttle: skip this frame without copying it
        try:
            view = frame.frame_buffer[:, :, :3]  # BGRA -> BGR view (no copy yet)
            region = self._region
            if full:
                bgr = view.copy()  # private BGR copy of the whole frame
                part, err = self._cut(bgr, region)
            else:
                bgr = None
                part, err = self._cut(view, region)  # copies the crop only
        except Exception as exc:  # noqa: BLE001 - never let the native callback see an exception
            log.warning("Could not read captured frame: %s", exc)
            return
        with self._lock:
            if bgr is not None:
                self._frame, self._frame_at = bgr, now
            if region == self._region:  # the crop may have changed while this frame was cut
                self._region_frame, self._region_error, self._region_cut = part, err, region
            self._frame_count += 1
            self._last_kept = now
        if bgr is not None:
            self._full_wanted.clear()
            self._full_ready.set()
        self._frame_event.set()

    @staticmethod
    def _cut(view: np.ndarray, region: tuple[int, int, int, int] | None) -> tuple[np.ndarray | None, str | None]:
        if region is None:
            return None, None
        try:
            return crop_frame(view, region), None
        except ValueError as exc:
            return None, str(exc)

    def stop(self) -> None:
        """Ask the capture session to stop and join the thread (with a timeout)."""
        self._stop_requested = True
        control = self._control
        if control is not None:
            try:
                control.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("control.stop() raised: %s", exc)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():
                log.warning("Capture thread did not stop within 2 s; it is a daemon and ends with the process")
        self._thread = None
        log.info("Capture stopped after %d frame(s)", self._frame_count)

    # -- frames ------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True while the capture thread is alive and the session is not closed."""
        return self._thread is not None and self._thread.is_alive() and not self._closed

    @property
    def frame_count(self) -> int:
        """Number of frames kept so far (after throttling)."""
        return self._frame_count

    def latest(self) -> np.ndarray | None:
        """Return a copy of the newest frame (HxWx3 BGR) or ``None``.

        Returns ``None`` once the session has closed (window gone, or no longer
        the best match) so callers do not keep OCRing a stale frame.
        """
        self._check_window()
        if self._closed:
            return None
        if self._region is not None and self.is_running:
            # region mode keeps only the crop: have the next frame copied whole for this caller
            self._full_ready.clear()
            self._full_wanted.set()
            self._full_ready.wait(timeout=1.0)
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def latest_region(self, crop: tuple[int, int, int, int]) -> np.ndarray | None:
        """The ``(left, top, right, bottom)`` crop of the newest frame (BGR), or ``None``.

        From the first call on, frames copy only this rectangle (see the class docstring);
        a different crop switches to it.

        Raises:
            ValueError: the crop does not fit the frame (as :func:`crop_frame`).
        """
        self._check_window()
        if self._closed:
            return None
        crop = tuple(int(v) for v in crop)  # type: ignore[assignment]
        if crop != self._region:
            with self._lock:
                self._region = crop
                frame, self._region_frame, self._region_error, self._region_cut = self._frame, None, None, None
                fresh = time.monotonic() - self._frame_at <= max(2.0 * self._min_interval, 0.5)
            # Cut the new crop from the last whole frame only when it is current: in region mode
            # that frame can be minutes old, and OCRing it would replay old chat as new lines.
            return crop_frame(frame, crop) if frame is not None and fresh else None
        with self._lock:
            if self._region_cut != crop:
                return None  # the next frame brings this crop
            part, err = self._region_frame, self._region_error
        if err is not None:
            raise ValueError(err)
        return None if part is None else part.copy()

    def wait_for_frame(self, timeout_s: float = 5.0) -> np.ndarray | None:
        """Block until a frame is available (or ``timeout_s`` elapses).

        Returns:
            A copy of the frame, or ``None`` on timeout / closed session.

        Raises:
            CaptureError: the capture thread died with an error.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if self._error is not None:
                self._raise_thread_error()
            frame = self.latest()
            if frame is not None:
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._closed:
                return None
            self._frame_event.clear()
            self._frame_event.wait(min(remaining, 0.25))


# ---------------------------------------------------------------------------
# mss (desktop region) fallback
# ---------------------------------------------------------------------------


class MssRegionSource:
    """Grab the game window's rectangle from the desktop with ``mss``.

    The window is looked up again (read-only, as :func:`find_game_window_info`) on
    every grab so a moved window keeps working.  While no valid game window exists
    there are no frames: the desktop where the game used to be is not grabbed.  Note
    that unlike WGC this sees whatever is on top of the game window.
    """

    def __init__(self, window_title: str) -> None:
        self._title = window_title
        self._rect: Rect | None = None
        self._warned_missing = False
        self.hwnd: int | None = None
        self.window: WindowInfo | None = None  #: the window being grabbed (title, class, pid)

    def start(self) -> None:
        """Verify ``mss`` is usable and that the window exists.

        Raises:
            WindowNotFoundError: no game window with the configured title exists
                (see :func:`find_game_window_info`).
            CaptureError: ``mss`` is not installed.
        """
        try:
            import mss
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise CaptureError("The 'mss' package is not installed") from exc
        # Constructing mss once makes *this* process DPI-aware so that the
        # window rectangle below is reported in physical pixels.
        with mss.mss():
            pass
        win = find_game_window_info(self._title)
        if win is None:
            raise WindowNotFoundError(f"Game window {self._title!r} not found (is the game running?)")
        self.window, self.hwnd, self._rect = win, win.hwnd, win.rect
        self._warned_missing = False
        log.info("Capturing window %s rect=%s via mss desktop grab", win.describe(), win.rect)

    def _current_rect(self) -> Rect | None:
        win = find_game_window_info(self._title)
        if win is None:
            # No grab of the old rectangle: with the game gone it shows some other window.
            if not self._warned_missing:
                log.warning("Game window %r disappeared; no frames until it is back", self._title)
                self._warned_missing = True
            return None
        if win.hwnd != self.hwnd:
            log.info("Game window is now %s rect=%s", win.describe(), win.rect)
        self.window, self.hwnd, self._rect = win, win.hwnd, win.rect
        self._warned_missing = False
        return self._rect

    def latest(self) -> np.ndarray | None:
        """Grab the window rectangle now and return it as HxWx3 BGR, or ``None``."""
        import mss
        from mss.exception import ScreenShotError

        rect = self._current_rect()
        if rect is None:
            return None
        left, top, right, bottom = rect
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0 or left <= -32000 or top <= -32000:
            log.debug("Window rectangle %s is empty or minimised; no frame", rect)
            return None
        try:
            with mss.mss() as sct:
                shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        except ScreenShotError as exc:
            log.warning("mss grab failed for %s: %s", rect, exc)
            return None
        bgra = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return bgra[:, :, :3].copy()

    def wait_for_frame(self, timeout_s: float = 5.0) -> np.ndarray | None:
        """Return a frame, retrying until ``timeout_s`` elapses."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            frame = self.latest()
            if frame is not None or time.monotonic() >= deadline:
                return frame
            time.sleep(0.1)

    def stop(self) -> None:
        """Nothing to release: each grab opens and closes its own handles."""
        log.info("mss capture stopped")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _supports_min_update_interval() -> bool:
    """Windows 11 24H2 (build 26100) and later honour GraphicsCaptureSession.MinUpdateInterval."""
    try:
        return sys.getwindowsversion().build >= 26100  # type: ignore[attr-defined]
    except AttributeError:  # not Windows
        return False


def make_source(cfg: Config) -> FrameSource:
    """Build the frame source selected by ``cfg.capture_backend``.

    Raises:
        ValueError: unknown backend name.
    """
    backend = cfg.capture_backend.lower()
    if backend == "wgc":
        # Keep a little headroom over the loop rate so latest() is fresh.
        return WgcWindowSource(cfg.window_title, max_fps=max(cfg.fps, 0.5) * 2.0)
    if backend == "mss":
        return MssRegionSource(cfg.window_title)
    raise ValueError(f"Unknown capture_backend {cfg.capture_backend!r}; expected 'wgc' or 'mss'")


def crop_frame(frame: np.ndarray, crop: tuple[int, int, int, int]) -> np.ndarray:
    """Return the ``(left, top, right, bottom)`` region of ``frame`` as a contiguous array.

    The crop is clamped to the frame bounds.

    Raises:
        ValueError: the clamped region is empty.
    """
    height, width = frame.shape[:2]
    left, top, right, bottom = (int(v) for v in crop)
    left, right = max(0, min(left, width)), max(0, min(right, width))
    top, bottom = max(0, min(top, height)), max(0, min(bottom, height))
    if right <= left or bottom <= top:
        raise ValueError(f"Crop {crop!r} is empty for a {width}x{height} frame")
    return np.ascontiguousarray(frame[top:bottom, left:right])

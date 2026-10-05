"""Window lookup from the second log audit: with the game closed, the app captured a browser
tab and another window whose titles merely contained the game's title.

Only a window titled exactly like the game (or the same in other case) with the game's
Unity window class counts; browser and chat windows never do.  A running capture re-checks
its window and ends when the window is gone or a better game window appeared.  No real
windows are needed: the read-only enumeration is replaced by fake windows.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mnmparse import capture  # noqa: E402
from mnmparse.capture import (  # noqa: E402
    MssRegionSource,
    WgcWindowSource,
    WindowInfo,
    WindowNotFoundError,
    find_game_window,
    find_game_window_info,
    window_switch_reason,
)

TITLE = "Monsters and Memories"


def _win(hwnd: int, title: str = TITLE, class_name: str = "UnityWndClass", *, pid: int = 4100,
         size: int = 1000, visible: bool = True) -> WindowInfo:
    return WindowInfo(hwnd, title, class_name, pid, (0, 0, size, size), visible)


def _windows(*wins: WindowInfo) -> Any:
    """Replace the read-only enumeration with ``wins`` (whatever title is asked for)."""
    return mock.patch.object(capture, "_enum_windows", return_value=list(wins))


class _Control:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _frame(value: int = 5) -> SimpleNamespace:
    buf = np.zeros((200, 300, 4), dtype=np.uint8)
    buf[:] = value
    return SimpleNamespace(frame_buffer=buf)


class WindowLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        capture._reported.clear()

    def test_browser_tab_with_the_exact_title_is_rejected(self) -> None:
        for cls in ("MozillaWindowClass", "Chrome_WidgetWin_1", "Chrome_WidgetWin_0"):
            with self.subTest(cls=cls), _windows(_win(31, class_name=cls, size=3000)):
                self.assertIsNone(find_game_window_info(TITLE))
                self.assertIsNone(find_game_window(TITLE))
                # never, even when a caller lists the browser's class as a game class
                self.assertIsNone(find_game_window_info(TITLE, classes={cls, "UnityWndClass"}))

    def test_substring_title_is_rejected(self) -> None:
        wins = (
            _win(32, f"{TITLE} Wiki - Mozilla Firefox", "UnityWndClass"),
            _win(33, f"Quest guide | {TITLE}", "UnityWndClass"),
            _win(34, f"{TITLE} ", "UnityWndClass"),
        )
        with _windows(*wins):
            self.assertIsNone(find_game_window_info(TITLE))

    def test_the_exact_unity_window_is_accepted(self) -> None:
        game = _win(40, pid=5200, size=800)
        wins = (
            _win(41, class_name="MozillaWindowClass", size=3000),  # bigger, same title: a browser
            _win(42, f"{TITLE} - Discord", "Chrome_WidgetWin_1", size=3000),
            game,
        )
        with _windows(*wins):
            found = find_game_window_info(TITLE)
            self.assertEqual(found, game)
            self.assertEqual((found.title, found.class_name, found.pid), (TITLE, "UnityWndClass", 5200))
            self.assertEqual(find_game_window(TITLE), (40, (0, 0, 800, 800)))

    def test_case_insensitive_exact_title_is_accepted(self) -> None:
        other_case = _win(50, TITLE.upper(), size=2000)
        with _windows(other_case):
            self.assertEqual(find_game_window_info(TITLE), other_case)
            self.assertEqual(find_game_window_info(f"  {TITLE.lower()} "), other_case)
        exact = _win(51, size=500)
        with _windows(other_case, exact):  # the exact title beats other case, whatever the size
            self.assertEqual(find_game_window_info(TITLE), exact)

    def test_visible_then_larger_window_wins(self) -> None:
        hidden, small, big = _win(60, size=3000, visible=False), _win(61, size=400), _win(62, size=900)
        with _windows(hidden, small, big):
            self.assertEqual(find_game_window_info(TITLE).hwnd, 62)

    def test_unexpected_class_is_not_captured_and_logged_once(self) -> None:
        odd = _win(70, class_name="SomeLauncherClass", pid=6100)
        with _windows(odd):
            with self.assertLogs("mnmparse.capture", level="INFO") as logs:
                self.assertIsNone(find_game_window_info(TITLE))
                self.assertIsNone(find_game_window_info(TITLE))  # the app looks every 2 s
        rejects = [r for r in logs.records if "Not capturing" in r.getMessage()]
        self.assertEqual(len(rejects), 1)
        self.assertEqual(rejects[0].levelname, "WARNING")
        self.assertIn("SomeLauncherClass", rejects[0].getMessage())
        self.assertIn("pid 6100", rejects[0].getMessage())

    def test_browser_with_the_title_is_logged_once_at_info(self) -> None:
        with _windows(_win(71, class_name="MozillaWindowClass")):
            with self.assertLogs("mnmparse.capture", level="INFO") as logs:
                find_game_window_info(TITLE)
                find_game_window_info(TITLE)
        rejects = [r for r in logs.records if "Not capturing" in r.getMessage()]
        self.assertEqual([r.levelname for r in rejects], ["INFO"])
        self.assertIn("browser", rejects[0].getMessage())

    def test_empty_title_never_matches(self) -> None:
        with _windows(_win(72, "")):
            self.assertIsNone(find_game_window_info(""))
            self.assertIsNone(find_game_window_info("   "))


class WindowSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        capture._reported.clear()

    def test_still_the_best_match(self) -> None:
        with _windows(_win(80)):
            self.assertIsNone(window_switch_reason(80, TITLE))

    def test_vanished_window(self) -> None:
        with _windows():
            self.assertIn("gone", window_switch_reason(80, TITLE))
        with _windows(_win(81)):  # another game window, the captured one is gone
            self.assertIn("gone", window_switch_reason(80, TITLE))

    def test_a_better_game_window(self) -> None:
        with _windows(_win(82, visible=False), _win(83)):
            self.assertIn("hwnd=83", window_switch_reason(82, TITLE))
        with _windows(_win(84, TITLE.lower()), _win(85)):
            self.assertIn("hwnd=85", window_switch_reason(84, TITLE))

    def test_size_alone_never_switches(self) -> None:
        # two game clients, the captured one minimised: stay on it
        with _windows(_win(86, size=100), _win(87, size=3000)):
            self.assertIsNone(window_switch_reason(86, TITLE))


class WgcRecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        capture._reported.clear()
        self.src = WgcWindowSource(TITLE, max_fps=12.0)
        self.src._thread = SimpleNamespace(is_alive=lambda: True)  # "running" without a session
        self.src.hwnd = 90
        self.src.window = _win(90)

    def feed(self, value: int = 5) -> None:
        self.src._last_kept = 0.0
        self.src._on_frame(_frame(value), _Control())

    def test_a_vanished_hwnd_ends_the_session(self) -> None:
        self.feed()
        self.src._next_check = 0.0
        with _windows(_win(91)):  # the captured window is gone; another game window exists
            with self.assertLogs("mnmparse.capture", level="WARNING") as logs:
                self.assertIsNone(self.src.latest_region((0, 0, 100, 100)))
        self.assertIn("ending this capture session", logs.output[0])
        self.assertFalse(self.src.is_running, "the app's capture loop restarts capture on this")
        self.assertIsNone(self.src.latest())
        control = _Control()
        self.src._on_frame(_frame(), control)
        self.assertTrue(control.stopped, "the library session is stopped on the next frame")

    def test_a_better_window_ends_the_session(self) -> None:
        self.src.window = _win(90, visible=False)
        self.feed()
        self.src._next_check = 0.0
        with _windows(_win(90, visible=False), _win(92)), self.assertLogs("mnmparse.capture", level="WARNING"):
            self.assertIsNone(self.src.latest())
        self.assertFalse(self.src.is_running)

    def test_the_best_window_keeps_capturing(self) -> None:
        self.feed(7)
        self.src._next_check = 0.0
        with _windows(_win(90)):
            part = self.src.latest_region((0, 0, 100, 100))
        self.assertEqual(int(part[0, 0, 0]), 7)
        self.assertTrue(self.src.is_running)

    def test_the_check_runs_every_window_check_s(self) -> None:
        self.feed()
        self.src._next_check = 0.0
        with _windows(_win(90)) as enum:
            for _ in range(5):
                self.src.latest_region((0, 0, 100, 100))
        self.assertEqual(enum.call_count, 1)
        self.assertGreater(self.src._next_check, 0.0)

    def test_no_check_before_a_window_is_chosen(self) -> None:
        src = WgcWindowSource(TITLE)
        with _windows() as enum:
            self.assertIsNone(src.latest())
        enum.assert_not_called()


class _FakeCapture:
    """``windows_capture.WindowsCapture`` stand-in that records its arguments."""

    made: list[dict] = []

    def __init__(self, **kwargs: object) -> None:
        _FakeCapture.made.append(kwargs)

    def event(self, handler: object) -> object:
        return handler

    def start(self) -> None:
        pass


class WgcStartTests(unittest.TestCase):
    def setUp(self) -> None:
        capture._reported.clear()
        _FakeCapture.made = []
        fake = ModuleType("windows_capture")
        fake.WindowsCapture = _FakeCapture  # type: ignore[attr-defined]
        patcher = mock.patch.dict(sys.modules, {"windows_capture": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_library_captures_exactly_the_validated_hwnd(self) -> None:
        wins = (_win(100, f"{TITLE} Wiki - Mozilla Firefox", "MozillaWindowClass"), _win(101, pid=7300))
        src = WgcWindowSource(TITLE, start_timeout_s=0.2)
        with _windows(*wins), self.assertLogs("mnmparse.capture", level="INFO") as logs:
            src.start()
        src.stop()
        self.assertEqual(_FakeCapture.made[0]["window_hwnd"], 101)
        self.assertNotIn("window_name", _FakeCapture.made[0])
        self.assertNotIn("monitor_index", _FakeCapture.made[0])
        started = next(line for line in logs.output if "Capturing window" in line)
        for part in (repr(TITLE), "class UnityWndClass", "pid 7300", "hwnd=101"):
            self.assertIn(part, started)

    def test_only_a_browser_tab_means_no_window(self) -> None:
        src = WgcWindowSource(TITLE, start_timeout_s=0.2)
        with _windows(_win(102, class_name="MozillaWindowClass")), self.assertRaises(WindowNotFoundError):
            src.start()
        self.assertEqual(_FakeCapture.made, [])


class MssLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        capture._reported.clear()

    def test_no_grab_of_the_old_rectangle_once_the_window_is_gone(self) -> None:
        src = MssRegionSource(TITLE)
        src.hwnd, src._rect = 110, (0, 0, 800, 600)
        with _windows(_win(111, class_name="MozillaWindowClass")):
            with self.assertLogs("mnmparse.capture", level="WARNING") as logs:
                self.assertIsNone(src._current_rect())
                self.assertIsNone(src._current_rect())
        self.assertEqual(len(logs.records), 1, "warned once")

    def test_follows_the_valid_game_window(self) -> None:
        src = MssRegionSource(TITLE)
        src.hwnd, src._rect = 110, (0, 0, 800, 600)
        with _windows(_win(112, size=700)):
            self.assertEqual(src._current_rect(), (0, 0, 700, 700))
        self.assertEqual(src.hwnd, 112)


try:
    import PySide6  # noqa: F401

    HAVE_QT = True
except ImportError:  # pragma: no cover - environment dependent
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class EngineRestartTests(unittest.TestCase):
    """The app's capture loop treats a source that ended this way as a lost window."""

    def test_stale_source_counts_as_a_lost_window(self) -> None:
        from mnmparse.app.engine import Engine
        from mnmparse.config import Config

        capture._reported.clear()
        src = WgcWindowSource(TITLE)
        src._thread = SimpleNamespace(is_alive=lambda: True)
        src.hwnd, src.window, src._next_check = 120, _win(120), 0.0
        engine = SimpleNamespace(_window_found=True)  # only the flag _window_lost touches
        with _windows(), self.assertLogs("mnmparse", level="WARNING"):
            self.assertIsNone(src.latest_region((0, 0, 10, 10)))
            self.assertTrue(Engine._window_lost(engine, src, 0.0, 0.0, Config()))
        self.assertFalse(engine._window_found)


if __name__ == "__main__":
    unittest.main()

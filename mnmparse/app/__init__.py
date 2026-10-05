"""PNUT M&M: the PySide6 desktop application built on the ``mnmparse`` pipeline.

The package contains only our own windows (main window, overlay, tray icon).  It never
touches the game process: the pipeline modules it drives (``capture``, ``ocr``, ...)
are the same passive, read-only ones the command-line tool uses (see SPEC.md section 1
and APP_SPEC.md section 1).

Run it with ``python -m mnmparse.app`` (or the console-less launcher ``PNUT M&M.cmd``).
"""

from __future__ import annotations

from mnmparse import __version__ as _pipeline_version

APP_NAME: str = "PNUT M&M"
"""Display name used for the window title, QApplication name and the tray icon."""

APP_VERSION: str = _pipeline_version
"""Version string shown on the About page; tracks the pipeline package version."""

ORGANIZATION: str = "mnmparse"
"""QSettings organisation name (settings live under ``mnmparse/MnM Parser``)."""

APP_TAGLINE: str = "Parses, Navigation, und Timers"
SETTINGS_APP_NAME: str = "MnM Parser"
"""Keep the established registry namespace so upgrading preserves every UI preference."""

__all__ = ["APP_NAME", "APP_TAGLINE", "APP_VERSION", "ORGANIZATION", "SETTINGS_APP_NAME"]

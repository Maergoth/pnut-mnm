"""Small Windows WebView2 boundary; importing this module never loads native code."""
from __future__ import annotations

from pathlib import Path
import sys

READY, LOAD_START, LOAD_DONE, URL_CHANGED = 1, 2, 3, 4
NAV_REQUEST, NEW_WINDOW, PROCESS_FAILED, JS_RESULT, ERROR = 5, 6, 7, 8, 9


class RuntimeUnavailable(RuntimeError):
    pass


def library_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "atlas" / "pnut_atlas_webview.dll"
    return Path(__file__).resolve().parents[2] / "build" / "atlas_webview" / "pnut_atlas_webview.dll"


class NativeHost:
    """Own one native child controller, with callbacks confined to the GUI thread."""

    def __init__(self, hwnd: int, storage: Path, callback):
        import ctypes

        self._closed = False
        self._handle = None
        self._callback_target = callback
        if sys.platform != "win32":
            raise RuntimeUnavailable("Interactive maps require Microsoft WebView2 on Windows.")
        # Use PNUT's dedicated profile and ordinary shared runtime, rather than
        # inheriting development overrides for a different application's browser.
        import os
        for atlas_env_key in tuple(os.environ):
            if atlas_env_key.startswith("WEBVIEW2_"):
                os.environ.pop(atlas_env_key, None)
        self._dll = ctypes.WinDLL(str(library_path()))
        pointer, string, integer = ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int32
        self._callback_type = ctypes.WINFUNCTYPE(integer, pointer, integer, integer, string)
        self._callback = self._callback_type(self._event)
        declarations = {
            "create": ([pointer, string, self._callback_type, pointer], pointer),
            "close": ([pointer], None),
            "navigate": ([pointer, string], integer),
            "load_html": ([pointer, string], integer),
            "eval_js": ([pointer, string, integer], integer),
            "resize": ([pointer], integer),
            "set_visible": ([pointer, integer], integer),
            "suspend": ([pointer], integer),
            "resume": ([pointer], integer),
            "stop": ([pointer], integer),
            "runtime_version": ([string, integer], integer),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self._dll, "pnut_atlas_" + name)
            function.argtypes, function.restype = arguments, result
        count = self._dll.pnut_atlas_runtime_version(None, 0)
        if not 1 < count < 256:
            raise RuntimeUnavailable("Microsoft WebView2 is needed for interactive maps.")
        version = ctypes.create_unicode_buffer(count)
        if self._dll.pnut_atlas_runtime_version(version, count) != count:
            raise RuntimeUnavailable("Microsoft WebView2 could not be detected.")
        self.runtime_version = version.value
        storage = Path(storage).resolve()
        storage.mkdir(parents=True, exist_ok=True)
        self._handle = self._dll.pnut_atlas_create(pointer(hwnd), str(storage), self._callback, None)
        if not self._handle:
            raise RuntimeError("The interactive map could not start.")

    def _event(self, _context, event, value, data):
        if self._closed:
            return 0
        try:
            return int(self._callback_target(event, value, data or "") or 0)
        except Exception:
            # No Python exception may escape a native COM callback.
            import logging
            logging.getLogger(__name__).exception("Atlas callback failed")
            return 0

    def _call(self, name, *arguments):
        if self._closed or not self._handle:
            return
        result = getattr(self._dll, "pnut_atlas_" + name)(self._handle, *arguments)
        if result < 0:
            raise RuntimeError(f"Atlas {name} failed (0x{result & 0xffffffff:08x})")

    def navigate(self, url: str):
        self._call("navigate", url)

    def load_html(self, html: str):
        self._call("load_html", html)

    def evaluate(self, script: str, request_id: int):
        self._call("eval_js", script, request_id)

    def resize(self):
        self._call("resize")

    def set_visible(self, visible: bool):
        self._call("set_visible", int(visible))

    def suspend(self):
        self._call("suspend")

    def stop(self):
        self._call("stop")

    def resume(self):
        self._call("resume")

    def close(self, *_args):
        if self._closed:
            return
        self._closed = True
        self._callback_target = None
        if self._handle:
            handle, self._handle = self._handle, None
            self._dll.pnut_atlas_close(handle)

"""Offline renderer diagnostic; the main startup check launches this hidden child."""
from __future__ import annotations

import json
import os
import base64
from pathlib import Path
import sys
import traceback


def run_native_smoke(report_path: Path, storage: Path) -> int:
    report = {"ok": False, "renderer": False, "javascript": False, "network": False}
    host = window = None
    try:
        # A WebView2 child needs a real HWND; offscreen Qt has no Windows handle.
        # This diagnostic creates its own hidden native window and never shows it.
        os.environ["QT_QPA_PLATFORM"] = "windows"
        from PySide6.QtCore import QEventLoop, QTimer, Qt
        from PySide6.QtWidgets import QApplication, QWidget
        from mnmparse.app import atlas_native as native

        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        window = QWidget()
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        window.resize(320, 200)
        loop, timer = QEventLoop(), QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        errors, results = [], []
        html = ("<html><head><meta http-equiv='Content-Security-Policy' "
                "content=\"default-src 'none'; script-src 'unsafe-inline'\"></head>"
                "<body id='result'>PNUT Atlas renderer</body></html>")
        inline_url = "data:text/html;charset=utf-8;base64," + base64.b64encode(html.encode()).decode()

        def callback(event, value, data):
            if event == native.NAV_REQUEST:
                return int(data in ("about:blank", inline_url))
            if event == native.READY:
                host.load_html(html)
            elif event == native.LOAD_DONE:
                if value:
                    host.evaluate("document.getElementById('result').textContent + ':' + (6 * 7)", 1)
                else:
                    errors.append(data or "Synthetic renderer navigation failed")
                    loop.quit()
            elif event == native.JS_RESULT and value == 1:
                results.append(json.loads(data))
                loop.quit()
            elif event in (native.ERROR, native.PROCESS_FAILED):
                errors.append(data or "Renderer process failed")
                loop.quit()
            return 0

        host = native.NativeHost(int(window.winId()), storage, callback)
        timer.start(8_000)
        loop.exec()
        timer.stop()
        if errors or results != ["PNUT Atlas renderer:42"]:
            raise RuntimeError(errors[0] if errors else "Native Atlas renderer timed out")
        if any(name.startswith("PySide6.QtWebEngine") for name in sys.modules):
            raise RuntimeError("Atlas unexpectedly loaded a bundled browser engine")
        report.update(ok=True, renderer=True, javascript=True)
    except Exception as exc:
        report.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        if host is not None:
            host.close()
        if window is not None:
            window.close()
        from mnmparse.storage import atomic_json
        atomic_json(report_path, report)
    return 0 if report["ok"] else 1

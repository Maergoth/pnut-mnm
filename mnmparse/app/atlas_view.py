"""Lazy MnM Atlas view using Windows' shared WebView2 runtime."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

_LOAD_TIMEOUT_MS = 30_000
WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/en-us/microsoft-edge/webview2/"


def _atlas_origin(url: QUrl) -> bool:
    from mnmparse.atlas import is_atlas_url
    return url.isValid() and is_atlas_url(url.toString(QUrl.ComponentFormattingOption.FullyEncoded))


def _external_url(url: QUrl) -> bool:
    return (url.isValid() and url.scheme() in ("https", "http") and bool(url.host())
            and not url.userInfo() and not any(ord(c) < 32 for c in url.toString()))


class _Surface(QWidget):
    """Own the native child handle; no browser engine is bundled with this widget."""
    def __init__(self, parent):
        super().__init__(parent)
        self.host = None
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.host is not None:
            self.host.resize()
            QTimer.singleShot(0, self.host.resize)

    def showEvent(self, event):
        super().showEvent(event)
        if self.host is not None:
            QTimer.singleShot(0, self.host.resize)


class AtlasView(QWidget):
    zone_changed = Signal(str)
    fallback_requested = Signal()

    def __init__(self, parent=None, *, storage_path: Path | None = None):
        super().__init__(parent)
        self._storage_path = storage_path
        self._zone = self._npc = ""
        self._active = self._closed = self._native_ready = False
        self._native = self._browser = None
        self._has_document = self._loading = self._failed = False
        self._document_map = None
        self._document_url = QUrl()
        self._requested_url = QUrl()
        self._programmatic_world = False
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._timed_out)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        self._notice = QWidget(self)
        notice_layout = QVBoxLayout(self._notice)
        notice_layout.addStretch(1)
        self._message = QLabel("Select Atlas to load its interactive map.", self._notice)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        notice_layout.addWidget(self._message)
        controls = QHBoxLayout()
        controls.addStretch(1)
        self.retry_button = QPushButton("Retry", self._notice)
        self.retry_button.clicked.connect(self._retry)
        controls.addWidget(self.retry_button)
        self.saved_maps_button = QPushButton("Use saved maps", self._notice)
        self.saved_maps_button.clicked.connect(self.fallback_requested)
        controls.addWidget(self.saved_maps_button)
        controls.addStretch(1)
        notice_layout.addLayout(controls)
        self.runtime_button = QPushButton("Get Microsoft WebView2", self._notice)
        self.runtime_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(WEBVIEW2_DOWNLOAD)))
        notice_layout.addWidget(self.runtime_button)
        self.runtime_button.hide()
        notice_layout.addStretch(1)
        self._stack.addWidget(self._notice)
        layout.addWidget(self._stack, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(6, 2, 6, 2)
        credit = QLabel("Maps by MnM Atlas", self)
        credit.setToolTip("Atlas markers open Atlas popups. Use PNUT's monster menu for the consider panel.")
        footer.addWidget(credit)
        footer.addStretch(1)
        self.open_button = QPushButton("Open full Atlas", self)
        self.open_button.setToolTip("Open this map on MnM Atlas in your browser")
        self.open_button.clicked.connect(self._open_full)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)
        self.retry_button.hide()

    def _url(self):
        from mnmparse.atlas import atlas_url
        return QUrl(atlas_url(self._zone, self._npc))

    def _create_engine(self):
        from mnmparse.app.atlas_native import NativeHost
        from mnmparse.config import project_path

        surface = _Surface(self)
        try:
            host = NativeHost(int(surface.winId()),
                              (self._storage_path or project_path("atlas_cache")) / "webview2", self._event)
        except Exception:
            surface.deleteLater()
            raise
        surface.host = host
        # The plain-Python owner is safe even if Qt destroys the parent first.
        # Native close disables callbacks before releasing the COM controller.
        surface.destroyed.connect(host.close)
        self._native, self._browser = host, surface
        self._stack.addWidget(surface)

    def activate(self, zone: str, npc: str = ""):
        if self._closed:
            return
        self._zone, self._npc, self._active = zone, npc, True
        try:
            target = self._url()
            if not _atlas_origin(target):
                raise ValueError("Unsupported Atlas map")
            if self._native is None:
                self._create_engine()
            if self._native_ready:
                self._native.resume()
                self._native.set_visible(True)
            from mnmparse.atlas import atlas_zone
            same_map = self._document_map == (atlas_zone(target.toString()) or target.path())
            if same_map and not npc and not self._failed:
                if self._has_document:
                    self._stack.setCurrentWidget(self._browser)
                    return
                if self._loading:
                    return
            if self._loading and target == self._requested_url:
                return
            self._load(target)
        except Exception as exc:
            from mnmparse.app.atlas_native import RuntimeUnavailable
            if isinstance(exc, RuntimeUnavailable):
                self.runtime_button.show()
                self._show_error("Microsoft WebView2 is needed for interactive maps. Get it from Microsoft or use saved maps.")
            else:
                self._show_error("Atlas could not be opened. You can retry or use saved maps.")

    def set_zone(self, zone: str):
        if not self._closed:
            if self._active:
                self.activate(zone)
            else:
                self._zone, self._npc = zone, ""

    def focus_npc(self, name: str):
        if not self._closed:
            self._npc = name
            if self._active:
                self.activate(self._zone, name)

    def deactivate(self):
        if self._closed:
            return
        self._active = self._loading = False
        self._programmatic_world = False
        self._timeout.stop()
        if self._native_ready:
            try:
                self._native.stop()
                self._native.set_visible(False)
                self._native.suspend()
            except RuntimeError:
                # A runtime exiting during app shutdown can reject Stop/Hide.
                # Always retire its controller and allow later activation to retry.
                self._discard_native()

    def _discard_native(self):
        if self._native is not None:
            self._native.close()
            self._stack.removeWidget(self._browser)
            self._browser.deleteLater()
        self._native = self._browser = None
        self._native_ready = self._has_document = False

    def shutdown(self):
        if self._closed:
            return
        self.deactivate()
        self._closed = True
        if self._native is not None:
            self._native.close()
        self._native_ready = False

    def _load(self, target):
        from mnmparse.atlas import atlas_zone
        self._failed = False
        self._loading = True
        self._requested_url = QUrl(target)
        self._programmatic_world = atlas_zone(target.toString()) is None and target.path() in ("", "/")
        self.runtime_button.hide()
        self._message.setText("Loading MnM Atlas…")
        self.retry_button.hide()
        self._stack.setCurrentWidget(self._notice)
        self._timeout.start(_LOAD_TIMEOUT_MS)
        if self._native_ready:
            self._native.navigate(target.toString(QUrl.ComponentFormattingOption.FullyEncoded))

    def _retry(self):
        if not self._closed:
            # A failed asynchronous environment cannot be reused.
            if self._native is not None and not self._native_ready:
                self._discard_native()
            self._has_document = self._loading = False
            self.activate(self._zone, self._npc)

    def _event(self, event: int, value: int, data: str) -> int:
        from mnmparse.app import atlas_native as native
        if self._closed:
            return 0
        if event == native.NAV_REQUEST:
            url = QUrl(data)
            if _atlas_origin(url) or (data == "about:blank" and not self._has_document):
                return int(self._active)
            if self._active and value & 1 and _external_url(url):
                QDesktopServices.openUrl(url)
            return 0
        if event == native.READY:
            self._native_ready = True
            if self._active:
                self._native.resize()
                self._native.set_visible(True)
                self._native.navigate(self._requested_url.toString())
            else:
                self._native.set_visible(False)
                self._native.suspend()
        elif event == native.URL_CHANGED:
            self._url_changed(QUrl(data))
        elif event == native.LOAD_START and self._active:
            self._loading = True
            self._timeout.start(_LOAD_TIMEOUT_MS)
        elif event == native.LOAD_DONE and self._active and self._loading:
            self._timeout.stop()
            self._loading = False
            self._programmatic_world = False
            if value and _atlas_origin(self._document_url):
                self._has_document, self._failed = True, False
                self._stack.setCurrentWidget(self._browser)
            else:
                self._show_error("Atlas could not load. Check your connection, retry, or use saved maps.")
        elif event == native.NEW_WINDOW and self._active and value & 1:
            url = QUrl(data)
            if _atlas_origin(url):
                QTimer.singleShot(0, lambda: self._open_embedded(url))
            elif _external_url(url):
                QDesktopServices.openUrl(url)
        elif event in (native.ERROR, native.PROCESS_FAILED):
            self._native_ready = False
            self._show_error("Atlas stopped unexpectedly. Retry or use saved maps.")
        return 0

    def _open_embedded(self, url):
        if self._active and not self._closed:
            self._load(url)

    def _url_changed(self, url):
        if self._closed or not _atlas_origin(url):
            return
        from mnmparse.atlas import atlas_zone
        zone = atlas_zone(url.toString())
        previous_map = self._document_map
        self._document_map = zone or url.path()
        self._document_url = QUrl(url)
        if not self._active:
            return
        if (zone is None and url.path() in ("", "/") and previous_map not in (None, "", "/")
                and not self._programmatic_world):
            zone = "World"
        if zone is not None and zone != self._zone:
            self._zone, self._npc = zone, ""
            self.zone_changed.emit(zone)

    def _timed_out(self):
        self._loading = False
        if self._active and not self._closed:
            if self._native_ready:
                self._native.stop()
                self._native.set_visible(False)
                self._native.suspend()
            self._show_error("Atlas is taking too long to load. Retry or use saved maps.")

    def _show_error(self, text):
        self._timeout.stop()
        self._loading = False
        self._failed = True
        self._message.setText(text)
        self.retry_button.show()
        self._stack.setCurrentWidget(self._notice)

    def _open_full(self):
        try:
            url = self._document_url if self._has_document else self._url()
            if _atlas_origin(url):
                from PySide6.QtCore import QUrlQuery
                query = QUrlQuery(url)
                query.removeAllQueryItems("embed")
                full = QUrl(url)
                full.setQuery(query)
                QDesktopServices.openUrl(full)
        except (ValueError, RuntimeError):
            return

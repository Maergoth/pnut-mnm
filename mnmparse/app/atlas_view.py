"""Lazy, isolated MnM Atlas browser. Importing or constructing this widget is offline."""
from __future__ import annotations

from pathlib import Path
import weakref

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

_CACHE_BYTES = 64 * 1024 * 1024
_LOAD_TIMEOUT_MS = 30_000
_profiles = None


def _atlas_origin(url: QUrl) -> bool:
    """Only the real HTTPS origin may become an embedded top-level document."""
    from mnmparse.atlas import is_atlas_url
    return url.isValid() and is_atlas_url(url.toString(QUrl.ComponentFormattingOption.FullyEncoded))


def _external_url(url: QUrl) -> bool:
    return (url.isValid() and url.scheme() in ("https", "http") and bool(url.host())
            and not url.userInfo() and not any(ord(c) < 32 for c in url.toString()))


class _Profiles(QObject):
    """One persistent profile per path; its pages always die before the profile.

    A deferred cleanup allows another view to acquire the same still-live profile
    during a view replacement, without opening its SQLite files twice.
    """
    def __init__(self, parent):
        super().__init__(parent)
        self.entries = {}
        self.pages = {}
        self.next_token = 0
        self.cleanup_timer = QTimer(self)
        self.cleanup_timer.setSingleShot(True)
        self.cleanup_timer.timeout.connect(self._cleanup)
        parent.aboutToQuit.connect(self.shutdown)

    def acquire(self, path: Path):
        from PySide6.QtWebEngineCore import QWebEngineProfile
        key = str(path.resolve()).casefold()
        if key not in self.entries:
            path.mkdir(parents=True, exist_ok=True)
            profile = QWebEngineProfile("pnut-atlas", self)
            profile.setPersistentStoragePath(str(path / "storage"))
            profile.setCachePath(str(path / "http"))
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
            profile.setHttpCacheMaximumSize(_CACHE_BYTES)
            profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
            profile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.StoreInMemory)
            profile.downloadRequested.connect(self._cancel_download)
            self.entries[key] = (profile, set())
        return key, self.entries[key][0]

    def register(self, key, page):
        self.next_token += 1
        token = self.next_token
        page.setProperty("pnut-atlas-token", token)
        self.entries[key][1].add(token)
        self.pages[token] = (key, page)
        page.destroyed.connect(self._page_destroyed)

    @Slot(QObject)
    def _page_destroyed(self, _obj=None):
        page = self.sender()
        token = page.property("pnut-atlas-token")
        entry = self.pages.pop(token, None)
        if entry is not None:
            self.entries[entry[0]][1].discard(token)
            self.cleanup_timer.start(0)

    @Slot()
    def _cleanup(self):
        import shiboken6
        for key, (profile, pages) in tuple(self.entries.items()):
            if not pages:
                del self.entries[key]
                shiboken6.delete(profile)

    @Slot()
    def shutdown(self):
        import shiboken6
        self.cleanup_timer.stop()
        for _key, page in tuple(self.pages.values()):
            if shiboken6.isValid(page):
                shiboken6.delete(page)
        self._cleanup()

    @Slot(QObject)
    def _cancel_download(self, download):
        download.cancel()


class AtlasView(QWidget):
    zone_changed = Signal(str)
    fallback_requested = Signal()

    def __init__(self, parent=None, *, storage_path: Path | None = None):
        super().__init__(parent)
        self._storage_path = storage_path
        self._zone = ""
        self._npc = ""
        self._active = False
        self._closed = False
        self._browser = None
        self._page = None
        self._has_document = False
        self._loading = False
        self._failed = False
        self._document_zone = None
        self._document_map = None
        self._ignore_failed_navigation = False
        self._programmatic_world = False
        self._requested_url = QUrl()
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
        notice_layout.addStretch(1)
        self._stack.addWidget(self._notice)
        layout.addWidget(self._stack, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(6, 2, 6, 2)
        credit = QLabel("Maps by MnM Atlas", self)
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
        """This is the only WebEngine import boundary; called on explicit activation."""
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from mnmparse.config import project_path
        global _profiles
        if _profiles is None:
            _profiles = _Profiles(QApplication.instance())
        key, profile = _profiles.acquire(self._storage_path or project_path("atlas_cache"))
        owner_ref = weakref.ref(self)

        class Page(QWebEnginePage):
            def acceptNavigationRequest(page, url, navigation_type, is_main_frame):
                if not is_main_frame:
                    return True
                owner = owner_ref()
                if owner is None or owner._closed:
                    return False
                if _atlas_origin(url) or (url.toString() == "about:blank" and not owner._has_document):
                    return True
                if navigation_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and _external_url(url):
                    owner._ignore_failed_navigation = True
                    QDesktopServices.openUrl(url)
                return False

            def chooseFiles(page, _mode, _old_files, _accepted_types):
                return []

        browser = QWebEngineView(self)
        page = Page(profile, browser)
        _profiles.register(key, page)
        browser.setPage(page)
        settings = page.settings()
        for attr in (QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard,
                     QWebEngineSettings.WebAttribute.JavascriptCanPaste,
                     QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
                     QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
                     QWebEngineSettings.WebAttribute.AllowWindowActivationFromJavaScript,
                     QWebEngineSettings.WebAttribute.NavigateOnDropEnabled):
            settings.setAttribute(attr, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, True)
        page.permissionRequested.connect(self._deny_permission)
        page.newWindowRequested.connect(self._new_window)
        page.loadStarted.connect(self._load_started)
        page.loadFinished.connect(self._load_finished)
        page.urlChanged.connect(self._url_changed)
        page.renderProcessTerminated.connect(self._renderer_terminated)
        self._browser, self._page = browser, page
        self._stack.addWidget(browser)

    def activate(self, zone: str, npc: str = ""):
        if self._closed:
            return
        self._zone, self._npc = zone, npc
        self._active = True
        try:
            target = self._url()
            if not _atlas_origin(target):
                raise ValueError("Unsupported Atlas map")
            if self._browser is None:
                self._create_engine()
            self._page.setLifecycleState(self._page.LifecycleState.Active)
            self._page.setVisible(True)
            from mnmparse.atlas import atlas_zone
            same_map = self._document_map == (atlas_zone(target.toString()) or target.path())
            if self._has_document and same_map and not npc and not self._failed:
                self._stack.setCurrentWidget(self._browser)
                return
            if self._loading and target == self._requested_url:
                return
            self._load(target)
        except Exception:
            self._show_error("Atlas could not be opened. You can retry or use saved maps.")

    def set_zone(self, zone: str):
        if self._closed:
            return
        if self._active:
            self.activate(zone)
        else:
            self._zone, self._npc = zone, ""

    def focus_npc(self, name: str):
        if self._closed:
            return
        self._npc = name
        if self._active:
            self.activate(self._zone, name)

    def deactivate(self):
        if self._closed:
            return
        self._active = False
        self._timeout.stop()
        self._loading = False
        self._programmatic_world = False
        import shiboken6
        if self._browser is not None and self._page is not None and shiboken6.isValid(self._page):
            self._browser.stop()
            self._page.setVisible(False)
            self._page.setLifecycleState(self._page.LifecycleState.Frozen)

    def shutdown(self):
        if self._closed:
            return
        self.deactivate()
        self._closed = True
        if self._browser is not None:
            import shiboken6
            browser, self._browser, self._page = self._browser, None, None
            self._stack.removeWidget(browser)
            if shiboken6.isValid(browser):
                shiboken6.delete(browser)

    def _load(self, target):
        from mnmparse.atlas import atlas_zone
        self._failed = False
        self._ignore_failed_navigation = False
        self._programmatic_world = (target.path() in ("", "/") and atlas_zone(target.toString()) is None)
        self._loading = True
        self._requested_url = target
        self._message.setText("Loading MnM Atlas…")
        self.retry_button.hide()
        self._stack.setCurrentWidget(self._notice)
        self._timeout.start(_LOAD_TIMEOUT_MS)
        self._page.setUrl(target)

    @Slot()
    def _retry(self):
        if not self._closed:
            self._has_document = False
            self.activate(self._zone, self._npc)

    @Slot()
    def _load_started(self):
        if self._active and not self._closed:
            self._loading = True
            self._timeout.start(_LOAD_TIMEOUT_MS)

    @Slot(bool)
    def _load_finished(self, ok):
        if not self._active or self._closed or not self._loading:
            return
        self._timeout.stop()
        self._loading = False
        if not ok and self._ignore_failed_navigation:
            self._ignore_failed_navigation = False
            if self._has_document:
                self._stack.setCurrentWidget(self._browser)
            return
        if ok and _atlas_origin(self._page.url()):
            self._failed = False
            self._has_document = True
            self._url_changed(self._page.url())
            self._programmatic_world = False
            self._stack.setCurrentWidget(self._browser)
        else:
            self._show_error("Atlas could not load. Check your connection, retry, or use saved maps.")

    @Slot(QUrl)
    def _url_changed(self, url):
        if self._closed or not _atlas_origin(url):
            return
        from mnmparse.atlas import atlas_zone
        zone = atlas_zone(url.toString())
        previous_map = self._document_map
        self._document_map = zone or url.path()
        if not self._active:
            return
        # Preserve explicit Atlas World navigation, but keep the host's context
        # when an unsupported game zone initially falls back to the world map.
        if (zone is None and url.path() in ("", "/") and previous_map not in (None, "", "/")
                and not self._programmatic_world):
            zone = "World"
        if zone is not None:
            self._document_zone = zone
            if zone != self._zone:
                self._zone, self._npc = zone, ""
                self.zone_changed.emit(zone)

    @Slot()
    def _timed_out(self):
        self._loading = False
        if self._active and not self._closed:
            self._browser.stop()
            self._show_error("Atlas is taking too long to load. Retry or use saved maps.")

    def _renderer_terminated(self, _status, _code):
        if not self._closed:
            self._show_error("Atlas stopped unexpectedly. Retry or use saved maps.")

    def _show_error(self, text):
        self._timeout.stop()
        self._loading = False
        self._failed = True
        self._programmatic_world = False
        self._message.setText(text)
        self.retry_button.show()
        self._stack.setCurrentWidget(self._notice)

    def _deny_permission(self, permission):
        permission.deny()

    @Slot(QObject)
    def _new_window(self, request):
        if self._closed or not self._active or not request.isUserInitiated():
            return
        url = request.requestedUrl()
        if _atlas_origin(url):
            self._load(url)
        elif _external_url(url):
            QDesktopServices.openUrl(url)

    @Slot()
    def _open_full(self):
        try:
            url = self._page.url() if self._has_document else self._url()
            if _atlas_origin(url):
                # The external full site should display its own normal navigation.
                from PySide6.QtCore import QUrlQuery
                query = QUrlQuery(url)
                query.removeAllQueryItems("embed")
                full = QUrl(url)
                full.setQuery(query)
                QDesktopServices.openUrl(full)
        except (ValueError, RuntimeError):
            return

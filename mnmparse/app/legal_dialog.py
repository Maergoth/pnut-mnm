"""Read bundled privacy, terms, and license documents without fetching remote content."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTabWidget, QTextBrowser, QVBoxLayout, QWidget

from mnmparse.app.window_identity import window_title


DOCUMENTS = (
    ("privacy", "Privacy", "privacy.md"),
    ("terms", "Terms & Disclaimer", "terms.md"),
    ("open-source", "Open Source Notices", "open-source.md"),
)


def legal_directory() -> Path:
    bundle = getattr(sys, "_MEIPASS", None)
    return (Path(bundle) if bundle else Path(__file__).resolve().parents[2]) / "legal"


class _OfflineBrowser(QTextBrowser):
    def loadResource(self, resource_type: int, url: QUrl):  # noqa: N802 - Qt override
        # These documents are text. Embedded images or other resources must never
        # turn opening a legal tab into a network request or an unrelated file read.
        return None


class LegalDialog(QDialog):
    def __init__(self, section: str = "privacy", parent: QWidget | None = None, *, legal_dir: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle(window_title("legal"))
        self.resize(820, 650)
        self.setMinimumSize(480, 360)
        self._legal_dir = (legal_dir if legal_dir is not None else legal_directory()).resolve()
        self._tabs = QTabWidget(self)
        self._tabs.setAccessibleName("Legal documents")
        self._tabs.setUsesScrollButtons(True)
        self._tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self._browsers: dict[str, QTextBrowser] = {}
        self.tabs = self._tabs
        self.browsers = self._browsers
        self._license_browser: QTextBrowser | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        heading = QLabel("Privacy, Terms & Open Source", self)
        heading.setProperty("class", "h2")
        layout.addWidget(heading)
        layout.addWidget(self._tabs, 1)
        self._notice = QLabel(self)
        self._notice.setProperty("class", "muted")
        self._notice.setTextFormat(Qt.TextFormat.PlainText)
        self._notice.setWordWrap(True)
        self._notice.hide()
        layout.addWidget(self._notice)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        for key, title, filename in DOCUMENTS:
            browser = self._browser(title)
            try:
                browser.setMarkdown((self._legal_dir / filename).read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                browser.setPlainText("This document is not available in this installation.")
            self._browsers[key] = browser
            index = self._tabs.addTab(browser, title.replace("&", "&&"))
            self._tabs.setTabToolTip(index, title)
        self.select_section(section)

    def _browser(self, title: str) -> QTextBrowser:
        browser = _OfflineBrowser(self)
        browser.setAccessibleName(title)
        browser.setOpenLinks(False)
        browser.setOpenExternalLinks(False)
        browser.setReadOnly(True)
        browser.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        browser.document().setBaseUrl(QUrl.fromLocalFile(str(self._legal_dir) + os.sep))
        browser.anchorClicked.connect(lambda url, source=browser: self._open_link(url, source))
        return browser

    def select_section(self, section: str) -> None:
        self._tabs.setCurrentWidget(self._browsers.get(section, self._browsers["privacy"]))
        self._notice.hide()

    def _local_file(self, url: QUrl) -> Path | None:
        if url.scheme() not in ("", "file") or url.host():
            return None
        path = url.toLocalFile() if url.isLocalFile() else url.path()
        if not path:
            return None
        # Reject traversal and other drives before touching the candidate path.
        try:
            candidate = Path(os.path.abspath(self._legal_dir / path))
            if not candidate.is_relative_to(self._legal_dir):
                return None
            candidate = candidate.resolve()
            if not candidate.is_relative_to(self._legal_dir) or not candidate.is_file():
                return None
        except (OSError, ValueError):
            return None
        return candidate

    def _open_link(self, url: QUrl, source: QTextBrowser) -> None:
        self._notice.hide()
        if url.scheme() == "https" and url.isValid() and url.host():
            if not QDesktopServices.openUrl(url):
                self._notice.setText("The link could not be opened.")
                self._notice.show()
            return
        if not url.scheme() and not url.path() and url.fragment():
            source.scrollToAnchor(url.fragment())
            return
        path = self._local_file(url)
        if path is not None:
            for key, _title, filename in DOCUMENTS:
                if path == self._legal_dir / filename:
                    self.select_section(key)
                    return
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                text = None
            if text is not None:
                if self._license_browser is None:
                    self._license_browser = self._browser("Full license text")
                    self._tabs.addTab(self._license_browser, "Full License")
                self._license_browser.setPlainText(text)
                self._license_browser.setAccessibleName(f"Full license text: {path.name}")
                self._tabs.setTabText(self._tabs.indexOf(self._license_browser), f"License: {path.name}".replace("&", "&&"))
                self._tabs.setTabToolTip(self._tabs.indexOf(self._license_browser), path.name)
                self._tabs.setCurrentWidget(self._license_browser)
                return
        self._notice.setText("This local document is unavailable.")
        self._notice.show()

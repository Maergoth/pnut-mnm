"""The in-app timer and trigger guide; opening it never leaves the editor."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QTextCursor, QTextOption
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout, QWidget

from mnmparse.app.trigger_help_content import TRIGGER_HELP_HTML
from mnmparse.app.widgets import token


class TriggerHelpDialog(QDialog):
    """A selectable, scrollable guide with a table of contents inside the app."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Timer/trigger help")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        self.browser = QTextBrowser(self)
        self.browser.setAccessibleName("Timer and trigger guide")
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.anchorClicked.connect(self._open_anchor)
        self.browser.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.browser.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        self.browser.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.browser.document().setDocumentMargin(20)
        self.browser.document().setDefaultStyleSheet(
            f"body {{ color: {token('TEXT')}; font-size: 14px; }}"
            f"h1 {{ color: {token('TEXT')}; font-size: 25px; margin-bottom: 12px; }}"
            f"h2 {{ color: {token('ACCENT')}; font-size: 19px; margin-top: 24px; margin-bottom: 9px; }}"
            f"h3 {{ color: {token('TEXT')}; font-size: 16px; margin-top: 16px; margin-bottom: 6px; }}"
            "p, li { margin-top: 6px; margin-bottom: 8px; }"
            f"a {{ color: {token('ACCENT2')}; }}"
            f"code, pre {{ color: {token('TEXT')}; background-color: {token('BG2')}; "
            "font-family: Consolas, monospace; font-size: 13px; white-space: pre-wrap; }"
            f"th {{ color: {token('ACCENT')}; }}"
            "td, th { padding: 5px; }"
        )
        self.browser.setHtml(TRIGGER_HELP_HTML)
        self.browser.moveCursor(QTextCursor.MoveOperation.Start)
        self.browser.verticalScrollBar().setValue(0)
        layout.addWidget(self.browser, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        if screen is None:
            self.setMinimumSize(480, 360)
            self.resize(920, 760)
        else:
            available = screen.availableGeometry().adjusted(24, 40, -24, -40)
            width, height = max(1, available.width()), max(1, available.height())
            self.setMinimumSize(min(480, width), min(360, height))
            self.resize(min(920, width), min(760, height))
            self.move(available.center() - self.rect().center())

    def _open_anchor(self, url: QUrl) -> None:
        """Follow only this guide's section links; never launch another application."""
        if url.isRelative() and not url.path() and not url.hasQuery() and url.hasFragment():
            self.browser.scrollToAnchor(url.fragment())

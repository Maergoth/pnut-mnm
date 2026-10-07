"""Small windows that dock under the overlay and can be pulled off it.

Docked, a panel follows its anchor (the overlay, or the panel docked above it) with the
same left edge and width, just below it, or above when there is no room below.  While the
overlay is unlocked, dragging a panel undocks it and it stays where it is dropped;
dropping it close to its anchor's bottom edge, a double click, or "Snap to overlay" in its
right-click menu docks it again.  Panels never take focus and follow the overlay's lock and
click-through state.  The stacking order is the overlay, the timer panel, then the
auto-attack bar (:meth:`OverlayWindow.dock_anchor`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRect, QSettings, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QMenu, QWidget

from mnmparse.app.window_identity import window_title

if TYPE_CHECKING:
    from mnmparse.app.overlay import OverlayWindow

DOCK_GAP = 4  #: pixels between a docked panel and its anchor
SNAP_DISTANCE = 24  #: dropping a panel this close to its anchor's bottom edge docks it


class DockedPanel(QWidget):
    """Base class: docking, dragging, cursor and the shared right-click entries."""

    SETTINGS_GROUP = "panel"
    TITLE = "PNUT M&M panel"

    def __init__(self, owner: "OverlayWindow", settings: QSettings, flags: Qt.WindowType) -> None:
        super().__init__(None)
        self._owner = owner
        self._settings = settings
        self.setWindowTitle(window_title(f"panel:{self.SETTINGS_GROUP}"))
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.setMouseTracking(True)
        self.enabled = True
        g = self.SETTINGS_GROUP
        self._docked = str(settings.value(f"{g}/docked", "true")).lower() in ("true", "1")
        saved = settings.value(f"{g}/geometry", None)
        self._free_geometry = saved if isinstance(saved, QRect) and saved.isValid() else None
        if self._free_geometry is not None:
            from mnmparse.app.overlay import clamp_to_screen  # a monitor may have gone away

            self._free_geometry = clamp_to_screen(self._free_geometry)
        self._drag_offset: QPointF | None = None
        self._press: QPointF | None = None
        self._dragged = False

    # -- state -------------------------------------------------------------------------
    @property
    def docked(self) -> bool:
        return self._docked

    def wants_visible(self) -> bool:
        """Whether the panel has something to show (subclasses refine)."""
        return True

    def update_cursor(self) -> None:
        """Move cursor only while the overlay is unlocked (the panel can be dragged then)."""
        self.setCursor(Qt.CursorShape.ArrowCursor if self._owner.locked else Qt.CursorShape.SizeAllCursor)

    def sync(self) -> None:
        """Show / hide with the overlay and follow it while docked."""
        owner = self._owner
        if not self.enabled or not owner.isVisible() or not self.wants_visible():
            if self.isVisible():
                self.hide()
                owner.panels_changed()
            return
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, owner.click_through)
        self.follow()
        if not self.isVisible():
            self.show()
            owner.panels_changed()

    def follow(self) -> None:
        if self._press is not None:
            return  # being dragged: the mouse decides
        if self._docked:
            g = self._owner.dock_anchor(self)
            y = g.bottom() + 1 + DOCK_GAP
            screen = self._owner.screen()
            if screen is not None and y + self.height() > screen.availableGeometry().bottom():
                y = g.top() - DOCK_GAP - self.height()  # no room below: sit above
            self.setGeometry(g.left(), y, g.width(), self.height())
        elif self._free_geometry is not None:
            r = QRect(self._free_geometry)
            r.setHeight(self.height())
            self.setGeometry(r)
        else:
            self._docked = True
            self.follow()

    def dock(self) -> None:
        self._docked = True
        self._settings.setValue(f"{self.SETTINGS_GROUP}/docked", True)
        self.follow()
        self._owner.panels_changed()

    def set_rows_height(self, height: int) -> None:
        """Change the height; the panels docked below follow."""
        if height != self.height():
            self.setFixedHeight(height)
            if self.isVisible():
                self.follow()
                self._owner.panels_changed()

    # -- mouse -------------------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition()
            self._dragged = False
            self._drag_offset = None if self._owner.locked else event.globalPosition() - QPointF(self.frameGeometry().topLeft())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._press is not None and (event.globalPosition() - self._press).manhattanLength() > 4:
                if not self._dragged:
                    self._dragged = True
                    was_docked, self._docked = self._docked, False
                    if was_docked:
                        self._owner.panels_changed()
            if self._dragged:
                self.move((event.globalPosition() - self._drag_offset).toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        dragged = self._dragged
        self._press = None  # follow() works again
        if event.button() == Qt.MouseButton.LeftButton and dragged:
            anchor = self._owner.dock_anchor(self)
            near = (
                abs(self.y() - anchor.bottom()) <= SNAP_DISTANCE
                and self.x() < anchor.right()
                and self.x() + self.width() > anchor.left()
            )
            if near:
                self.dock()
            else:
                self._free_geometry = QRect(self.geometry())
                self._settings.setValue(f"{self.SETTINGS_GROUP}/docked", False)
                self._settings.setValue(f"{self.SETTINGS_GROUP}/geometry", self._free_geometry)
        self._drag_offset = None
        self._dragged = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.dock()
        event.accept()

    def add_menu_entries(self, menu: QMenu, event: Any) -> dict[Any, Any]:
        """Subclasses add their own entries; returns ``{action: callable}``."""
        return {}

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.setStyleSheet(self._owner.menu_qss())
        handlers = self.add_menu_entries(menu, event)
        if handlers:
            menu.addSeparator()
        snap = menu.addAction("Snap to overlay")
        snap.setEnabled(not self._docked)
        handlers[snap] = self.dock
        chosen = menu.exec(event.globalPos())
        menu.deleteLater()
        action = handlers.get(chosen)
        if action is not None:
            action()


__all__ = ["DockedPanel", "DOCK_GAP", "SNAP_DISTANCE"]

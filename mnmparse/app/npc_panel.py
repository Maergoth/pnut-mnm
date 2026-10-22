"""A detachable, passive NPC information panel with cache-backed item tooltips.

The controller owns wiki requests and caching.  This widget only displays supplied
plain-text data and asks for an item when its drop row is hovered.  Tooltip updates
reuse one non-activating window, so a cache refresh never closes and reopens it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QCursor, QDesktopServices, QFont, QFontMetricsF, QHideEvent, QMouseEvent, QPainter, QPainterPath,
    QPaintEvent, QPen, QResizeEvent, QWheelEvent,
)
from PySide6.QtWidgets import QLabel, QMenu, QScrollBar, QVBoxLayout, QWidget

from mnmparse.app.docked_panel import DOCK_GAP, DockedPanel
from mnmparse.app.npc_panel_lifetime import NpcPanelLifetime
from mnmparse.app.widgets import make_font, qcolor, token
from mnmparse.npc_wiki import WIKI, unique_drops_excluding_common, wiki_url

if TYPE_CHECKING:
    from mnmparse.app.overlay import OverlayWindow
    from mnmparse.npc_wiki import ItemDetails, NpcDetails


@dataclass(frozen=True)
class _Row:
    text: str
    kind: str = "body"
    item: str = ""


def _available_text(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    key = " ".join(text.split()).casefold()
    direction, separator, remainder = key.partition(":")
    if separator and direction in {"gained", "lost"}:
        key = remainder.strip()
    return "" if key in {"", "unknown", "none", "none known", "n/a", "not known", "not yet known", "tbd", "not available in wiki data"} else text


class _ItemTooltip(QWidget):
    """A game-inspired item frame which never takes focus or intercepts input."""

    def __init__(self, panel: "NpcPanel") -> None:
        super().__init__(panel, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._panel = panel
        self._name = QLabel(self)
        self._stats = QLabel(self)
        self._note = QLabel(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        for label in (self._name, self._stats, self._note):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            layout.addWidget(label)

    def present(self, name: str, lines: tuple[str, ...], note: str, position: QPoint) -> None:
        px = self._panel._px
        self._name.setFont(make_font(px * 1.08, weight=QFont.Weight.DemiBold))
        self._stats.setFont(make_font(px))
        self._note.setFont(make_font(px * 0.85))
        self._name.setStyleSheet(f"color: {token('ACCENT')}; background: transparent;")
        self._stats.setStyleSheet(f"color: {token('TEXT')}; background: transparent;")
        self._note.setStyleSheet(f"color: {token('MUTED')}; background: transparent;")
        self._name.setText(name)
        self._stats.setText("\n".join(lines))
        self._stats.setVisible(bool(lines))
        self._note.setText(note)
        self._note.setVisible(bool(note))
        screen = self._panel.screen()
        area = screen.availableGeometry() if screen is not None else None
        width = int(max(260, min(440, 340 * px / 13)))
        if area is not None:
            width = min(width, area.width())
        self.setFixedWidth(width)
        # QWidget.adjustSize uses the unconstrained label hints and can clip the
        # last stat lines after wrapping. Size the frame at its actual width.
        self.layout().invalidate()
        self.resize(width, self.layout().heightForWidth(width))
        self.layout().activate()
        if area is not None:
            position.setX(max(area.left(), min(position.x(), area.right() - self.width() + 1)))
            position.setY(max(area.top(), min(position.y(), area.bottom() - self.height() + 1)))
        self.move(position)
        self.update()
        if not self.isVisible():
            self.show()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        frame = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setBrush(qcolor(token("BG1"), 0.98))
        painter.setPen(QPen(qcolor(token("ACCENT"), 0.75), 1))
        painter.drawRoundedRect(frame, 5, 5)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(qcolor(token("LINE")), 1))
        painter.drawRoundedRect(frame.adjusted(3, 3, -3, -3), 3, 3)
        painter.end()


class NpcPanel(DockedPanel):
    """Considered NPC information; docking and input behavior match the timer panel."""

    SETTINGS_GROUP = "npc_panel"
    TITLE = "PNUT M&M NPC"
    item_hovered = Signal(str)

    def __init__(self, owner: "OverlayWindow", settings: QSettings, flags: Qt.WindowType) -> None:
        super().__init__(owner, settings, flags)
        self._px = 13.0
        self._name = ""
        self._details: "NpcDetails | None" = None
        self._status = ""
        self._fields = {"level": True, "common_drops": False, "unique_drops": True,
                        "quests": False, "reputation": False}
        self._rows: list[_Row] = []
        self._row_rects: list[tuple[QRectF, _Row]] = []
        self._body = QRectF()
        self._laying_out = False
        self._following = False
        self._column_count = 1
        self._resize_edge = ""
        self._resize_changed = False
        self._resize_origin: tuple[QPointF, QRect] | None = None
        self._link_press: tuple[str, str] | None = None
        self._link_origin: QPointF | None = None
        self._link_moved = False
        try:
            saved_width = int(settings.value("npc_panel/width", 0))
        except (TypeError, ValueError):
            saved_width = 0
        self._preferred_width: int | None = max(180, saved_width) if saved_width > 0 else None
        try:
            saved_height = int(settings.value("npc_panel/height", 0))
        except (TypeError, ValueError):
            saved_height = 0
        self._preferred_height: int | None = max(64, saved_height) if saved_height > 0 else None
        self._hovered_item = ""
        self._tooltip_position = QPoint()
        self._items: dict[str, "ItemDetails"] = {}
        self._item_status: dict[str, str] = {}
        self._tooltip = _ItemTooltip(self)
        self._scroll = QScrollBar(Qt.Orientation.Vertical, self)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setAccessibleName("NPC information")
        self._scroll.valueChanged.connect(self._scrolled)
        self._lifetime = NpcPanelLifetime(self)
        self._scroll.installEventFilter(self)
        self._scroll.hide()
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumWidth(180)
        self._apply_fonts()
        self._apply_scroll_style()
        self._build_rows()
        self._layout_rows()

    def show_npc(self, name: str) -> None:
        """Select a new NPC immediately, clearing the previous NPC's details."""
        name = str(name or "").strip()
        if name:
            self._lifetime.present()
        if name != self._name:
            self._name = name
            self._details = None
            self._status = "Loading NPC information…" if name else ""
            self._scroll.setValue(0)
            self._clear_hover()
        self._refresh()

    def set_details(self, details: "NpcDetails | None", status: str = "") -> None:
        self._details = details
        if details is not None and not self._name:
            self._name = details.name
        self._status = str(status or "")
        self._refresh()

    def set_status(self, status: str) -> None:
        """Change request status without replacing the currently displayed cache."""
        self._status = str(status or "")
        self._refresh()

    def configure(self, *, enabled: bool | None = None, level: bool | None = None,
                  common_drops: bool | None = None, unique_drops: bool | None = None,
                  quests: bool | None = None, reputation: bool | None = None) -> None:
        """Apply preferences; omitted fields keep their existing selection."""
        if enabled is not None:
            self.enabled = bool(enabled)
        values = {"level": level, "common_drops": common_drops, "unique_drops": unique_drops,
                  "quests": quests, "reputation": reputation}
        for key, value in values.items():
            if value is not None:
                self._fields[key] = bool(value)
        self._clear_hover()
        self._refresh()

    def set_item_details(self, item: "ItemDetails") -> None:
        key = item.name.casefold()
        self._items[key] = item
        self._item_status.pop(key, None)
        if self._hovered_item.casefold() == key:
            self._present_item()

    def set_item_status(self, name: str, status: str) -> None:
        key = name.casefold()
        self._item_status[key] = str(status or "")
        if self._hovered_item.casefold() == key:
            self._present_item()

    def set_font_px(self, px: float) -> None:
        self._px = max(7.0, min(30.0, float(px)))
        self.refresh_style()

    def refresh_style(self) -> None:
        """Refresh theme and the owner's opacity after appearance settings change."""
        self._apply_fonts()
        self._apply_scroll_style()
        self._refresh()
        if self._hovered_item:
            self._present_item()

    def _settings_gear_changed(self) -> None:
        """The hover gear floats over the unchanged title and information."""
        self.update()

    def wants_visible(self) -> bool:
        return bool(self._name) and any(self._fields.values()) and not self._lifetime.expired

    @property
    def options(self) -> dict[str, bool]:
        return {"enabled": bool(self.enabled), **self._fields}

    def sync(self) -> None:
        if self._owner.locked or self._owner.click_through:
            self._finish_gesture()
        if self._owner.click_through or not self.enabled or not self._owner.isVisible():
            self._clear_hover()
        if self._owner.click_through:
            self._lifetime.set_hovered(False)
        self._lifetime.sync()
        super().sync()

    def _available_area(self) -> QRect:
        screen = (self._owner.screen() or self.screen()) if self._docked else (self.screen() or self._owner.screen())
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)

    def set_panel_width(self, width: int) -> None:
        """Remember an independent width without changing the docking choice."""
        area = self._available_area()
        self._preferred_width = max(self.minimumWidth(), min(int(width), area.width()))
        self._settings.setValue("npc_panel/width", self._preferred_width)
        if not self._docked:
            self._free_geometry = QRect(self.geometry())
            self._free_geometry.setWidth(self._preferred_width)
        self.follow()
        self._layout_rows()
        self._save_free_geometry()

    def set_panel_height(self, height: int) -> None:
        """Remember a height independently of content, width, and docking."""
        self._preferred_height = max(self.minimumHeight(), min(int(height), self._available_area().height()))
        self._settings.setValue("npc_panel/height", self._preferred_height)
        self.follow()
        self._save_free_geometry()

    def set_rows_height(self, height: int) -> None:
        # DockedPanel fixes content-driven panel heights. This panel also has a
        # user-selected height, so keep QWidget's maximum height unrestricted.
        if height != self.height():
            self.resize(self.width(), height)
            if self.isVisible():
                self.follow()
                self._owner.panels_changed()

    def _save_free_geometry(self) -> None:
        if not self._docked:
            self._free_geometry = QRect(self.geometry())
            self._settings.setValue("npc_panel/docked", False)
            self._settings.setValue("npc_panel/geometry", self._free_geometry)

    def follow(self) -> None:
        if self._following or self._press is not None or self._resize_origin is not None:
            return
        self._following = True
        try:
            area = self._available_area()
            anchor = self._owner.dock_anchor(self)
            if self._docked:
                width = self._preferred_width or anchor.width()
                left = anchor.left()
            else:
                saved = self._free_geometry or self.geometry()
                width = self._preferred_width or saved.width()
                left = saved.left()
            width = min(area.width(), max(self.minimumWidth(), width))
            left = max(area.left(), min(left, area.right() - width + 1))
            self.setGeometry(left, self.y(), width, self.height())
            self._layout_rows()
            if self._docked:
                below = anchor.bottom() + 1 + DOCK_GAP
                above = anchor.top() - DOCK_GAP - self.height()
                top = below if below + self.height() <= area.bottom() + 1 else above
            else:
                top = (self._free_geometry or self.geometry()).top()
            top = max(area.top(), min(top, area.bottom() - self.height() + 1))
            self.move(left, top)
        finally:
            self._following = False

    def update_cursor(self) -> None:
        self.update()
        if ((self._owner.locked or self._owner.click_through)
                and (self._resize_origin is not None or self._press is not None)):
            self._finish_gesture()
            return
        if self._resize_origin is not None:
            self.setCursor(self._edge_cursor(self._resize_edge))
        else:
            self.setCursor(self._cursor_at(self.mapFromGlobal(QCursor.pos())))

    def add_menu_entries(self, menu: QMenu, event: Any) -> dict[Any, Any]:
        self._clear_hover()
        hide = menu.addAction("Hide NPC information")
        return {hide: self._hide_panel}

    def _hide_panel(self) -> None:
        setter = getattr(self._owner, "set_npc_panel_enabled", None)
        if setter is not None:
            setter(False)
        else:
            self.configure(enabled=False)

    def _apply_fonts(self) -> None:
        self._font = make_font(self._px)
        self._heading_font = make_font(self._px * 0.87, weight=QFont.Weight.DemiBold)
        self._title_font = make_font(self._px * 1.04, weight=QFont.Weight.DemiBold)
        self._note_font = make_font(self._px * 0.84)

    def _apply_scroll_style(self) -> None:
        self._scroll.setStyleSheet(
            "QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,0.25); border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        )

    def _build_rows(self) -> None:
        rows: list[_Row] = []
        if self._fields["level"]:
            value = _available_text(getattr(self._details, "level", ""))
            if value:
                rows.append(_Row(f"Level: {value}"))
        common = getattr(self._details, "common_drops", ()) or ()
        unique = unique_drops_excluding_common(common, getattr(self._details, "unique_drops", ()) or ())
        for key, label in (("unique_drops", "Unique drops"), ("common_drops", "Common drops"),
                           ("quests", "Required by quests"), ("reputation", "Reputation gained / lost")):
            if not self._fields[key]:
                continue
            values = unique if key == "unique_drops" else getattr(self._details, key, ()) or ()
            available = [text for value in values if (text := _available_text(value))]
            if not available:
                continue
            rows.append(_Row(label, "heading"))
            rows.extend(_Row(f"• {text}", item=text if key.endswith("drops") else "") for text in available)
        self._rows = rows
        if self._hovered_item and not any(row.item.casefold() == self._hovered_item.casefold() for row in rows):
            self._clear_hover()

    def _refresh(self) -> None:
        self._build_rows()
        self._layout_rows()
        self.sync()
        self.update_cursor()
        self.update()

    def _layout_rows(self) -> None:
        if self._laying_out:
            return
        self._laying_out = True
        try:
            margin = 10
            title_h = int(QFontMetricsF(self._title_font).height()) + 13
            note_h = int(QFontMetricsF(self._note_font).height()) + 8 if self._status else 0
            area = self._available_area()
            available_h = area.height()
            if self._docked:
                anchor = self._owner.dock_anchor(self)
                available_h = max(area.bottom() - anchor.bottom() - DOCK_GAP,
                                  anchor.top() - area.top() - DOCK_GAP)
            minimum_h = title_h + note_h + 24 + 8
            self.setMinimumHeight(minimum_h)
            if self._preferred_height is not None:
                max_body_h = max(24, min(self._preferred_height, available_h) - title_h - note_h - 8)
            else:
                max_body_h = max(24, min(int(280 * self._px / 13), available_h - title_h - note_h - 8))
            # Reserve the gutter even for short data, avoiding reflow when the scroll bar appears.
            width = max(40, self.width() - 2 * margin - 12)
            gap = 12
            minimum_column = max(150, int(170 * self._px / 13))
            maximum_columns = max(1, min(8, int((width + gap) / (minimum_column + gap))))
            # Wide windows should use their horizontal space even when a tall
            # preferred height could fit everything in one narrow column.
            first_columns = min(maximum_columns, max(1, (len(self._rows) + 11) // 12)) if maximum_columns >= 3 else 1
            rects, content_h = [], 0.0
            for columns in range(first_columns, maximum_columns + 1):
                column_width = (width - (columns - 1) * gap) / columns
                measured = [(row, self._row_height(row, column_width)) for row in self._rows]
                rects, needed, content_h = self._pack_rows(measured, column_width, gap, max_body_h)
                if needed <= columns or columns == maximum_columns:
                    # Balance lists across the chosen columns, retaining
                    # vertical scrolling for arbitrarily long data.
                    low = 0.0
                    high = sum(h for _, h in measured) + 10 * len(measured) + 1
                    for _ in range(14):
                        target = (low + high) / 2
                        _, used, _ = self._pack_rows(measured, column_width, gap, target)
                        if used <= columns:
                            high = target
                        else:
                            low = target
                    rects, needed, content_h = self._pack_rows(measured, column_width, gap, high)
                    self._column_count = needed
                    break
            body_h = max_body_h if self._preferred_height is not None else max(24, min(max_body_h, int(content_h + 0.999)))
            self._body = QRectF(margin, title_h, width, body_h)
            self._row_rects = rects
            self._scroll.setPageStep(body_h)
            self._scroll.setSingleStep(max(12, int(self._px * 2)))
            self._scroll.setRange(0, max(0, int(content_h + 0.999) - body_h))
            self._scroll.setVisible(self._scroll.maximum() > 0)
            self._scroll.setGeometry(self.width() - margin - 7, title_h, 7, body_h)
            self.set_rows_height(title_h + body_h + note_h + 8)
        finally:
            self._laying_out = False

    def _text_flags(self, row: _Row, width: float) -> int:
        font = self._heading_font if row.kind == "heading" else self._font
        fm = QFontMetricsF(font)
        long_word = any(fm.horizontalAdvance(word) > width for word in row.text.split())
        wrap = Qt.TextFlag.TextWrapAnywhere if long_word else Qt.TextFlag.TextWordWrap
        return int(wrap | Qt.AlignmentFlag.AlignTop)

    def _row_height(self, row: _Row, width: float) -> float:
        font = self._heading_font if row.kind == "heading" else self._font
        fm = QFontMetricsF(font)
        height = fm.boundingRect(QRectF(0, 0, width, 100000), self._text_flags(row, width), row.text).height()
        return max(fm.height(), height) + (10 if row.kind == "heading" else 5)

    @staticmethod
    def _pack_rows(measured: list[tuple[_Row, float]], width: float, gap: int,
                   target_height: float) -> tuple[list[tuple[QRectF, _Row]], int, float]:
        rects: list[tuple[QRectF, _Row]] = []
        column, y, tallest = 0, 0.0, 0.0
        heading: tuple[_Row, float] | None = None
        for index, (row, height) in enumerate(measured):
            required = height
            if row.kind == "heading" and index + 1 < len(measured):
                required += measured[index + 1][1]
            if y and y + required > target_height:
                tallest = max(tallest, y)
                column += 1
                y = 0.0
                if row.kind != "heading" and heading is not None:
                    label, label_h = heading
                    rects.append((QRectF(10 + column * (width + gap), y, width, label_h), label))
                    y += label_h
            if row.kind == "heading":
                heading = (row, height)
            rects.append((QRectF(10 + column * (width + gap), y, width, height), row))
            y += height
        return rects, column + 1, max(tallest, y)

    def _scrolled(self) -> None:
        self._clear_hover()
        self.update()

    def _item_at(self, position: QPoint) -> str:
        if not self._body.contains(position):
            return ""
        point = position - QPoint(0, int(self._body.top()) - self._scroll.value())
        return next((row.item for rect, row in self._row_rects if row.item and rect.contains(point)), "")

    def _title_rect(self) -> QRectF:
        return QRectF(10, 5, max(0, self.width() - 20), self._body.top() - 7)

    def _name_rect(self) -> QRectF:
        rect = self._title_rect()
        metrics = QFontMetricsF(self._title_font)
        visible = metrics.elidedText(self._name, Qt.TextElideMode.ElideRight, rect.width())
        rect.setWidth(min(rect.width(), metrics.horizontalAdvance(visible)))
        return rect

    def _link_at(self, position: QPoint) -> tuple[str, str] | None:
        if self._owner.click_through:
            return None
        if self._name and self._name_rect().contains(position):
            return "npc", self._name
        item = self._item_at(position)
        return ("item", item) if item else None

    def _open_link(self, link: tuple[str, str]) -> None:
        if self._owner.click_through or not self.enabled or not self.isVisible():
            return
        kind, name = link
        record = self._details if kind == "npc" else self._items.get(name.casefold())
        source = getattr(record, "source", "")
        try:
            parts = urlsplit(source) if isinstance(source, str) else None
        except ValueError:
            parts = None
        canonical = (parts is not None and parts.scheme == "https"
                     and parts.netloc.casefold() == urlsplit(WIKI).netloc
                     and parts.path.startswith("/wiki/") and len(parts.path) > 6
                     and not parts.query and "\\" not in parts.path)
        try:
            target = source if canonical else wiki_url(name)
        except ValueError:
            return
        QDesktopServices.openUrl(QUrl(target))

    def _hover_item(self, name: str, position: QPoint) -> None:
        if self._owner.click_through or not self.isVisible():
            self._clear_hover()
            return
        if name == self._hovered_item:
            return
        self._hovered_item = name
        self.update()
        if not name:
            self._tooltip.hide()
            return
        self._tooltip_position = position + QPoint(18, 18)
        self._present_item()
        self.item_hovered.emit(name)

    def _present_item(self) -> None:
        name = self._hovered_item
        if not name:
            return
        key = name.casefold()
        item = self._items.get(key)
        lines = tuple(item.lines) if item is not None else ()
        note = self._item_status.get(key, "")
        if not lines and not note:
            note = "Item statistics unavailable" if item is not None else "Loading item details…"
        self._tooltip.present(name, lines, note, QPoint(self._tooltip_position))

    def _clear_hover(self) -> None:
        if self._hovered_item:
            self.update()
        self._hovered_item = ""
        self._tooltip.hide()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.UngrabMouse and hasattr(self, "_resize_origin"):
            self._finish_gesture()
        if event.type() == QEvent.Type.ToolTip:
            # Reuse the same tooltip for repeated help events and async cache updates.
            self._hover_item(self._item_at(event.pos()), event.globalPos())
            return True
        return super().event(event)

    def _check_mouse_presence(self) -> None:
        inside = (self.isVisible() and not self._owner.click_through
                  and self.rect().contains(self.mapFromGlobal(QCursor.pos())))
        self._lifetime.set_hovered(inside)

    def enterEvent(self, event: QEvent) -> None:  # noqa: N802
        self._lifetime.set_hovered(not self._owner.click_through)
        super().enterEvent(event)

    def eventFilter(self, watched: Any, event: QEvent) -> bool:  # noqa: N802
        if watched is self._scroll:
            if event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove):
                self._lifetime.set_hovered(not self._owner.click_through)
            elif event.type() == QEvent.Type.Leave:
                QTimer.singleShot(0, self, self._check_mouse_presence)
            elif event.type() == QEvent.Type.MouseButtonPress:
                self._lifetime.set_interacting(True)
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._lifetime.set_interacting(False)
            elif event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.Hide):
                self._lifetime.set_interacting(False)
                QTimer.singleShot(0, self, self._check_mouse_presence)
        return super().eventFilter(watched, event)

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802
        self._lifetime.set_interacting(True)
        try:
            super().contextMenuEvent(event)
        finally:
            self._lifetime.set_interacting(False)
            self._check_mouse_presence()

    def _grip_rect(self) -> QRect:
        return QRect(self.width() - 18, self.height() - 18, 16, 16)

    def _edge_at(self, x: float, y: float | None = None) -> str:
        if self._owner.locked or self._owner.click_through:
            return ""
        y = self.height() / 2 if y is None else y
        if self._grip_rect().contains(QPoint(round(x), round(y))):
            return "rightbottom"
        edge = "left" if x < 6 else "right" if x >= self.width() - 6 else ""
        edge += "top" if y < 6 else "bottom" if y >= self.height() - 6 else ""
        return edge

    @staticmethod
    def _edge_cursor(edge: str) -> Qt.CursorShape:
        horizontal = "left" in edge or "right" in edge
        vertical = "top" in edge or "bottom" in edge
        if horizontal and vertical:
            return Qt.CursorShape.SizeFDiagCursor if edge in {"lefttop", "rightbottom"} else Qt.CursorShape.SizeBDiagCursor
        return Qt.CursorShape.SizeHorCursor if horizontal else Qt.CursorShape.SizeVerCursor

    def _cursor_at(self, point: QPoint) -> Qt.CursorShape:
        edge = self._edge_at(point.x(), point.y()) if self.rect().contains(point) else ""
        if edge:
            return self._edge_cursor(edge)
        if self._link_at(point):
            return Qt.CursorShape.PointingHandCursor
        return Qt.CursorShape.ArrowCursor if self._owner.locked or self._owner.click_through else Qt.CursorShape.SizeAllCursor

    def _finish_gesture(self) -> None:
        """Persist an interrupted resize/drag and release its lifetime hold."""
        resizing = self._resize_origin is not None
        changed = self._resize_changed
        dragged = self._dragged
        self._link_press = self._link_origin = None
        self._link_moved = False
        if not resizing and self._press is None:
            return
        self._resize_origin = None
        self._resize_edge = ""
        self._resize_changed = False
        self._press = None
        self._drag_offset = None
        self._dragged = False
        if changed:
            self._save_preferred_size()
        if changed or dragged:
            self._save_free_geometry()
            self.follow()
            self._owner.panels_changed()
        self._lifetime.set_interacting(False)
        self.update_cursor()

    def _save_preferred_size(self) -> None:
        if self._preferred_width is not None:
            self._settings.setValue("npc_panel/width", self._preferred_width)
        if self._preferred_height is not None:
            self._settings.setValue("npc_panel/height", self._preferred_height)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._link_press = self._link_origin = None
        self._link_moved = False
        if self._owner.click_through:
            event.ignore()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._lifetime.set_interacting(True)
            edge = self._edge_at(event.position().x(), event.position().y())
            if edge:
                self._resize_edge = edge
                self._resize_origin = (event.globalPosition(), QRect(self.geometry()))
                self._resize_changed = False
                self._clear_hover()
                self.update_cursor()
                event.accept()
                return
            self._link_press = self._link_at(event.position().toPoint())
            self._link_origin = event.globalPosition()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._link_origin is not None and (event.globalPosition() - self._link_origin).manhattanLength() > 4:
            self._link_moved = True
        if self._press is not None and ((self._owner.locked and self._drag_offset is not None) or self._owner.click_through
                                       or not event.buttons() & Qt.MouseButton.LeftButton):
            self._finish_gesture()
        if self._resize_origin is not None:
            if (self._owner.locked or self._owner.click_through
                    or not event.buttons() & Qt.MouseButton.LeftButton):
                self._finish_gesture()
                return
            start, original = self._resize_origin
            dx = round(event.globalPosition().x() - start.x())
            dy = round(event.globalPosition().y() - start.y())
            horizontal = "left" in self._resize_edge or "right" in self._resize_edge
            vertical = "top" in self._resize_edge or "bottom" in self._resize_edge
            if (horizontal and dx) or (vertical and dy) or self._resize_changed:
                area = self._available_area()
                minimum = min(self.minimumWidth(), area.width())
                left, width = original.left(), original.width()
                top, height = original.top(), original.height()
                if "left" in self._resize_edge:
                    right = min(original.right(), area.right())
                    left = max(area.left(), min(original.left() + dx, right - minimum + 1))
                    width = right - left + 1
                elif horizontal:
                    left = max(area.left(), min(original.left(), area.right() - minimum + 1))
                    width = max(minimum, min(original.width() + dx, area.right() - left + 1))
                minimum_h = min(self.minimumHeight(), area.height())
                if "top" in self._resize_edge:
                    bottom = min(original.bottom(), area.bottom())
                    top = max(area.top(), min(original.top() + dy, bottom - minimum_h + 1))
                    height = bottom - top + 1
                elif vertical:
                    top = max(area.top(), min(original.top(), area.bottom() - minimum_h + 1))
                    height = max(minimum_h, min(original.height() + dy, area.bottom() - top + 1))
                if self._docked and ("left" in self._resize_edge or "top" in self._resize_edge):
                    self._docked = False
                    self._owner.panels_changed()
                if horizontal:
                    self._preferred_width = width
                if vertical:
                    self._preferred_height = height
                self._resize_changed = True
                self.setGeometry(left, top, width, height)
                self.move(left, max(area.top(), min(self.y(), area.bottom() - self.height() + 1)))
            event.accept()
            return
        super().mouseMoveEvent(event)
        if not event.buttons() and not self._owner.click_through:
            self._lifetime.set_hovered(True)
            self._hover_item(self._item_at(event.position().toPoint()), event.globalPosition().toPoint())
            self.setCursor(self._cursor_at(event.position().toPoint()))
        else:
            self._clear_hover()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._resize_origin is not None and event.button() == Qt.MouseButton.LeftButton:
            changed = self._resize_changed
            self._resize_origin = None
            self._resize_edge = ""
            self._resize_changed = False
            if changed:
                self._save_preferred_size()
                self._save_free_geometry()
                self.follow()
                self._save_free_geometry()
                self._owner.panels_changed()
            self._lifetime.set_interacting(False)
            self._check_mouse_presence()
            self.update_cursor()
            event.accept()
            return
        link = self._link_press
        moved = self._link_moved or self._dragged or (self._link_origin is not None
                and (event.globalPosition() - self._link_origin).manhattanLength() > 4)
        self._link_press = self._link_origin = None
        self._link_moved = False
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._lifetime.set_interacting(False)
            self._check_mouse_presence()
            if link is not None and not moved and self._link_at(event.position().toPoint()) == link:
                self._open_link(link)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self._clear_hover()
        self._check_mouse_presence()
        super().leaveEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802
        self._clear_hover()
        self._finish_gesture()
        self._lifetime.hidden()
        super().hideEvent(event)

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self._lifetime.stop()
        super().closeEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_rows"):
            self._layout_rows()
            self._clear_hover()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._scroll.maximum() > 0:
            delta = event.pixelDelta().y() or int(event.angleDelta().y() / 120 * self._scroll.singleStep())
            self._scroll.setValue(self._scroll.value() - delta)
            event.accept()
        else:
            super().wheelEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(frame, 8, 8)
        painter.fillPath(path, qcolor(token("BG0"), float(getattr(self._owner, "opacity", 0.85))))
        painter.setPen(QPen(qcolor(token("LINE")), 1))
        painter.drawPath(path)
        title_rect = self._title_rect()
        painter.setFont(self._title_font)
        painter.setPen(qcolor(token("ACCENT")))
        painter.drawText(title_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         QFontMetricsF(self._title_font).elidedText(self._name, Qt.TextElideMode.ElideRight, title_rect.width()))
        painter.save()
        painter.setClipRect(self._body)
        painter.translate(0, self._body.top() - self._scroll.value())
        for rect, row in self._row_rects:
            painter.setFont(self._heading_font if row.kind == "heading" else self._font)
            color = "MUTED" if row.kind in ("heading", "muted") else "TEXT"
            if row.item and row.item == self._hovered_item:
                color = "ACCENT"
            painter.setPen(qcolor(token(color)))
            painter.drawText(rect, self._text_flags(row, rect.width()), row.text)
        painter.restore()
        if self._status:
            painter.setFont(self._note_font)
            painter.setPen(qcolor(token("MUTED")))
            note = QRectF(10, self._body.bottom() + 5, max(0, self.width() - 20), self.height() - self._body.bottom() - 7)
            painter.drawText(note, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                             QFontMetricsF(self._note_font).elidedText(self._status, Qt.TextElideMode.ElideRight,
                                 max(0, note.width() - (16 if not self._owner.locked else 0))))
        if not self._owner.locked and not self._owner.click_through:
            # Match OverlaySizeGrip's six-dot handle while keeping all gesture
            # handling in this passive window rather than a child size grip.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(token("MUTED"), 0.9))
            grip = self._grip_rect()
            for dx, dy in ((10, 10), (10, 6), (6, 10), (10, 2), (6, 6), (2, 10)):
                painter.drawEllipse(QRectF(grip.x() + dx, grip.y() + dy, 2.4, 2.4))
        painter.end()


__all__ = ["NpcPanel"]

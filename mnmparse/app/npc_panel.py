"""A detachable, passive NPC information panel with cache-backed item tooltips.

The controller owns wiki requests and caching.  This widget only displays supplied
plain-text data and asks for an item when its drop row is hovered.  Tooltip updates
reuse one non-activating window, so a cache refresh never closes and reopens it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEvent, QPoint, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import (
    QFont, QFontMetricsF, QHideEvent, QMouseEvent, QPainter, QPainterPath,
    QPaintEvent, QPen, QResizeEvent, QWheelEvent,
)
from PySide6.QtWidgets import QLabel, QMenu, QScrollBar, QVBoxLayout, QWidget

from mnmparse.app.docked_panel import DockedPanel
from mnmparse.app.widgets import make_font, qcolor, token

if TYPE_CHECKING:
    from mnmparse.app.overlay import OverlayWindow
    from mnmparse.npc_wiki import ItemDetails, NpcDetails


@dataclass(frozen=True)
class _Row:
    text: str
    kind: str = "body"
    item: str = ""


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
        self._hovered_item = ""
        self._tooltip_position = QPoint()
        self._items: dict[str, "ItemDetails"] = {}
        self._item_status: dict[str, str] = {}
        self._tooltip = _ItemTooltip(self)
        self._scroll = QScrollBar(Qt.Orientation.Vertical, self)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setAccessibleName("NPC information")
        self._scroll.valueChanged.connect(self._scrolled)
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

    def wants_visible(self) -> bool:
        return bool(self._name) and any(self._fields.values())

    @property
    def options(self) -> dict[str, bool]:
        return {"enabled": bool(self.enabled), **self._fields}

    def sync(self) -> None:
        if self._owner.click_through or not self.enabled or not self._owner.isVisible():
            self._clear_hover()
        super().sync()

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
            value = str(getattr(self._details, "level", "") or "Unknown")
            rows.append(_Row(f"Level: {value}"))
        for key, label in (("common_drops", "Common drops"), ("unique_drops", "Unique drops"),
                           ("quests", "Required by quests"), ("reputation", "Reputation gained / lost")):
            if not self._fields[key]:
                continue
            rows.append(_Row(label, "heading"))
            values = getattr(self._details, key, ()) or ()
            if values:
                for value in values:
                    text = str(value)
                    rows.append(_Row(f"• {text}", item=text if key.endswith("drops") else ""))
            else:
                rows.append(_Row("Not available in wiki data" if self._details is not None else "Unknown", "muted"))
        self._rows = rows

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
            max_body_h = int(280 * self._px / 13)
            screen = self.screen()
            if screen is not None:
                max_body_h = min(max_body_h, max(60, screen.availableGeometry().height() - title_h - note_h - 32))
            # Reserve the gutter even for short data, avoiding reflow when the scroll bar appears.
            width = max(40, self.width() - 2 * margin - 12)
            y = 0.0
            rects = []
            for row in self._rows:
                font = self._heading_font if row.kind == "heading" else self._font
                fm = QFontMetricsF(font)
                h = fm.boundingRect(QRectF(0, 0, width, 100000),
                                    int(Qt.TextFlag.TextWordWrap), row.text).height()
                if row.kind == "heading":
                    y += 5
                h = max(fm.height(), h) + 5
                rects.append((QRectF(margin, y, width, h), row))
                y += h
            body_h = max(24, min(max_body_h, int(y)))
            self._body = QRectF(margin, title_h, width, body_h)
            self._row_rects = rects
            self._scroll.setPageStep(body_h)
            self._scroll.setSingleStep(max(12, int(self._px * 2)))
            self._scroll.setRange(0, max(0, int(y) - body_h))
            self._scroll.setVisible(self._scroll.maximum() > 0)
            self._scroll.setGeometry(self.width() - margin - 7, title_h, 7, body_h)
            self.set_rows_height(title_h + body_h + note_h + 8)
        finally:
            self._laying_out = False

    def _scrolled(self) -> None:
        self._clear_hover()
        self.update()

    def _item_at(self, position: QPoint) -> str:
        if not self._body.contains(position):
            return ""
        point = position - QPoint(0, int(self._body.top()) - self._scroll.value())
        return next((row.item for rect, row in self._row_rects if row.item and rect.contains(point)), "")

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
        if event.type() == QEvent.Type.ToolTip:
            # Reuse the same tooltip for repeated help events and async cache updates.
            self._hover_item(self._item_at(event.pos()), event.globalPos())
            return True
        return super().event(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        super().mouseMoveEvent(event)
        if not event.buttons() and not self._owner.click_through:
            self._hover_item(self._item_at(event.position().toPoint()), event.globalPosition().toPoint())
        else:
            self._clear_hover()

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self._clear_hover()
        super().leaveEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802
        self._clear_hover()
        super().hideEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_rows"):
            self._layout_rows()

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
        title_rect = QRectF(10, 5, max(0, self.width() - 20), self._body.top() - 7)
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
            painter.drawText(rect, int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop), row.text)
        painter.restore()
        if self._status:
            painter.setFont(self._note_font)
            painter.setPen(qcolor(token("MUTED")))
            note = QRectF(10, self._body.bottom() + 5, max(0, self.width() - 20), self.height() - self._body.bottom() - 7)
            painter.drawText(note, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                             QFontMetricsF(self._note_font).elidedText(self._status, Qt.TextElideMode.ElideRight, note.width()))
        painter.end()


__all__ = ["NpcPanel"]

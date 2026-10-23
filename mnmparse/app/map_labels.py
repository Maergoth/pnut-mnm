"""Map-image label editing, vector icons and zoom-independent scene markers."""
from __future__ import annotations

from functools import lru_cache
from html import escape
import math
import unicodedata

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTextBoundaryFinder
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGraphicsItem,
                              QGraphicsObject, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget)

from mnmparse.app import theme
from mnmparse.app.widgets import prevent_wheel_value_changes
from mnmparse.app.window_identity import window_title
from mnmparse.map_labels import ICONS, MAX_NAME, MAX_NOTES, MapLabel


def label_icon(kind: str) -> QIcon:
    """A small, font-independent symbol with contrast against light and dark maps."""
    return QIcon(_icon(kind if kind in dict(ICONS) else "pin"))


@lru_cache(maxsize=16)
def _icon(kind: str) -> QIcon:
    color = {"danger": theme.DANGER, "camp": theme.ACCENT2, "merchant": theme.SUCCESS}.get(kind, theme.ACCENT)
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 96):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 24, size / 24)
        painter.setPen(QPen(theme.qcolor(color), 1.3, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(theme.qcolor(theme.BG0, .95))
        painter.drawEllipse(QRectF(1, 1, 22, 22))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if kind == "pin":
            path = QPainterPath(QPointF(12, 20))
            path.cubicTo(9, 16, 6, 12, 6, 9)
            path.cubicTo(6, 1, 18, 1, 18, 9)
            path.cubicTo(18, 12, 15, 16, 12, 20)
            painter.drawPath(path)
            painter.drawEllipse(QRectF(10, 6.5, 4, 4))
        elif kind == "star":
            path = QPainterPath()
            for index in range(10):
                angle = -math.pi / 2 + index * math.pi / 5
                radius = 8 if index % 2 == 0 else 3.5
                point = QPointF(12 + math.cos(angle) * radius, 12 + math.sin(angle) * radius)
                path.moveTo(point) if index == 0 else path.lineTo(point)
            path.closeSubpath()
            painter.setBrush(theme.qcolor(color, .18))
            painter.drawPath(path)
        elif kind == "camp":
            path = QPainterPath(QPointF(4.5, 19))
            path.lineTo(12, 5)
            path.lineTo(19.5, 19)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawLine(QPointF(12, 11), QPointF(12, 19))
            painter.drawLine(QPointF(12, 11), QPointF(7.5, 19))
        elif kind == "treasure":
            painter.drawRoundedRect(QRectF(5, 7, 14, 12), 2, 2)
            painter.drawLine(QPointF(5, 12), QPointF(19, 12))
            painter.setBrush(theme.qcolor(theme.BG0))
            painter.drawRect(QRectF(10, 10, 4, 5))
        elif kind == "danger":
            path = QPainterPath(QPointF(12, 4))
            path.lineTo(20, 19)
            path.lineTo(4, 19)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawLine(QPointF(12, 9), QPointF(12, 13))
            painter.setBrush(theme.qcolor(color))
            painter.drawEllipse(QRectF(11.3, 15.3, 1.4, 1.4))
        else:  # merchant: a coin purse, without font-dependent currency characters
            path = QPainterPath(QPointF(9, 7))
            path.lineTo(8, 4.5)
            path.lineTo(16, 4.5)
            path.lineTo(15, 7)
            path.cubicTo(22, 17, 18, 20, 12, 20)
            path.cubicTo(6, 20, 2, 17, 9, 7)
            painter.drawPath(path)
            painter.drawLine(QPointF(9, 8), QPointF(15, 8))
            painter.drawEllipse(QRectF(9.5, 11, 5, 5))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _text_values(name: str, notes: str) -> tuple[str, str]:
    return name.strip(), notes.replace("\r\n", "\n").replace("\r", "\n").strip()


class MapLabelDialog(QDialog):
    """A normal keyboard-focus dialog; opening it never writes the label store."""

    def __init__(self, parent: QWidget | None = None, *, label: MapLabel | None = None):
        super().__init__(parent)
        self.setWindowTitle(window_title("map-label-editor"))
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self.setStyleSheet(theme.main_qss())
        prevent_wheel_value_changes(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        heading = QLabel("Edit map label" if label is not None else "Add map label")
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        hint = QLabel("Saved on this map image.")
        hint.setProperty("class", "muted")
        layout.addWidget(hint)
        form = QFormLayout()
        self.name_input = QLineEdit(label.name if label is not None else "")
        self.name_input.setAccessibleName("Map label name")
        self.name_input.setAccessibleDescription(f"Required. Up to {MAX_NAME} characters.")
        self.name_input.setPlaceholderText("Label name")
        # Qt counts UTF-16 units; validation below counts Unicode code points.
        self.name_input.setMaxLength(MAX_NAME * 2)
        form.addRow("Name", self.name_input)
        self.icon_combo = QComboBox()
        self.icon_combo.setAccessibleName("Map label icon")
        self.icon_combo.setIconSize(QSize(24, 24))
        for kind, title in ICONS:
            self.icon_combo.addItem(label_icon(kind), title, kind)
        self.icon_combo.setCurrentIndex(max(0, self.icon_combo.findData(label.icon if label is not None else "pin")))
        form.addRow("Icon", self.icon_combo)
        self.notes_input = QPlainTextEdit()
        self.notes_input.setPlainText(label.notes if label is not None else "")
        self.notes_input.setAccessibleName("Map label notes")
        self.notes_input.setAccessibleDescription(f"Optional. Up to {MAX_NOTES} characters.")
        self.notes_input.setPlaceholderText("Notes (optional)")
        self.notes_input.setMinimumHeight(100)
        form.addRow("Notes", self.notes_input)
        layout.addLayout(form)
        self.notes_count = QLabel()
        self.notes_count.setProperty("class", "muted")
        self.notes_count.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.notes_count)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: " + theme.DANGER + ";")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name_input.textChanged.connect(self._validate)
        self.notes_input.textChanged.connect(self._validate)
        self._validate()
        self.resize(420, self.sizeHint().height())
        self.name_input.setFocus()
        self.name_input.selectAll()

    def value(self) -> dict[str, str]:
        name, notes = _text_values(self.name_input.text(), self.notes_input.toPlainText())
        return {"name": name, "notes": notes, "icon": self.icon_combo.currentData()}

    def _validate(self) -> bool:
        value = self.value()
        name, notes = value["name"], value["notes"]
        self.notes_count.setText(f"{len(notes)} / {MAX_NOTES}")
        error = ("Enter a label name." if not name else
                 f"Use a name of up to {MAX_NAME} characters." if len(name) > MAX_NAME else
                 "Use a single line for the label name." if any(unicodedata.category(c) in {"Cc", "Cs"} for c in name) else
                 f"Use notes of up to {MAX_NOTES} characters." if len(notes) > MAX_NOTES else
                 "Remove unsupported control characters from the notes." if any(
                     unicodedata.category(c) in {"Cc", "Cs"} and c not in "\n\t" for c in notes) else "")
        self.error.setText(error)
        self.error.setVisible(bool(error))
        self.save_button.setEnabled(not error)
        return not error

    def accept(self) -> None:
        if self._validate():
            super().accept()


def _tooltip_text(text: str) -> str:
    # Break opportunities are display-only and preserve complete emoji graphemes.
    encoded = text.encode("utf-16-le")
    boundaries = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, text)
    parts, start = [], 0
    while (end := boundaries.toNextBoundary()) != -1:
        cluster = encoded[start * 2:end * 2].decode("utf-16-le")
        parts.append("<br>" if cluster == "\n" else escape(cluster))
        if not cluster.isspace():
            parts.append("\u200b")
        start = end
    return "".join(parts)


def _tooltip(label: MapLabel) -> str:
    notes = "<br><br>" + _tooltip_text(label.notes) if label.notes else ""
    return ('<qt><table width="280" cellspacing="0" cellpadding="3"><tr><td><b>'
            + _tooltip_text(label.name) + "</b>" + notes + "</td></tr></table></qt>")


class MapLabelItem(QGraphicsObject):
    """A 24px marker whose name and notes appear only in its native tooltip."""

    def __init__(self, label: MapLabel):
        super().__init__()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self.set_label(label)

    def set_label(self, label: MapLabel) -> None:
        self.label = label
        self._icon = label_icon(label.icon)
        self.setToolTip(_tooltip(label))
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt API
        return QRectF(-12, -12, 24, 24)

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        self._icon.paint(painter, self.boundingRect().toRect())


__all__ = ["MapLabelDialog", "MapLabelItem", "label_icon"]

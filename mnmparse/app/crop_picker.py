"""Visual crop calibration: a frame preview with a draggable rectangle and an OCR test.

:class:`CropPicker` shows one captured game frame scaled to fit, lets the user drag,
resize (8 handles) or nudge (arrow keys) the crop rectangle, mirrors it in four spin
boxes and can run the OCR on the current crop to show what the pipeline reads.  The
crop is always reported in full-frame pixels, exactly like ``Config.crop``.

Capturing a frame and running the OCR both go through the engine
(``engine.grab_frame()`` / ``engine.test_ocr()``) on a short-lived background thread
so the GUI never blocks; the game itself is only ever read, never touched.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mnmparse.app import theme

if TYPE_CHECKING:
    from mnmparse.app.engine import Engine
    from mnmparse.config import Config
    from mnmparse.ocr import OcrLine

log = logging.getLogger(__name__)

__all__ = ["CropPicker", "CropCanvas", "OcrBox", "button_qss", "css_color", "qcolor"]

Crop = tuple[int, int, int, int]

#: Frame size assumed for the preview before any frame has been captured.
_NOMINAL_FRAME: tuple[int, int] = (3840, 2160)
_HANDLE_PX = 9
_MIN_SIZE = 8
_MARGIN = 6


# ----------------------------------------------------------------------------------
# Colour helpers (theme tokens may be hex or ``rgba(r,g,b,a)`` strings)
# ----------------------------------------------------------------------------------


def qcolor(token: str | QColor) -> QColor:
    """Parse a theme token (``#rrggbb`` or ``rgba(r, g, b, a)``) into a :class:`QColor`."""
    if isinstance(token, QColor):
        return QColor(token)
    text = token.strip()
    if text.lower().startswith("rgba(") or text.lower().startswith("rgb("):
        inner = text[text.index("(") + 1 : text.rindex(")")] if ")" in text else text[text.index("(") + 1 :]
        parts = [p.strip() for p in inner.split(",")]
        try:
            r, g, b = (int(float(parts[i])) for i in range(3))
            alpha = 1.0
            if len(parts) > 3:
                a = float(parts[3])
                alpha = a / 255.0 if a > 1.0 else a
        except (ValueError, IndexError):
            log.debug("Unparseable colour token %r", token)
            return QColor(theme.TEXT)
        color = QColor(r, g, b)
        color.setAlphaF(max(0.0, min(1.0, alpha)))
        return color
    return QColor(text)


def css_color(token: str | QColor, *, alpha: float | None = None) -> str:
    """Return a Qt-stylesheet-safe ``rgba(r, g, b, a255)`` form of ``token``."""
    color = qcolor(token)
    a = int(round((color.alphaF() if alpha is None else alpha) * 255))
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {a})"


def button_qss() -> str:
    """Stylesheet for the ``Primary`` / ``Chip`` push-button object names (shared with pages)."""
    line = css_color(theme.LINE)
    return f"""
    QPushButton#Chip {{
        background: transparent; color: {theme.MUTED}; border: 1px solid {line};
        border-radius: 8px; padding: 3px 10px; min-height: 20px;
    }}
    QPushButton#Chip:hover {{ background: {theme.BG2}; color: {theme.TEXT}; }}
    QPushButton#Chip:disabled {{ color: {css_color(theme.MUTED, alpha=0.5)}; }}
    QPushButton#Primary {{
        background: {theme.ACCENT}; color: {theme.BG0}; border: none; border-radius: 8px;
        padding: 6px 18px; font-weight: 600; min-height: 24px;
    }}
    QPushButton#Primary:hover {{ background: #f5c67c; }}
    QPushButton#Primary:disabled {{ background: {theme.BG2}; color: {theme.MUTED}; }}
    """


# ----------------------------------------------------------------------------------
# Background job helper
# ----------------------------------------------------------------------------------


class _Job(QObject):
    """Run ``fn`` on a daemon thread and deliver ``(result, error)`` on the GUI thread."""

    done = Signal(object, object)

    def __init__(self, fn: Callable[[], Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def start(self) -> None:
        threading.Thread(target=self._run, name="crop-picker-job", daemon=True).start()

    def _run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - reported to the UI, never raised on the thread
            log.exception("Crop picker job failed")
            self.done.emit(None, exc)
            return
        self.done.emit(result, None)


# ----------------------------------------------------------------------------------
# Canvas
# ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OcrBox:
    """One recognised line, in full-frame pixels (``w`` is estimated from the text)."""

    x: int
    y: int
    w: int
    h: int
    text: str


def _frame_to_qimage(frame: np.ndarray) -> QImage:
    """Convert a BGR or BGRA ``uint8`` array to a detached :class:`QImage`."""
    if frame.ndim != 3 or frame.shape[2] not in (3, 4):
        raise ValueError(f"Expected HxWx3 or HxWx4 frame, got shape {frame.shape}")
    data = np.ascontiguousarray(frame)
    height, width, channels = data.shape
    if channels == 3:
        fmt = QImage.Format.Format_BGR888
    else:
        fmt = QImage.Format.Format_RGB32  # BGRA little-endian == 0xAARRGGBB
    image = QImage(data.data, width, height, width * channels, fmt)
    return image.copy()


class CropCanvas(QWidget):
    """The preview: frame scaled to fit, crop rectangle with 8 handles, OCR boxes."""

    crop_changed = Signal(tuple)

    _HANDLE_KEYS = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._scaled: QPixmap | None = None
        self._frame_size: tuple[int, int] = _NOMINAL_FRAME
        self._crop: Crop = (0, 0, 100, 100)
        self._boxes: list[OcrBox] = []
        self._drag: str | None = None  # handle key, "move" or "new"
        self._drag_origin = QPointF()
        self._drag_crop: Crop = self._crop
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -- public ----------------------------------------------------------------------
    def set_frame(self, frame: np.ndarray | None) -> None:
        """Show ``frame`` (BGR/BGRA array) or a blank nominal canvas when ``None``."""
        if frame is None:
            self._pixmap = None
            self._frame_size = _NOMINAL_FRAME
        else:
            image = _frame_to_qimage(frame)
            self._pixmap = QPixmap.fromImage(image)
            self._frame_size = (image.width(), image.height())
        self._scaled = None
        self._boxes = []
        self._set_crop(self._clamp(self._crop), emit=True)
        self.update()

    def frame_size(self) -> tuple[int, int]:
        """``(width, height)`` of the frame (nominal 3840x2160 before a capture)."""
        return self._frame_size

    def has_frame(self) -> bool:
        return self._pixmap is not None

    def crop(self) -> Crop:
        """``(left, top, right, bottom)`` in full-frame pixels."""
        return self._crop

    def set_crop(self, crop: Crop) -> None:
        """Set the rectangle programmatically (clamped; emits :attr:`crop_changed` on change)."""
        self._set_crop(self._clamp(tuple(int(v) for v in crop)), emit=True)

    def set_boxes(self, boxes: list[OcrBox]) -> None:
        """Overlay OCR line boxes (full-frame pixels)."""
        self._boxes = list(boxes)
        self.update()

    # -- geometry --------------------------------------------------------------------
    def _clamp(self, crop: Crop) -> Crop:
        width, height = self._frame_size
        left, top, right, bottom = crop
        left = max(0, min(left, width - _MIN_SIZE))
        top = max(0, min(top, height - _MIN_SIZE))
        right = max(left + _MIN_SIZE, min(right, width))
        bottom = max(top + _MIN_SIZE, min(bottom, height))
        return (left, top, right, bottom)

    def _set_crop(self, crop: Crop, *, emit: bool) -> None:
        if crop == self._crop:
            return
        self._crop = crop
        self.update()
        if emit:
            self.crop_changed.emit(crop)

    def _image_rect(self) -> QRectF:
        """Where the (scaled) frame sits inside the widget."""
        fw, fh = self._frame_size
        avail_w = max(1, self.width() - 2 * _MARGIN)
        avail_h = max(1, self.height() - 2 * _MARGIN)
        scale = min(avail_w / fw, avail_h / fh)
        w, h = fw * scale, fh * scale
        return QRectF(_MARGIN + (avail_w - w) / 2, _MARGIN + (avail_h - h) / 2, w, h)

    def _scale(self) -> float:
        return self._image_rect().width() / self._frame_size[0]

    def _to_widget(self, x: float, y: float) -> QPointF:
        rect = self._image_rect()
        s = self._scale()
        return QPointF(rect.left() + x * s, rect.top() + y * s)

    def _to_frame(self, pos: QPointF) -> tuple[float, float]:
        rect = self._image_rect()
        s = self._scale()
        return ((pos.x() - rect.left()) / s, (pos.y() - rect.top()) / s)

    def _crop_rect_widget(self) -> QRectF:
        left, top, right, bottom = self._crop
        p1 = self._to_widget(left, top)
        p2 = self._to_widget(right, bottom)
        return QRectF(p1, p2)

    def _handles(self) -> dict[str, QRectF]:
        r = self._crop_rect_widget()
        cx, cy = r.center().x(), r.center().y()
        points = {
            "nw": (r.left(), r.top()),
            "n": (cx, r.top()),
            "ne": (r.right(), r.top()),
            "e": (r.right(), cy),
            "se": (r.right(), r.bottom()),
            "s": (cx, r.bottom()),
            "sw": (r.left(), r.bottom()),
            "w": (r.left(), cy),
        }
        half = _HANDLE_PX / 2
        return {k: QRectF(x - half, y - half, _HANDLE_PX, _HANDLE_PX) for k, (x, y) in points.items()}

    def _hit(self, pos: QPointF) -> str | None:
        for key, rect in self._handles().items():
            if rect.adjusted(-3, -3, 3, 3).contains(pos):
                return key
        if self._crop_rect_widget().contains(pos):
            return "move"
        return None

    @staticmethod
    def _cursor_for(hit: str | None) -> Qt.CursorShape:
        return {
            "nw": Qt.CursorShape.SizeFDiagCursor,
            "se": Qt.CursorShape.SizeFDiagCursor,
            "ne": Qt.CursorShape.SizeBDiagCursor,
            "sw": Qt.CursorShape.SizeBDiagCursor,
            "n": Qt.CursorShape.SizeVerCursor,
            "s": Qt.CursorShape.SizeVerCursor,
            "e": Qt.CursorShape.SizeHorCursor,
            "w": Qt.CursorShape.SizeHorCursor,
            "move": Qt.CursorShape.SizeAllCursor,
        }.get(hit or "", Qt.CursorShape.CrossCursor)

    # -- events ----------------------------------------------------------------------
    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        self._scaled = None
        super().resizeEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = event.position()
        hit = self._hit(pos)
        self._drag_origin = pos
        self._drag_crop = self._crop
        if hit is None:
            # Start a brand-new rectangle from this point.
            fx, fy = self._to_frame(pos)
            x, y = int(round(fx)), int(round(fy))
            self._drag = "new"
            self._drag_crop = (x, y, x, y)
            self._set_crop(self._clamp((x, y, x + _MIN_SIZE, y + _MIN_SIZE)), emit=True)
        else:
            self._drag = hit
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position()
        if self._drag is None:
            self.setCursor(self._cursor_for(self._hit(pos)))
            return
        s = self._scale()
        dx = (pos.x() - self._drag_origin.x()) / s
        dy = (pos.y() - self._drag_origin.y()) / s
        left, top, right, bottom = self._drag_crop
        if self._drag == "move":
            w, h = right - left, bottom - top
            fw, fh = self._frame_size
            nl = int(round(max(0, min(left + dx, fw - w))))
            nt = int(round(max(0, min(top + dy, fh - h))))
            self._set_crop((nl, nt, nl + w, nt + h), emit=True)
        elif self._drag == "new":
            fx, fy = self._to_frame(pos)
            x0, y0 = left, top
            x1, y1 = int(round(fx)), int(round(fy))
            crop = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            self._set_crop(self._clamp(crop), emit=True)
        else:
            key = self._drag
            if "w" in key:
                left = min(int(round(left + dx)), right - _MIN_SIZE)
            if "e" in key:
                right = max(int(round(right + dx)), left + _MIN_SIZE)
            if "n" in key:
                top = min(int(round(top + dy)), bottom - _MIN_SIZE)
            if "s" in key:
                bottom = max(int(round(bottom + dy)), top + _MIN_SIZE)
            self._set_crop(self._clamp((left, top, right, bottom)), emit=True)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self._drag = None
            self.setCursor(self._cursor_for(self._hit(event.position())))
            event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """Arrows nudge by 1 px (Shift: 10 px); Ctrl+arrows resize the right/bottom edge."""
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        resize = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        dx = dy = 0
        key = event.key()
        if key == Qt.Key.Key_Left:
            dx = -step
        elif key == Qt.Key.Key_Right:
            dx = step
        elif key == Qt.Key.Key_Up:
            dy = -step
        elif key == Qt.Key.Key_Down:
            dy = step
        else:
            super().keyPressEvent(event)
            return
        left, top, right, bottom = self._crop
        if resize:
            crop = (left, top, max(left + _MIN_SIZE, right + dx), max(top + _MIN_SIZE, bottom + dy))
        else:
            fw, fh = self._frame_size
            w, h = right - left, bottom - top
            nl = max(0, min(left + dx, fw - w))
            nt = max(0, min(top + dy, fh - h))
            crop = (nl, nt, nl + w, nt + h)
        self._set_crop(self._clamp(crop), emit=True)
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), qcolor(theme.BG0))
        image_rect = self._image_rect()

        if self._pixmap is not None:
            target = image_rect.toRect()
            if self._scaled is None or self._scaled.size() != target.size() * self.devicePixelRatioF():
                size = QSize(
                    int(target.width() * self.devicePixelRatioF()),
                    int(target.height() * self.devicePixelRatioF()),
                )
                scaled = self._pixmap.scaled(
                    size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
                scaled.setDevicePixelRatio(self.devicePixelRatioF())
                self._scaled = scaled
            painter.drawPixmap(target.topLeft(), self._scaled)
        else:
            painter.fillRect(image_rect, qcolor(theme.BG2))
            painter.setPen(QPen(qcolor(theme.MUTED)))
            painter.drawText(
                image_rect,
                Qt.AlignmentFlag.AlignCenter,
                "Open the game, then choose Capture frame.",
            )

        # Dim everything outside the crop.
        crop_rect = self._crop_rect_widget()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 110)))
        outside = [
            QRectF(image_rect.left(), image_rect.top(), image_rect.width(), crop_rect.top() - image_rect.top()),
            QRectF(image_rect.left(), crop_rect.bottom(), image_rect.width(), image_rect.bottom() - crop_rect.bottom()),
            QRectF(image_rect.left(), crop_rect.top(), crop_rect.left() - image_rect.left(), crop_rect.height()),
            QRectF(crop_rect.right(), crop_rect.top(), image_rect.right() - crop_rect.right(), crop_rect.height()),
        ]
        for rect in outside:
            if rect.width() > 0 and rect.height() > 0:
                painter.drawRect(rect)

        # OCR boxes.
        if self._boxes:
            box_pen = QPen(qcolor(theme.ACCENT2), 1.0)
            painter.setPen(box_pen)
            painter.setBrush(QBrush(css_qcolor(theme.ACCENT2, 0.12)))
            for box in self._boxes:
                p1 = self._to_widget(box.x, box.y)
                p2 = self._to_widget(box.x + box.w, box.y + box.h)
                painter.drawRect(QRectF(p1, p2))

        # Crop outline + handles.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 160), 3.0))
        painter.drawRect(crop_rect)
        painter.setPen(QPen(qcolor(theme.ACCENT), 1.5))
        painter.drawRect(crop_rect)
        painter.setBrush(QBrush(qcolor(theme.ACCENT)))
        painter.setPen(QPen(qcolor(theme.BG0), 1.0))
        for rect in self._handles().values():
            painter.drawRoundedRect(rect, 2, 2)

        # Size readout in the corner.
        left, top, right, bottom = self._crop
        readout = f"{left}, {top}  -  {right}, {bottom}   ({right - left} x {bottom - top})"
        painter.setPen(QPen(qcolor(theme.TEXT)))
        painter.setBrush(QBrush(css_qcolor(theme.BG0, 0.75)))
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(readout) + 12
        th = metrics.height() + 6
        badge = QRectF(image_rect.right() - tw - 6, image_rect.bottom() - th - 6, tw, th)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(badge, 6, 6)
        painter.setPen(QPen(qcolor(theme.TEXT)))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, readout)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(css_qcolor(theme.ACCENT, 0.6), 1.0))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        painter.end()


def css_qcolor(token: str, alpha: float) -> QColor:
    """``qcolor(token)`` with its alpha replaced by ``alpha`` (0..1)."""
    color = qcolor(token)
    color.setAlphaF(alpha)
    return color


# ----------------------------------------------------------------------------------
# The picker widget
# ----------------------------------------------------------------------------------


class CropPicker(QWidget):
    """Frame preview + crop rectangle + spin boxes + "Capture frame" / "Test OCR".

    Args:
        engine: Provides ``grab_frame()`` and ``test_ocr(frame, crop, cfg)``.
        cfg: Initial configuration (crop and OCR settings).
    """

    crop_changed = Signal(tuple)
    frame_captured = Signal(tuple)
    ocr_test_completed = Signal(int)

    def __init__(self, engine: Engine, cfg: Config, parent: QWidget | None = None,
                 *, calibration_preview: bool = False) -> None:
        super().__init__(parent)
        self._engine = engine
        self._cfg = cfg
        self._frame: np.ndarray | None = None
        self._job: _Job | None = None
        self._calibration_preview = bool(calibration_preview)

        self.setStyleSheet(button_qss())
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Preview.
        preview = QFrame()
        preview.setObjectName("CropPreview")
        preview.setStyleSheet(
            f"QFrame#CropPreview {{ background: {theme.BG0}; border: 1px solid {css_color(theme.LINE)}; "
            "border-radius: 8px; }"
        )
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(4, 4, 4, 4)
        self.canvas = CropCanvas()
        self.canvas.setAccessibleName("Combat chat crop")
        self.canvas.setAccessibleDescription(
            "Drag inside to move, drag a handle to resize, or drag outside to draw a new box. "
            "Arrows nudge 1 pixel, Shift moves 10 pixels, and Ctrl+arrows resize."
        )
        preview_layout.addWidget(self.canvas)
        layout.addWidget(preview, 1)

        # Side panel.
        side = QVBoxLayout()
        side.setSpacing(8)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._capture = QPushButton("Capture frame")
        self._capture.setObjectName("Primary")
        self._capture.setCursor(Qt.CursorShape.PointingHandCursor)
        self._capture.clicked.connect(self.capture_frame)
        self._test = QPushButton("Test OCR")
        self._test.setObjectName("Chip")
        self._test.setCursor(Qt.CursorShape.PointingHandCursor)
        self._test.clicked.connect(self.run_test_ocr)
        self._test.setEnabled(False)
        buttons.addWidget(self._capture)
        buttons.addWidget(self._test)
        buttons.addStretch(1)
        side.addLayout(buttons)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self._spins: dict[str, QSpinBox] = {}
        for index, (key, caption) in enumerate((("left", "Left"), ("top", "Top"), ("right", "Right"), ("bottom", "Bottom"))):
            label = QLabel(caption)
            label.setStyleSheet(f"color: {theme.MUTED};")
            spin = QSpinBox()
            spin.setAccessibleName(f"Crop {caption}")
            label.setBuddy(spin)
            spin.setRange(0, 16384)
            spin.setFixedWidth(96)
            spin.setAlignment(Qt.AlignmentFlag.AlignRight)
            spin.valueChanged.connect(lambda _value, key=key: self._on_spin(key))
            row, col = divmod(index, 2)
            grid.addWidget(label, row, col * 2)
            grid.addWidget(spin, row, col * 2 + 1)
            self._spins[key] = spin
        grid.setColumnStretch(4, 1)
        side.addLayout(grid)

        self._status = QLabel("No frame yet.")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {theme.MUTED};")
        side.addWidget(self._status)

        hint = QLabel("Drag to move; use handles to resize. Drag outside to draw.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px;")
        side.addWidget(hint)

        ocr_caption = QLabel("OCR result")
        ocr_caption.setStyleSheet("font-weight: 600;")
        side.addWidget(ocr_caption)
        self._lines = QListWidget()
        self._lines.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._lines.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._lines.setFrameShape(QFrame.Shape.NoFrame)
        self._lines.setWordWrap(True)
        self._lines.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._lines.setStyleSheet(
            f"QListWidget {{ background: {theme.BG2}; border: 1px solid {css_color(theme.LINE)}; border-radius: 8px; "
            f"padding: 4px; color: {theme.TEXT}; }}"
        )
        side.addWidget(self._lines, 1)

        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(340)
        layout.addWidget(side_widget, 0)

        self.canvas.crop_changed.connect(self._on_canvas_crop)
        self.set_crop(tuple(int(v) for v in cfg.crop))

    # -- public ----------------------------------------------------------------------
    def crop(self) -> Crop:
        """The crop rectangle ``(left, top, right, bottom)`` in full-frame pixels."""
        return self.canvas.crop()

    def set_crop(self, crop: Crop) -> None:
        """Set the rectangle; the spin boxes follow and :attr:`crop_changed` fires on change."""
        self.canvas.set_crop(crop)
        self._sync_spins(self.canvas.crop())

    def set_config(self, cfg: Config) -> None:
        """Use ``cfg`` (engine / scale / preprocess) for the next OCR test."""
        self._cfg = cfg
        if self._frame is not None:
            self.canvas.set_frame(self._display_frame())
        self._lines.clear()
        self.canvas.set_boxes([])

    def set_calibration_preview(self, enabled: bool) -> None:
        """Permit a local setup canvas preview; frame access and OCR stay protected."""
        self._calibration_preview = bool(enabled)
        self.canvas.set_frame(self._display_frame())

    def _display_frame(self) -> np.ndarray | None:
        from mnmparse.privacy import casual_enabled
        if self._frame is not None and casual_enabled(self._cfg) and not self._calibration_preview:
            return np.zeros_like(self._frame)
        return self._frame

    def set_frame(self, frame: np.ndarray | None) -> None:
        """Show ``frame`` in the preview (used by Capture frame; also handy for tests)."""
        self._frame = frame
        self.canvas.set_frame(self._display_frame())
        self._lines.clear()
        self._test.setEnabled(frame is not None)
        w, h = self.canvas.frame_size()
        for key in ("left", "right"):
            self._spins[key].setMaximum(w)
        for key in ("top", "bottom"):
            self._spins[key].setMaximum(h)
        self._sync_spins(self.canvas.crop())
        if frame is None:
            self._status.setText("No frame yet.")
        else:
            self._status.setText(f"Captured {w} × {h}.")
            self.frame_captured.emit((w, h))

    def frame(self) -> np.ndarray | None:
        """The last captured frame, if any."""
        from mnmparse.privacy import casual_enabled
        return np.zeros_like(self._frame) if self._frame is not None and casual_enabled(self._cfg) else self._frame

    def capture_frame(self) -> None:
        """Grab one frame of the game window through the engine (background thread)."""
        if self._job is not None:
            return
        self._status.setText("Capturing a frame…")
        self._run_job(self._engine.grab_frame, self._on_frame_done)

    def run_test_ocr(self) -> None:
        """Run the OCR on the current crop of the captured frame (background thread)."""
        if self._job is not None or self._frame is None:
            return
        frame, crop, cfg = self._frame, self.crop(), self._cfg
        self._status.setText("Running OCR…")

        def work() -> tuple[list[OcrLine], float]:
            t0 = time.perf_counter()
            lines = self._engine.test_ocr(frame, crop, cfg)
            return list(lines), (time.perf_counter() - t0) * 1000.0

        self._run_job(work, self._on_ocr_done)

    # -- internals -------------------------------------------------------------------
    def _run_job(self, fn: Callable[[], Any], on_done: Callable[[Any, BaseException | None], None]) -> None:
        self._set_busy(True)
        job = _Job(fn, self)
        self._job = job

        def finished(result: object, error: object) -> None:
            self._job = None
            self._set_busy(False)
            job.deleteLater()
            on_done(result, error if isinstance(error, BaseException) else None)

        job.done.connect(finished)
        job.start()

    def _set_busy(self, busy: bool) -> None:
        self._capture.setEnabled(not busy)
        self._test.setEnabled(not busy and self._frame is not None)

    def _on_frame_done(self, result: object, error: BaseException | None) -> None:
        if error is not None:
            self._status.setText(f"Capture failed: {error}")
            return
        if not isinstance(result, np.ndarray):
            self._status.setText("No frame arrived. Is the game running?")
            return
        self.set_frame(result)

    def _on_ocr_done(self, result: object, error: BaseException | None) -> None:
        self._lines.clear()
        if error is not None:
            self._status.setText(f"OCR failed: {error}")
            self.canvas.set_boxes([])
            self.ocr_test_completed.emit(0)
            return
        lines, elapsed_ms = result if isinstance(result, tuple) else ([], 0.0)
        left, top, right, _bottom = self.crop()
        boxes: list[OcrBox] = []
        for line in lines:
            from mnmparse.privacy import casual_enabled, safe_event_text
            from mnmparse.parser import parse_line
            text = line.text
            if casual_enabled(self._cfg):
                text = safe_event_text(parse_line(text, time.time(), self._cfg.player_name), self._cfg) or "Text hidden in Carebear Mode"
            width = min(int(len(line.text) * line.h * 0.55) or line.h, right - (left + line.x))
            boxes.append(OcrBox(left + int(line.x), top + int(line.y), max(width, 4), int(line.h), text))
            self._lines.addItem(text)
        self.canvas.set_boxes(boxes)
        self._status.setText(f"{len(boxes)} lines found.")
        self.ocr_test_completed.emit(len(boxes))
        log.info("Test OCR: %d lines in %.0f ms", len(boxes), elapsed_ms)

    def _sync_spins(self, crop: Crop) -> None:
        for key, value in zip(("left", "top", "right", "bottom"), crop, strict=True):
            spin = self._spins[key]
            if spin.value() != value:
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)

    def _on_canvas_crop(self, crop: object) -> None:
        if isinstance(crop, tuple) and len(crop) == 4:
            self._sync_spins(crop)  # type: ignore[arg-type]
            self.crop_changed.emit(tuple(crop))

    def _on_spin(self, key: str) -> None:
        """A spin box changed; ``key`` names it (``left``/``top``/``right``/``bottom``).

        A value that would leave the crop thinner than ``_MIN_SIZE`` px (Left typed past
        Right, Top past Bottom, or past the frame edge) is rejected: the box snaps back
        to the current crop and the status line says why, instead of the canvas
        silently collapsing the rectangle to a sliver at the far edge.
        """
        left = self._spins["left"].value()
        top = self._spins["top"].value()
        right = self._spins["right"].value()
        bottom = self._spins["bottom"].value()
        width, height = self.canvas.frame_size()
        limits = {
            "left": left <= min(right, width) - _MIN_SIZE,
            "top": top <= min(bottom, height) - _MIN_SIZE,
            "right": right >= left + _MIN_SIZE and right <= width,
            "bottom": bottom >= top + _MIN_SIZE and bottom <= height,
        }
        if not limits[key]:
            current = dict(zip(("left", "top", "right", "bottom"), self.canvas.crop(), strict=True))
            self._status.setText(f"Crop rejected: keep it inside the frame and at least {_MIN_SIZE} × {_MIN_SIZE} pixels.")
            self._sync_spins(tuple(current[k] for k in ("left", "top", "right", "bottom")))  # type: ignore[arg-type]
            return
        self.canvas.set_crop((left, top, right, bottom))
        # The canvas clamps; reflect the effective rectangle back into the boxes.
        self._sync_spins(self.canvas.crop())

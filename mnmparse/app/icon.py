"""The application icon, drawn with QPainter so no image assets need to ship.

:func:`make_icon` returns a ``QIcon`` with 256/128/64/48/32/16 px renderings of a dark
rounded square carrying a gold three-bar chart glyph; :func:`write_ico` saves the same
renderings as a multi-size ``.ico`` (used by the desktop shortcut and the PyInstaller exe).
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from mnmparse.app import theme

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PySide6.QtGui import QIcon, QImage, QPixmap

log = logging.getLogger(__name__)

ICON_SIZES: tuple[int, ...] = (256, 128, 64, 48, 32, 16)
"""Pixel sizes rendered into the icon, largest first (the ICO directory order)."""

_BAR_FRACTIONS: tuple[float, ...] = (0.42, 0.72, 1.0)
"""Heights of the three bars relative to the glyph box."""


def render_icon_pixmap(size: int) -> QPixmap:
    """Render the icon at ``size`` x ``size`` physical pixels (antialiased)."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # Background: dark rounded square with a subtle vertical gradient and a hairline.
        inset = size * 0.03
        rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
        radius = size * 0.22
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, theme.qcolor(theme.BG2))
        grad.setColorAt(1.0, theme.qcolor(theme.BG0))
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, grad)
        if size >= 32:
            pen = QPen(QColor(255, 255, 255, 28))
            pen.setWidthF(max(1.0, size / 128.0))
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        # Glyph: three gold bars rising left to right, bottom-aligned.
        painter.setPen(Qt.PenStyle.NoPen)
        box_w = size * 0.58
        box_h = size * 0.56
        box_x = (size - box_w) / 2.0
        box_y = (size - box_h) / 2.0 + size * 0.02
        gap = box_w * 0.12
        bar_w = (box_w - 2 * gap) / 3.0
        bar_radius = max(1.0, bar_w * 0.22) if size >= 32 else 0.0
        for i, frac in enumerate(_BAR_FRACTIONS):
            h = box_h * frac
            x = box_x + i * (bar_w + gap)
            y = box_y + (box_h - h)
            bar = QRectF(x, y, bar_w, h)
            bar_grad = QLinearGradient(bar.topLeft(), bar.bottomLeft())
            bar_grad.setColorAt(0.0, theme.qcolor(theme.lighten(theme.ACCENT, 0.18)))
            bar_grad.setColorAt(1.0, theme.qcolor(theme.ACCENT))
            painter.setBrush(bar_grad)
            if bar_radius > 0:
                painter.drawRoundedRect(bar, bar_radius, bar_radius)
            else:
                painter.drawRect(bar)
    finally:
        painter.end()
    return pm


def make_icon() -> QIcon:
    """Build the application ``QIcon`` with every size in :data:`ICON_SIZES`."""
    from PySide6.QtGui import QIcon

    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(render_icon_pixmap(size))
    return icon


def _png_bytes(image: QImage) -> bytes:
    """Encode ``image`` as PNG with ``QImageWriter``.

    Raises:
        RuntimeError: the PNG plugin is unavailable or encoding failed.
    """
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImageWriter

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buf, b"png")
    if not writer.write(image):
        raise RuntimeError(f"PNG encoding failed: {writer.errorString()}")
    buf.close()
    return bytes(buf.data())


def _ico_container(images: list[tuple[int, bytes]]) -> bytes:
    """Assemble PNG-encoded images into one ICO file (Vista+ PNG entries).

    Args:
        images: ``(size, png_bytes)`` pairs, each a square image of ``size`` px.
    """
    header = struct.pack("<HHH", 0, 1, len(images))
    directory = bytearray()
    payload = bytearray()
    offset = len(header) + 16 * len(images)
    for size, data in images:
        dim = 0 if size >= 256 else size  # 0 encodes 256 in the one-byte width/height
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        payload += data
        offset += len(data)
    return bytes(header) + bytes(directory) + bytes(payload)


def write_ico(path: str | Path) -> Path:
    """Write the icon as a multi-size ``.ico`` file and return the written path.

    The ICO is assembled from PNG-encoded renderings of every size in
    :data:`ICON_SIZES` (Qt's own ``"ico"`` writer only stores one image per file).
    When PNG encoding fails, it falls back to ``QImageWriter("ico")`` with the largest
    rendering, and when that plugin is unavailable too it writes ``<stem>_<size>.png``
    files next to ``path`` and returns the 256 px one (the caller logs a warning).

    Raises:
        OSError: the destination could not be written.
    """
    from PySide6.QtGui import QImageWriter

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        images = [(size, _png_bytes(render_icon_pixmap(size).toImage())) for size in ICON_SIZES]
        dest.write_bytes(_ico_container(images))
        log.info("Wrote icon %s (%d sizes)", dest, len(images))
        return dest
    except RuntimeError as exc:
        log.warning("PNG encoding unavailable (%s); trying Qt's ICO writer", exc)

    formats = {bytes(f).decode("ascii", "ignore") for f in QImageWriter.supportedImageFormats()}
    if "ico" in formats:
        writer = QImageWriter(str(dest), b"ico")
        if writer.write(render_icon_pixmap(ICON_SIZES[0]).toImage()):
            log.info("Wrote single-size icon %s via QImageWriter", dest)
            return dest
        log.warning("QImageWriter(ico) failed: %s", writer.errorString())

    log.warning("ICO plugin unavailable; writing PNG icons next to %s instead", dest)
    first: Path | None = None
    for size in ICON_SIZES:
        png = dest.with_name(f"{dest.stem}_{size}.png")
        if not render_icon_pixmap(size).save(str(png), "PNG"):
            raise OSError(f"could not write {png}")
        first = first or png
    assert first is not None
    return first


def ensure_icon_file(path: str | Path) -> Path | None:
    """Create the ``.ico`` at ``path`` if it is missing; never raises (returns ``None`` on failure)."""
    dest = Path(path)
    if dest.is_file():
        return dest
    try:
        return write_ico(dest)
    except Exception as exc:  # noqa: BLE001 - a missing icon must not stop the app
        log.warning("Could not create %s: %s", dest, exc)
        return None


def main(argv: list[str] | None = None) -> int:
    """``python -m mnmparse.app.icon [PATH]``: write the ICO (default ``assets/icon.ico``).

    Used by ``make_shortcut.ps1`` and ``build_exe.ps1``; runs with the offscreen platform
    so no window is created.
    """
    import os
    import sys

    from mnmparse.config import project_path

    args = list(sys.argv[1:] if argv is None else argv)
    target = Path(args[0]) if args else project_path("assets") / "icon.ico"
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication([sys.argv[0]])
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        written = write_ico(target)
    except OSError as exc:
        log.error("could not write %s: %s", target, exc)
        return 1
    log.info("icon written to %s", written)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry
    raise SystemExit(main())


__all__ = ["ICON_SIZES", "ensure_icon_file", "main", "make_icon", "render_icon_pixmap", "write_ico"]

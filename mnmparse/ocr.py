"""OCR engines and image preprocessing.

:func:`preprocess` turns a BGR crop of the chat window into something the
OCR engine reads well (the chat is drawn at 50% opacity over the 3D scene, so
"brightest channel" is a good text/background separator), optionally
upscaling it.  The engines return :class:`OcrLine` objects whose coordinates
are expressed in the *un-scaled* input image, so callers can mix scales freely.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

import cv2
import numpy as np

from .config import Config

log = logging.getLogger(__name__)

PREPROCESS_MODES: tuple[str, ...] = ("none", "gray", "maxchannel")


class OcrUnavailableError(RuntimeError):
    """The requested OCR engine cannot be created (the CLI maps this to exit code 3)."""


@dataclass
class OcrLine:
    """One visual text line.

    Attributes:
        x: Left edge in input-image pixels.
        y: Top edge in input-image pixels.
        h: Height in input-image pixels.
        text: Recognised text of the line.
    """

    x: int
    y: int
    h: int
    text: str


class OcrEngine(Protocol):
    """Reads text lines from a BGR image."""

    def read(self, bgr: np.ndarray) -> list[OcrLine]:
        """Return the lines sorted top-to-bottom, coordinates in the input image's pixels."""


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------


def _as_bgr(image: np.ndarray) -> np.ndarray:
    """Return ``image`` as an HxWx3 uint8 BGR array (gray and BGRA are converted)."""
    img = np.asarray(image)
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.ndim == 3 and img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    if img.ndim == 3 and img.shape[2] == 3:
        return img
    if img.ndim == 3 and img.shape[2] == 1:
        return cv2.cvtColor(img[:, :, 0], cv2.COLOR_GRAY2BGR)
    raise ValueError(f"Unsupported image shape {img.shape}")


def preprocess(bgr: np.ndarray, mode: str, scale: float) -> np.ndarray:
    """Prepare a BGR image for OCR; always returns an HxWx3 uint8 BGR array.

    Args:
        bgr: Input image (BGR; gray or BGRA inputs are accepted too).
        mode: ``"none"`` (unchanged), ``"gray"`` (luma, replicated to 3
            channels) or ``"maxchannel"`` (per-pixel max of B, G, R replicated
            to 3 channels; separates light text from the dim scene behind it).
        scale: Resize factor; ``1.0`` keeps the size, anything else resizes
            with bicubic interpolation.  Output shape is ``round(h*scale)`` by
            ``round(w*scale)``.

    Raises:
        ValueError: unknown ``mode`` or non-positive ``scale``.
    """
    if mode not in PREPROCESS_MODES:
        raise ValueError(f"Unknown preprocess mode {mode!r}; expected one of {PREPROCESS_MODES}")
    if scale <= 0:
        raise ValueError(f"scale must be positive, got {scale!r}")
    img = _as_bgr(bgr)
    if mode == "gray":
        out = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    elif mode == "maxchannel":
        out = cv2.cvtColor(img.max(axis=2), cv2.COLOR_GRAY2BGR)
    else:
        out = img
    if abs(scale - 1.0) > 1e-9:
        out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return np.ascontiguousarray(out)


def _sorted_lines(lines: list[OcrLine]) -> list[OcrLine]:
    lines.sort(key=lambda ln: (ln.y, ln.x))
    return lines


# ---------------------------------------------------------------------------
# Windows.Media.Ocr
# ---------------------------------------------------------------------------


class WindowsOcr:
    """Windows.Media.Ocr (built into Windows) behind a synchronous :meth:`read`.

    The WinRT ``recognize_async`` coroutine is driven to completion on a
    private, per-thread asyncio loop that is reused between calls, so
    :meth:`read` can be called repeatedly from one thread (or from several
    threads, serialised by a lock) without "event loop is closed" errors.

    Args:
        language: BCP-47 tag; falls back to the user's profile languages when
            that language is not installed.
        scale: The factor :func:`preprocess` was called with.  Coordinates
            of the returned lines are divided by it so they refer to the
            un-scaled image.
    """

    def __init__(self, language: str = "en-US", scale: float = 1.0) -> None:
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.graphics.imaging import (
                BitmapAlphaMode,
                BitmapBufferAccessMode,
                BitmapPixelFormat,
                SoftwareBitmap,
            )
            from winrt.windows.media.ocr import OcrEngine as WinOcrEngine
        except ImportError as exc:
            raise OcrUnavailableError(
                "Windows OCR needs the winrt-Windows.Media.Ocr / Graphics.Imaging / "
                "Globalization packages"
            ) from exc
        self._SoftwareBitmap = SoftwareBitmap
        self._BitmapPixelFormat = BitmapPixelFormat
        self._BitmapAlphaMode = BitmapAlphaMode
        self._BitmapBufferAccessMode = BitmapBufferAccessMode

        engine = None
        try:
            engine = WinOcrEngine.try_create_from_language(Language(language))
        except Exception as exc:  # noqa: BLE001 - bad tag or missing language pack
            log.debug("OcrEngine for %r failed (%s); trying profile languages", language, exc)
        if engine is None:
            engine = WinOcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise OcrUnavailableError(
                f"Windows OCR has no recogniser for {language!r} and none of the user's languages"
            )
        self._engine = engine
        self._max_dim = int(getattr(WinOcrEngine, "max_image_dimension", 0) or 0)
        self.language: str = engine.recognizer_language.language_tag
        self.scale: float = float(scale) if scale > 0 else 1.0
        self._lock = threading.Lock()
        self._tls = threading.local()
        log.info("Windows OCR ready (language %s, scale %.2f)", self.language, self.scale)

    # -- event loop handling ----------------------------------------------

    def _loop(self) -> asyncio.AbstractEventLoop:
        loop: asyncio.AbstractEventLoop | None = getattr(self._tls, "loop", None)
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            self._tls.loop = loop
        return loop

    def _run(self, awaitable: Any) -> Any:  # noqa: ANN401
        """Run a WinRT async operation to completion on this thread's loop."""
        return self._loop().run_until_complete(awaitable)

    def close(self) -> None:
        """Close the calling thread's private event loop (optional tidy-up)."""
        loop: asyncio.AbstractEventLoop | None = getattr(self._tls, "loop", None)
        if loop is not None and not loop.is_closed():
            loop.close()
        self._tls.loop = None

    # -- bitmap conversion --------------------------------------------------

    def _to_software_bitmap(self, bgra: np.ndarray) -> Any:  # noqa: ANN401
        height, width = bgra.shape[:2]
        row_bytes = width * 4
        sb = self._SoftwareBitmap(self._BitmapPixelFormat.BGRA8, width, height, self._BitmapAlphaMode.IGNORE)
        buf = sb.lock_buffer(self._BitmapBufferAccessMode.WRITE)
        try:
            desc = buf.get_plane_description(0)
            ref = buf.create_reference()
            try:
                view = memoryview(ref)
                try:
                    if desc.start_index == 0 and desc.stride == row_bytes:
                        view[: row_bytes * height] = bgra.tobytes()
                    else:  # padded rows: copy one row at a time
                        for row in range(height):
                            offset = desc.start_index + row * desc.stride
                            view[offset : offset + row_bytes] = bgra[row].tobytes()
                finally:
                    view.release()
            finally:
                ref.close()
        finally:
            buf.close()
        return sb

    # -- public API -----------------------------------------------------------

    def read(self, bgr: np.ndarray) -> list[OcrLine]:
        """OCR an (already preprocessed) BGR image.

        Returns:
            Lines sorted by ``y`` (then ``x``), words ordered by ``x`` when
            computing each line's box, with coordinates divided by
            :attr:`scale` so they are in the un-scaled image's pixels.  The
            text of each line is the engine's own line text.
        """
        img = _as_bgr(bgr)
        height, width = img.shape[:2]
        if height == 0 or width == 0:
            return []
        extra = 1.0
        if self._max_dim and max(height, width) > self._max_dim:
            extra = self._max_dim / float(max(height, width))
            log.warning("Image %dx%d exceeds Windows OCR limit %d; downscaling by %.3f",
                        width, height, self._max_dim, extra)
            img = cv2.resize(img, None, fx=extra, fy=extra, interpolation=cv2.INTER_AREA)
        bgra = np.ascontiguousarray(cv2.cvtColor(img, cv2.COLOR_BGR2BGRA))
        bitmap = self._to_software_bitmap(bgra)
        with self._lock:
            result = self._run(self._engine.recognize_async(bitmap))
        undo = self.scale * extra
        lines: list[OcrLine] = []
        for line in result.lines:
            words = sorted(line.words, key=lambda wd: wd.bounding_rect.x)
            if not words:
                continue
            x0 = min(wd.bounding_rect.x for wd in words)
            y0 = min(wd.bounding_rect.y for wd in words)
            y1 = max(wd.bounding_rect.y + wd.bounding_rect.height for wd in words)
            text = (line.text or " ".join(wd.text for wd in words)).strip()
            if not text:
                continue
            lines.append(OcrLine(x=round(x0 / undo), y=round(y0 / undo), h=round((y1 - y0) / undo), text=text))
        return _sorted_lines(lines)


# ---------------------------------------------------------------------------
# RapidOCR (optional, slower, pure ONNX)
# ---------------------------------------------------------------------------


class RapidOcr:
    """RapidOCR (``rapidocr_onnxruntime``) fallback engine.

    Args:
        scale: Same meaning as for :class:`WindowsOcr`.
    """

    def __init__(self, scale: float = 1.0) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise OcrUnavailableError("RapidOCR needs the rapidocr-onnxruntime package") from exc
        try:
            self._ocr = RapidOCR()
        except Exception as exc:  # noqa: BLE001 - model files missing, onnxruntime broken, ...
            raise OcrUnavailableError(f"RapidOCR could not initialise: {exc}") from exc
        self.scale: float = float(scale) if scale > 0 else 1.0
        self._lock = threading.Lock()
        log.info("RapidOCR ready (scale %.2f)", self.scale)

    def read(self, bgr: np.ndarray) -> list[OcrLine]:
        """OCR an (already preprocessed) BGR image; see :meth:`WindowsOcr.read`."""
        img = _as_bgr(bgr)
        if img.shape[0] == 0 or img.shape[1] == 0:
            return []
        with self._lock:
            result, _elapsed = self._ocr(img)
        lines: list[OcrLine] = []
        for item in result or []:
            box, text, _score = item[0], str(item[1]), item[2]
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
            text = text.strip()
            if not text:
                continue
            lines.append(
                OcrLine(
                    x=round(min(xs) / self.scale),
                    y=round(min(ys) / self.scale),
                    h=round((max(ys) - min(ys)) / self.scale),
                    text=text,
                )
            )
        return _sorted_lines(lines)


def make_engine(cfg: Config) -> OcrEngine:
    """Build the OCR engine selected by ``cfg.ocr_engine`` with ``cfg.ocr_scale``.

    Raises:
        ValueError: unknown engine name.
        OcrUnavailableError: the engine's runtime is missing.
    """
    name = cfg.ocr_engine.lower()
    if name == "windows":
        return WindowsOcr(scale=cfg.ocr_scale)
    if name == "rapid":
        return RapidOcr(scale=cfg.ocr_scale)
    raise ValueError(f"Unknown ocr_engine {cfg.ocr_engine!r}; expected 'windows' or 'rapid'")

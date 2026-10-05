"""Crop-region picker: a plain Tk window showing one captured frame.

:func:`pick_crop` displays a *copy* of a captured game frame (scaled down to fit the screen),
draws the current crop rectangle, lets the user drag a new one and returns it in full-frame
pixel coordinates. It is an ordinary application window: not kept on top, not transparent,
not an overlay, and it never touches the game window in any way (SPEC section 1).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger(__name__)

Crop = tuple[int, int, int, int]
"""(left, top, right, bottom) in full-frame pixels; right/bottom are exclusive."""

MAX_DISPLAY_WIDTH = 1600
"""The frame is scaled down so the preview is at most this wide."""

MAX_DISPLAY_HEIGHT = 900
"""...and at most this tall (3840x2160 at 1600 wide is exactly 900 tall)."""

MIN_CROP_PX = 4
"""Drags smaller than this in either dimension are ignored (treated as an accidental click)."""

WINDOW_TITLE = "mnmparse - calibrate"


def _clamp_crop(crop: tuple, width: int, height: int) -> Crop:
    """Normalise a crop to ints, left<right / top<bottom, inside a width x height frame."""
    left, top, right, bottom = (int(round(v)) for v in crop)
    left, right = sorted((left, right))
    top, bottom = sorted((top, bottom))
    left = min(max(left, 0), width)
    right = min(max(right, 0), width)
    top = min(max(top, 0), height)
    bottom = min(max(bottom, 0), height)
    return left, top, right, bottom


def _to_rgb(frame: np.ndarray) -> np.ndarray:
    """BGR / BGRA / grayscale numpy frame -> contiguous RGB (or gray) array for PIL."""
    import numpy as np

    if frame.ndim == 2:
        return np.ascontiguousarray(frame)
    channels = frame.shape[2]
    if channels >= 3:
        return np.ascontiguousarray(frame[:, :, 2::-1])  # B,G,R -> R,G,B (drops alpha)
    return np.ascontiguousarray(frame[:, :, 0])


class _CropPicker:
    """The Tk window; created and run by :func:`pick_crop`."""

    def __init__(self, frame: np.ndarray, current: Crop, scale: float) -> None:
        import tkinter as tk

        from PIL import Image, ImageTk

        self.height, self.width = frame.shape[:2]
        self.scale = scale
        self.crop: Crop = current
        self.result: Crop | None = None
        self._drag_start: tuple[int, int] | None = None

        disp_w = max(1, round(self.width * scale))
        disp_h = max(1, round(self.height * scale))
        image = Image.fromarray(_to_rgb(frame))
        if (disp_w, disp_h) != (self.width, self.height):
            image = image.resize((disp_w, disp_h), Image.Resampling.BILINEAR)

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        self.root.resizable(False, False)
        # Deliberately no always-on-top attribute, no borderless/override mode and no
        # transparency: this is a normal window that sits in the taskbar like any other app.

        tk.Label(
            self.root,
            text="Drag a rectangle around the Combat window text. "
            "Enter or OK saves, Esc or Cancel keeps the current crop.",
            anchor="w",
        ).pack(fill="x", padx=8, pady=(8, 4))

        self.photo = ImageTk.PhotoImage(image, master=self.root)  # keep a reference alive
        self.canvas = tk.Canvas(
            self.root, width=disp_w, height=disp_h, highlightthickness=0, cursor="crosshair"
        )
        self.canvas.pack(padx=8, pady=4)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.rect = self.canvas.create_rectangle(
            *self._to_display(self.crop), outline="#00ff40", width=2
        )

        bottom = tk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=(4, 8))
        self.status = tk.StringVar(master=self.root)
        tk.Label(bottom, textvariable=self.status, anchor="w").pack(
            side="left", fill="x", expand=True
        )
        tk.Button(bottom, text="Cancel", width=10, command=self._cancel).pack(
            side="right", padx=(6, 0)
        )
        tk.Button(bottom, text="OK", width=10, command=self._confirm).pack(side="right")
        self._update_status()

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        for key in ("<Return>", "<KP_Enter>"):
            self.root.bind(key, lambda _e: self._confirm())
        self.root.bind("<Escape>", lambda _e: self._cancel())

    # -- coordinate transforms ---------------------------------------------------------

    def _to_display(self, crop: Crop) -> tuple[int, int, int, int]:
        """Frame pixels -> preview canvas pixels."""
        left, top, right, bottom = crop
        return (
            round(left * self.scale),
            round(top * self.scale),
            round(right * self.scale),
            round(bottom * self.scale),
        )

    def _to_frame(self, dx: int, dy: int) -> tuple[int, int]:
        """Preview canvas pixels -> frame pixels, clamped to the frame."""
        fx = min(max(round(dx / self.scale), 0), self.width)
        fy = min(max(round(dy / self.scale), 0), self.height)
        return fx, fy

    # -- event handlers ----------------------------------------------------------------

    @staticmethod
    def _xy(event: object) -> tuple[int, int]:
        return int(getattr(event, "x", 0)), int(getattr(event, "y", 0))

    def _on_press(self, event: object) -> None:
        self._drag_start = self._to_frame(*self._xy(event))

    def _on_drag(self, event: object) -> None:
        if self._drag_start is None:
            return
        fx, fy = self._to_frame(*self._xy(event))
        sx, sy = self._drag_start
        preview = _clamp_crop((sx, sy, fx, fy), self.width, self.height)
        self.canvas.coords(self.rect, *self._to_display(preview))
        self._update_status(preview)

    def _on_release(self, event: object) -> None:
        if self._drag_start is None:
            return
        fx, fy = self._to_frame(*self._xy(event))
        sx, sy = self._drag_start
        self._drag_start = None
        candidate = _clamp_crop((sx, sy, fx, fy), self.width, self.height)
        if candidate[2] - candidate[0] < MIN_CROP_PX or candidate[3] - candidate[1] < MIN_CROP_PX:
            log.debug("ignoring tiny drag %s", candidate)
        else:
            self.crop = candidate
        self.canvas.coords(self.rect, *self._to_display(self.crop))
        self._update_status()

    def _update_status(self, preview: Crop | None = None) -> None:
        crop = preview if preview is not None else self.crop
        w, h = crop[2] - crop[0], crop[3] - crop[1]
        self.status.set(
            f"crop (left, top, right, bottom) = {crop}   size {w}x{h} px   "
            f"frame {self.width}x{self.height}, preview scale {self.scale:.3f}"
        )

    def _confirm(self) -> None:
        self.result = self.crop
        self.root.quit()

    def _cancel(self) -> None:
        self.result = None
        self.root.quit()

    # -- run ---------------------------------------------------------------------------

    def run(self) -> Crop | None:
        """Show the window, block until OK/Enter or Cancel/Esc/close, then tear it down."""
        self.root.lift()  # raises only our own window; the game window is never touched
        self.root.focus_set()
        try:
            self.root.mainloop()
        finally:
            try:
                self.root.destroy()
            except Exception:  # already destroyed
                log.debug("Tk destroy failed", exc_info=True)
        return self.result


def pick_crop(frame_bgr: np.ndarray, current: tuple) -> Crop | None:
    """Let the user pick a crop rectangle on ``frame_bgr`` in a normal Tk window.

    Args:
        frame_bgr: full game-window frame, HxWx3 BGR (HxWx4 BGRA and grayscale are tolerated).
        current: the current crop ``(left, top, right, bottom)`` in frame pixels; drawn as the
            starting rectangle and returned unchanged if the user confirms without dragging.

    Returns:
        The chosen crop in full-frame pixel coordinates (display scaling undone, clamped to the
        frame), or ``None`` if the user cancelled (Esc, Cancel button or closing the window).
    """
    if frame_bgr is None or getattr(frame_bgr, "ndim", 0) < 2:
        raise ValueError("pick_crop needs an HxW[xC] numpy frame")
    height, width = frame_bgr.shape[:2]
    if width == 0 or height == 0:
        raise ValueError("pick_crop needs a non-empty frame")
    scale = min(1.0, MAX_DISPLAY_WIDTH / width, MAX_DISPLAY_HEIGHT / height)
    start = _clamp_crop(current, width, height)
    log.info(
        "calibrate: frame %dx%d, preview scale %.3f, current crop %s", width, height, scale, start
    )
    result = _CropPicker(frame_bgr, start, scale).run()
    log.info("calibrate: result %s", result)
    return result

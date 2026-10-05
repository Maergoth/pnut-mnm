"""Render the real timer overlay into a small, reproducible README animation.

Run with the development environment: python scripts/capture_timer_demo.py
Uses sample timers and a deterministic clock at 4x speed. No capture, audio,
network, installed app, or personal settings are opened. All Qt windows render
offscreen; the temporary settings file is deleted when the script finishes.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ["SystemRoot"]) / "Fonts"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image
from PySide6.QtCore import QRect, QSettings, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.theme import apply_theme
from mnmparse.app.timer_panel import TimerPanel
from mnmparse.triggers import TimerBoard, Trigger


class _OffscreenAnchor(QWidget):
    """Only the docking contract; no overlay runtime or game capture is needed."""

    locked = True
    click_through = False
    opacity = 1.0

    def dock_anchor(self, _panel: QWidget) -> QRect:
        return QRect(0, 0, 720, 1)

    def panels_changed(self) -> None:
        pass


def _frame(panel: TimerPanel) -> Image.Image:
    image = panel.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    rgba = Image.frombytes("RGBA", (image.width(), image.height()), image.bits().tobytes())
    # Flatten only the translucent rounded corners for reliable GIF rendering.
    background = Image.new("RGB", rgba.size, "#0d1117")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def main() -> int:
    app = QApplication([sys.argv[0]])
    apply_theme(app)
    output = ROOT / "docs" / "images" / "timers-demo.gif"
    review = ROOT / "dist" / "review" / "timers-demo-contact-sheet.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    review.parent.mkdir(parents=True, exist_ok=True)
    frames: list[Image.Image] = []
    with tempfile.TemporaryDirectory(prefix="pnut-timer-demo-") as temp:
        settings = QSettings(str(Path(temp) / "demo.ini"), QSettings.Format.IniFormat)
        owner = _OffscreenAnchor()
        owner.show()
        panel = TimerPanel(owner, settings, Qt.WindowType.FramelessWindowHint)
        board = TimerBoard()
        for name, seconds, warning in (
            ("Root · a sunbleached sentinel", 26, 12),
            ("Pacify · a dunes prowler", 42, 15),
            ("Rebuff · group", 68, 20),
        ):
            board.start(Trigger(name=name, timer=True, timer_seconds=seconds,
                                timer_warn_s=warning, timer_low_s=5), name, 1_000.0)
        panel.runner = SimpleNamespace(board=board)
        panel.set_font_px(24)
        # 70 frames at 10 fps, each advancing the sample clock by 0.4 seconds.
        for index in range(70):
            now = 1_000.0 + index * 0.4
            with patch("mnmparse.app.timer_panel.time.time", return_value=now):
                board.tick(now)
                panel.refresh()
                panel._anim.stop()
                app.processEvents()
                frames.append(_frame(panel))
        panel.hide()
        owner.hide()
        settings.sync()
        panel.deleteLater()
        owner.deleteLater()
        app.processEvents()

    # A shared palette includes every timer state and prevents color flicker.
    keyframes = [frames[index] for index in (0, 36, 54, 66)]
    contact = Image.new("RGB", (frames[0].width, frames[0].height * len(keyframes)), "#0d1117")
    for index, frame in enumerate(keyframes):
        contact.paste(frame, (0, index * frame.height))
    contact.save(review)
    palette = contact.quantize(colors=256)
    encoded = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    durations = [100] * len(encoded)
    durations[-1] = 1_100  # Pause at expiry before looping: eight seconds in total.
    encoded[0].save(output, save_all=True, append_images=encoded[1:], duration=durations,
                    loop=0, optimize=True, disposal=1)
    with Image.open(output) as result:
        if result.n_frames < 50 or output.stat().st_size >= 3_000_000:
            raise RuntimeError("Animation must retain its frames and stay below 3 MB")
        print(f"{output}: {result.width}x{result.height}, {result.n_frames} frames, "
              f"{output.stat().st_size:,} bytes; sample timers at 4x speed")
    print(f"Review contact sheet: {review}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

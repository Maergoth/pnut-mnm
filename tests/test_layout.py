"""Layout guarantees: nothing in the main window or the overlay is clipped.

The project's rule: the window must never get so narrow that text gets crunched out of
existence.  These tests build the real windows (offscreen), put them at their minimum
and at a wide size, and check every visible widget: no button or single-line label is
narrower than its text, no widget is squeezed below its minimum, and no widget spills out
of its parent.  Narrow meters hide their optional columns instead of clipping them.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":  # real Windows fonts on the offscreen platform (realistic text widths)
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    from PySide6.QtCore import QSettings, QSize, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QAbstractButton,
        QAbstractScrollArea,
        QApplication,
        QLabel,
        QPushButton,
        QScrollArea,
        QWidget,
    )

    HAVE_QT = True
except ImportError:  # pragma: no cover - the app is optional
    HAVE_QT = False

from mnmparse.config import Config

PLAYER = "Maergoth"


def _qapp() -> Any:
    app = QApplication.instance()
    return app if app is not None else QApplication([sys.argv[0]])


def _inside_scroll_area(w: Any) -> bool:
    parent = w.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return True
        parent = parent.parentWidget()
    return False


def clipped_widgets(root: Any) -> list[str]:
    """Every visible widget under ``root`` whose text or content does not fit."""
    from mnmparse.app.widgets import ElidedLabel

    problems: list[str] = []
    for w in root.findChildren(QWidget):
        if not w.isVisibleTo(root) or w.width() <= 0:
            continue
        name = f"{type(w).__name__}#{w.objectName()}"
        if isinstance(w, QLabel) and not isinstance(w, ElidedLabel) and not w.wordWrap() and w.text():
            if (w.pixmap() is None or w.pixmap().isNull()) and w.width() + 1 < w.minimumSizeHint().width():
                problems.append(f"{name} {w.text()!r}: {w.width()} px < {w.minimumSizeHint().width()}")
        if isinstance(w, QLabel) and w.wordWrap() and w.text() and w.heightForWidth(w.width()) > w.height() + 1:
            # a wrapped label given fewer lines than its text needs (the last line(s) vanish)
            problems.append(f"{name} {w.text()[:40]!r}: {w.height()} px tall, needs {w.heightForWidth(w.width())}")
        if isinstance(w, QAbstractButton) and w.text():
            hint = w.sizeHint()
            if w.width() + 1 < hint.width() or w.height() + 1 < hint.height():
                problems.append(f"{name} {w.text()!r}: {w.width()}x{w.height()} < {hint.width()}x{hint.height()}")
        if not isinstance(w, (QLabel, QAbstractButton)) and not _inside_scroll_area(w):
            hint = w.minimumSizeHint()
            if hint.isValid() and (w.width() + 1 < hint.width() or w.height() + 1 < hint.height()):
                problems.append(f"{name} squeezed: {w.width()}x{w.height()} < {hint.width()}x{hint.height()}")
        if isinstance(w, QScrollArea) and w.widget() is not None:
            if w.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
                need = w.widget().minimumSizeHint().width()
                if need > w.viewport().width() + 1:
                    problems.append(f"{name} content needs {need} px, view is {w.viewport().width()}")
        parent = w.parentWidget()
        if parent is not None and not w.isWindow() and not _inside_scroll_area(w):
            g = w.geometry()
            if g.right() > parent.width() + 1 or g.bottom() > parent.height() + 1 or g.left() < -1 or g.top() < -1:
                problems.append(f"{name} spills out of {type(parent).__name__}: {g} in {parent.size()}")
    return problems


def _actor(name: str, dmg: int, *, you: bool = False, npc: bool = False) -> Any:
    from mnmparse.app import theme
    from mnmparse.app.models import ActorRow, SkillRow

    row = ActorRow(
        name=name, damage=dmg, dps=dmg / 30, taken=dmg // 3, dtps=dmg / 90, heals=dmg // 2, hps=dmg / 60,
        healed=0, swings=40, hits=33, misses=7, hit_pct=82.5, max_hit=dmg // 5, avg_hit=dmg / 33, share=0.3,
        color=theme.actor_color(name, is_you=you, is_npc=npc), is_you=you, is_npc=npc, is_pet=False,
        skills=[SkillRow("Crusader Strike", 10, dmg // 2, dmg // 5, dmg / 20, 10, 1), SkillRow("crush", 23, dmg // 2, dmg // 6, dmg / 40, 23, 6)],
    )
    row.utility = 4
    row.cc_types = {"stun": 2, "interrupt": 1}
    row.aggro = 1
    row.prevented = 37
    return row


def _snap(start: float, zone: str = "", *, closed: bool = True, label: str = "a skeletal cavalier, a skeletal warrior") -> Any:
    from mnmparse.app.models import EncounterSnapshot

    rows = [_actor(PLAYER, 12000, you=True), _actor("Povebizu", 9000), _actor("Dogabetarolem", 7000), _actor("a skeletal warrior", 3000, npc=True)]
    return EncounterSnapshot(
        key=f"{start:.3f}", label=label, start=start, end=start + 38, duration=38.0, closed=closed,
        event_count=82, total_damage=31000, raid_dps=815.6, killed=["a skeletal warrior"], rows=rows, zone=zone,
    )


def _offscreen(widget: Any) -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)


def _settle(app: Any, ms: int = 0) -> None:
    for _ in range(4):
        app.processEvents()
    if ms:
        QTest.qWait(ms)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class MainWindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from mnmparse.app import theme

        cls.app = _qapp()
        theme.apply_theme(cls.app)

    def _window(self, tmp: str) -> Any:
        from mnmparse.app.main import MainWindow, _MissingEngine

        cfg = Config(player_name=PLAYER)
        settings = QSettings(str(Path(tmp) / "layout.ini"), QSettings.Format.IniFormat)
        win = MainWindow(_MissingEngine(cfg), None, cfg, settings)
        _offscreen(win)
        live = win.page("live")
        t0 = time.time() - 3600
        for i, zone in enumerate(["", "", "Night Harbor (East)", "Night Harbor (East)", "Wyrmsbane Tomb", "Wyrmsbane Tomb"]):
            live.add_encounter(_snap(t0 + i * 120, zone))
        live.set_snapshot(_snap(t0 + 900, "Wyrmsbane Tomb", closed=False, label="a skeletal monk"))
        return win

    def _check_every_page(self, win: Any) -> None:
        live = win.page("live")
        for metric in ("overview", "damage", "healing", "taken"):
            win.show_page("live")
            live.set_metric(metric)
            _settle(self.app)
            self.assertEqual(clipped_widgets(win), [], f"live/{metric} at {win.size()}")
        for page in ("session", "feed", "settings", "about"):
            win.show_page(page)
            _settle(self.app)
            self.assertEqual(clipped_widgets(win), [], f"{page} at {win.size()}")

    def test_nothing_clips_at_the_minimum_size(self) -> None:
        from mnmparse.app.main import MIN_WINDOW_SIZE

        with tempfile.TemporaryDirectory() as tmp:
            win = self._window(tmp)
            try:
                win.resize(MIN_WINDOW_SIZE)
                win.show()
                _settle(self.app, 20)
                self.assertGreaterEqual(win.width(), MIN_WINDOW_SIZE.width())
                self._check_every_page(win)
                # the window refuses to get smaller than its content needs
                win.resize(QSize(400, 300))
                _settle(self.app)
                self.assertGreaterEqual(win.width(), MIN_WINDOW_SIZE.width())
                self.assertGreaterEqual(win.height(), MIN_WINDOW_SIZE.height())
                self._check_every_page(win)
            finally:
                win.close()
                win.deleteLater()
                _settle(self.app)

    def test_nothing_clips_when_wide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            win = self._window(tmp)
            try:
                win.resize(QSize(1600, 950))
                win.show()
                _settle(self.app, 20)
                self._check_every_page(win)
            finally:
                win.close()
                win.deleteLater()
                _settle(self.app)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class WidgetLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def test_meter_hides_optional_columns_instead_of_clipping(self) -> None:
        from mnmparse.app.widgets import MeterTable

        table = MeterTable(compact=False)
        _offscreen(table)
        try:
            table.set_rows(_snap(1.0).rows, "damage")
            table.resize(1200, 300)
            table.show()
            _settle(self.app)
            self.assertEqual(table.hidden_columns(), [], "a wide meter shows every column")
            table.resize(table.minimum_width(), 300)
            _settle(self.app)
            hidden = table.hidden_columns()
            self.assertNotIn("name", hidden)
            self.assertNotIn("damage", hidden, "the metric's own number always stays")
            self.assertEqual(set(hidden), {"rank", "dps", "share", "hit_pct", "max"})
            # a little wider than "everything fits minus the rank column": only rank goes
            widths = {c.key: table._column_width(c) for c in table._model.columns if not c.stretch}
            just_rank = sum(widths.values()) - widths["rank"] + table.name_min_width() + 4
            table.resize(just_rank + 4, 300)  # the view's viewport is a few px narrower than the table
            _settle(self.app)
            self.assertIn("rank", table.hidden_columns())
            self.assertNotIn("dps", table.hidden_columns())
        finally:
            table.deleteLater()
            _settle(self.app)

    def test_overlay_minimum_width_fits_tabs_and_meter(self) -> None:
        from mnmparse.app.overlay import OverlayWindow

        with tempfile.TemporaryDirectory() as tmp:
            settings = QSettings(str(Path(tmp) / "overlay.ini"), QSettings.Format.IniFormat)
            for scale in (1.0, 1.5):
                overlay = OverlayWindow(settings, Config(overlay_font_scale=scale))
                _offscreen(overlay)
                try:
                    overlay.set_font_scale(scale)
                    self.assertGreaterEqual(overlay.minimumWidth(), overlay._tabs.needed_width())
                    self.assertGreaterEqual(overlay.minimumWidth(), overlay._table.minimum_width())
                    overlay.resize(overlay.minimumWidth(), 300)
                    overlay.show()
                    overlay.set_snapshot(_snap(1.0, closed=False))
                    _settle(self.app, 150)
                    for tab in ("overview", "damage", "healing", "taken", "session", "feed"):
                        overlay.set_tab(tab)
                        _settle(self.app)
                        self.assertEqual(clipped_widgets(overlay), [], f"overlay {tab} at x{scale}")
                    overlay.set_tab("overview")
                    _settle(self.app)
                    self.assertNotIn("dps", overlay._table.hidden_columns())
                finally:
                    overlay.close()
                    overlay.deleteLater()
                    _settle(self.app)

    def test_encounter_header_moves_stats_to_a_second_row_when_narrow(self) -> None:
        from mnmparse.app.pages import _EncounterHeader

        header = _EncounterHeader()
        _offscreen(header)
        try:
            header.add_action(QPushButton("Reset encounter"))
            header.set_snapshot(_snap(1.0, "Wyrmsbane Tomb", closed=False))
            header.resize(1100, 80)
            header.show()
            _settle(self.app)
            self.assertTrue(header.is_wide())
            header.resize(header.minimumSizeHint().width(), 140)
            _settle(self.app)
            self.assertFalse(header.is_wide(), "stats wrap below the title instead of clipping")
            self.assertEqual(clipped_widgets(header), [])
            header.resize(1100, 80)
            _settle(self.app)
            self.assertTrue(header.is_wide())
        finally:
            header.deleteLater()
            _settle(self.app)

    def test_segment_button_has_room_for_its_bold_checked_text(self) -> None:
        from PySide6.QtGui import QFont, QFontMetrics

        from mnmparse.app.pages import _SegmentButton

        for text in ("Overview", "Damage", "Healing", "Taken"):
            button = _SegmentButton(text)
            try:
                button.setChecked(True)
                bold = QFont(button.font())
                bold.setWeight(QFont.Weight.DemiBold)
                self.assertGreaterEqual(button.sizeHint().width(), QFontMetrics(bold).horizontalAdvance(text), text)
            finally:
                button.deleteLater()

    def test_flow_layout_wraps_and_never_exceeds_the_line(self) -> None:
        from mnmparse.app.widgets import FlowLayout

        box = QWidget()
        _offscreen(box)
        try:
            flow = FlowLayout(box, h_spacing=6, v_spacing=6)
            buttons = [QPushButton(f"Zone number {i}") for i in range(6)]
            for b in buttons:
                flow.addWidget(b)
            width = buttons[0].sizeHint().width() * 2 + 40
            box.resize(width, flow.heightForWidth(width))
            box.show()
            _settle(self.app)
            rows = {b.geometry().top() for b in buttons}
            self.assertGreater(len(rows), 1, "items wrap onto more lines")
            for b in buttons:
                self.assertLessEqual(b.geometry().right(), width)
            self.assertGreaterEqual(flow.heightForWidth(width), max(b.geometry().bottom() for b in buttons))
        finally:
            box.deleteLater()
            _settle(self.app)


if __name__ == "__main__":
    unittest.main()

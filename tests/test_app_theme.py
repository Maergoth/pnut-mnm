"""Theme, icon and main-window-shell tests for the desktop app.

The colour helpers are pure Python.  The Qt parts run on the ``offscreen`` platform and
are skipped when PySide6 is not installed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":  # real Windows fonts on the offscreen platform (realistic text widths)
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from mnmparse import grammar  # noqa: E402
from mnmparse.app import theme  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication  # noqa: E402

    HAVE_QT = True
except ImportError:  # pragma: no cover - environment dependent
    HAVE_QT = False

MIN_CONTRAST = 3.0
"""Every text colour must reach at least this WCAG ratio against the darkest background."""


def _qapp() -> "QApplication":
    app = QApplication.instance()
    return app if app is not None else QApplication([sys.argv[0]])


class ActorColorTests(unittest.TestCase):
    """``actor_color`` is stable, case-insensitive and honours the role precedence."""

    def test_same_name_same_color(self) -> None:
        self.assertEqual(theme.actor_color("Tovozen"), theme.actor_color("Tovozen"))
        self.assertEqual(theme.actor_color("Tovozen"), theme.actor_color("tovozen"))
        self.assertEqual(theme.actor_color("  Tovozen "), theme.actor_color("Tovozen"))

    def test_party_colors_come_from_palette(self) -> None:
        for name in ("Tovozen", "Pidef", "Wululiso", "Maergoth", "Sigito", "Garth"):
            self.assertIn(theme.actor_color(name), theme.PARTY)

    def test_names_spread_over_palette(self) -> None:
        names = [f"Player{i}" for i in range(64)]
        used = {theme.actor_color(n) for n in names}
        self.assertGreaterEqual(len(used), len(theme.PARTY) - 1)

    def test_role_precedence(self) -> None:
        self.assertEqual(theme.actor_color("Maergoth", is_you=True), theme.YOU)
        self.assertEqual(theme.actor_color("Maergoth", is_you=True, is_npc=True, is_pet=True), theme.YOU)
        self.assertEqual(theme.actor_color("Fluffy", is_pet=True, is_npc=True), theme.PET)
        self.assertEqual(theme.actor_color("a stumbling zombie", is_npc=True), theme.NPC)

    def test_stable_across_runs(self) -> None:
        # crc32-based, so the mapping must not depend on Python's per-process hash seed.
        self.assertEqual(theme.actor_color("Tovozen"), "#5bc0eb")
        self.assertEqual(theme.actor_color("Pidef"), "#5bc0eb")


class KindColorTests(unittest.TestCase):
    """``KIND_COLORS`` covers every parser kind; ``feed_color`` applies the you/danger rules."""

    def test_every_kind_has_a_color(self) -> None:
        for kind in grammar.KINDS:
            self.assertIn(kind, theme.KIND_COLORS, kind)

    def test_feed_color_rules(self) -> None:
        self.assertEqual(theme.feed_color("melee_hit", is_player_action=True), theme.YOU)
        self.assertEqual(theme.feed_color("ability_hit", is_player_action=True), theme.YOU)
        self.assertEqual(theme.feed_color("melee_hit", is_player_target=True), theme.DANGER)
        self.assertEqual(theme.feed_color("melee_hit"), theme.TEXT)
        self.assertEqual(theme.feed_color("melee_miss", is_player_action=True), theme.MUTED)
        self.assertEqual(theme.feed_color("heal"), theme.SUCCESS)
        self.assertEqual(theme.feed_color("kill"), theme.ACCENT)
        self.assertEqual(theme.feed_color("cast"), theme.ACCENT2)
        self.assertEqual(theme.feed_color("no-such-kind"), theme.KIND_COLORS["unknown"])


class ContrastTests(unittest.TestCase):
    """Every colour that carries text reads against BG0 and BG1."""

    def test_text_colors_contrast(self) -> None:
        colors = {
            "TEXT": theme.TEXT,
            "MUTED": theme.MUTED,
            "ACCENT": theme.ACCENT,
            "ACCENT2": theme.ACCENT2,
            "DANGER": theme.DANGER,
            "SUCCESS": theme.SUCCESS,
            "YOU": theme.YOU,
            "NPC": theme.NPC,
            "PET": theme.PET,
        }
        colors.update({f"PARTY[{i}]": c for i, c in enumerate(theme.PARTY)})
        colors.update({f"KIND[{k}]": c for k, c in theme.KIND_COLORS.items()})
        for name, color in colors.items():
            for bg in (theme.BG0, theme.BG1):
                with self.subTest(color=name, bg=bg):
                    self.assertGreaterEqual(theme.contrast_ratio(color, bg), MIN_CONTRAST)

    def test_luminance_endpoints(self) -> None:
        self.assertAlmostEqual(theme.relative_luminance("#000000"), 0.0)
        self.assertAlmostEqual(theme.relative_luminance("#ffffff"), 1.0)
        self.assertAlmostEqual(theme.contrast_ratio("#000000", "#ffffff"), 21.0)


class ColorHelperTests(unittest.TestCase):
    def test_rgba(self) -> None:
        self.assertEqual(theme.rgba("#0f1117", 0.85), "rgba(15,17,23,0.850)")
        self.assertEqual(theme.rgba("#ffffff", 2.0), "rgba(255,255,255,1.000)")

    def test_mix_and_lighten(self) -> None:
        self.assertEqual(theme.mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(theme.mix("#123456", "#ffffff", 0.0), "#123456")
        self.assertEqual(theme.lighten("#000000", 1.0), "#ffffff")

    def test_bad_hex_raises(self) -> None:
        with self.assertRaises(ValueError):
            theme.rgba("#fff", 1.0)

    def test_overlay_qss_uses_opacity(self) -> None:
        qss = theme.overlay_qss(0.85)
        self.assertIn("rgba(15,17,23,0.850)", qss)
        self.assertIn("rgba(15,17,23,1.000)", theme.overlay_qss(5.0))


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class QtThemeTests(unittest.TestCase):
    """``apply_theme`` and the font helpers against a real (offscreen) QApplication."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def test_apply_theme(self) -> None:
        theme.apply_theme(self.app)
        # With a style sheet active, app.style() is Qt's QStyleSheetStyle proxy over Fusion.
        self.assertIn(self.app.style().metaObject().className(), ("QStyleSheetStyle", "QFusionStyle"))
        self.assertIn("QPushButton[class=\"primary\"]", self.app.styleSheet())
        self.assertEqual(self.app.font().pixelSize(), theme.MAIN_FONT_PX)

    def test_app_font_and_tabular(self) -> None:
        font = theme.app_font(13)
        self.assertEqual(font.pixelSize(), 13)
        self.assertEqual(font.families()[0], theme.FONT_FAMILY)
        self.assertIn(theme.FONT_FALLBACK, font.families())
        numbers = theme.number_font(14)  # must not raise, whatever the Qt version
        self.assertEqual(numbers.pixelSize(), 14)

    def test_qcolor(self) -> None:
        c = theme.qcolor(theme.ACCENT, 0.5)
        self.assertEqual((c.red(), c.green(), c.blue()), (0xF0, 0xB3, 0x5B))
        self.assertEqual(c.alpha(), 128)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class IconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def test_make_icon_sizes(self) -> None:
        from mnmparse.app import icon

        sizes = {(s.width(), s.height()) for s in icon.make_icon().availableSizes()}
        self.assertEqual(sizes, {(s, s) for s in icon.ICON_SIZES})

    def test_write_ico_multi_size(self) -> None:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QImageReader

        from mnmparse.app import icon

        with tempfile.TemporaryDirectory() as tmp:
            out = icon.write_ico(Path(tmp) / "sub" / "icon.ico")
            self.assertTrue(out.is_file())
            self.assertEqual(out.suffix, ".ico")
            data = out.read_bytes()  # read through a buffer so no file handle outlives the temp dir
        self.assertEqual(data[:6], b"\x00\x00\x01\x00\x06\x00")  # ICONDIR: type 1, 6 images
        buffer = QBuffer()
        buffer.setData(QByteArray(data))  # the buffer owns a copy; nothing points at a temporary
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer, b"ico")
        self.assertTrue(reader.canRead())
        self.assertEqual(reader.imageCount(), len(icon.ICON_SIZES))
        self.assertEqual(reader.size().width(), icon.ICON_SIZES[0])


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class MainWindowShellTests(unittest.TestCase):
    """The shell starts with placeholder pages and no overlay, and routes state/status."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def _settings(self, tmp: str):
        from PySide6.QtCore import QSettings

        return QSettings(str(Path(tmp) / "test.ini"), QSettings.Format.IniFormat)

    def test_shell_with_missing_pages(self) -> None:
        from mnmparse.app import main as app_main
        from mnmparse.config import Config

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(app_main._pages_mod, {}, clear=True):
            settings = self._settings(tmp)
            engine = app_main._MissingEngine(Config())
            window = app_main.MainWindow(engine, None, Config(minimize_to_tray=False), settings)
            try:
                self.assertEqual(window.size().width(), 1180)
                self.assertEqual(window.size().height(), 760)
                for key, _label in app_main.PAGES:
                    self.assertIsInstance(window.page(key), app_main._MissingPage)
                window.show_page("feed")
                self.assertIs(window._stack.currentWidget(), window.page("feed"))
                self.assertTrue(window._nav_buttons["feed"].isChecked())

                window.on_state("running")
                self.assertEqual(window._start_button.text(), "Stop capture")
                window.on_status(
                    {"state": "running", "fps": 3.9, "ocr_ms": 31.0, "frames": 10, "messages": 5,
                     "occluded": 1, "window_found": True, "lines": 12}
                )
                window.on_state("stopped")
                self.assertEqual(window._start_button.text(), "Start capture")

                quits: list[bool] = []
                window.quit_requested.connect(lambda: quits.append(True))
                window.close()
                self.assertEqual(quits, [True])
                self.assertEqual(settings.value("main/page"), "feed")
            finally:
                window.deleteLater()
                self.app.processEvents()

    def test_minimise_to_tray_hides(self) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        from mnmparse.app import main as app_main
        from mnmparse.config import Config

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(app_main._pages_mod, {}, clear=True), \
                mock.patch.object(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)):
            settings = self._settings(tmp)
            engine = app_main._MissingEngine(Config())
            window = app_main.MainWindow(engine, None, Config(minimize_to_tray=True), settings)
            try:
                hidden: list[bool] = []
                window.minimised_to_tray.connect(lambda: hidden.append(True))
                window.show()
                window.close()
                self.assertEqual(hidden, [True])
                self.assertFalse(window.isVisible())
            finally:
                window.deleteLater()
                self.app.processEvents()

    def test_player_action_helpers(self) -> None:
        from mnmparse.app import main as app_main
        from mnmparse.parser import parse_line

        ev = parse_line("You crush a stumbling zombie for 8 points of damage.", 1.0, "Maergoth")
        self.assertTrue(app_main.is_player_action(ev, "Maergoth"))
        self.assertFalse(app_main.is_player_target(ev, "Maergoth"))
        ev = parse_line("a stumbling zombie bites YOU for 20 points of damage.", 1.0, "")
        self.assertFalse(app_main.is_player_action(ev, ""))
        self.assertTrue(app_main.is_player_target(ev, ""))
        ev = parse_line("Tovozen crushes a stumbling zombie for 2 points of damage.", 1.0, "Maergoth")
        self.assertFalse(app_main.is_player_action(ev, "Maergoth"))

    def test_nav_icon_states(self) -> None:
        from PySide6.QtGui import QIcon

        from mnmparse.app import main as app_main

        icon = app_main.nav_icon("settings")
        self.assertFalse(icon.isNull())
        self.assertFalse(icon.pixmap(22, QIcon.Mode.Normal, QIcon.State.On).isNull())


if __name__ == "__main__":
    unittest.main()

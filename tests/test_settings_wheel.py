"""Wheel gestures scroll forms without silently editing the setting under them."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication, QComboBox, QDoubleSpinBox, QScrollArea, QSlider,
        QSpinBox, QVBoxLayout, QWidget,
    )
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SettingsWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.windows = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = QSettings(str(Path(self.tmp.name) / "ui.ini"), QSettings.Format.IniFormat)

    def tearDown(self):
        for widget in self.windows:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def wheel(self, widget, *, angle=-120, pixels=0):
        point = widget.rect().center()
        event = QWheelEvent(
            QPointF(point), QPointF(widget.mapToGlobal(point)),
            QPoint(0, pixels), QPoint(0, angle), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(widget, event)
        self.app.processEvents()

    def form(self):
        from mnmparse.app.widgets import prevent_wheel_value_changes

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setValue(50)
        double = QDoubleSpinBox()
        double.setRange(0, 100)
        double.setValue(50.5)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems([f"Choice {i}" for i in range(100)])
        combo.setCurrentIndex(50)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setValue(50)
        for control in (spin, double, combo, slider):
            layout.addWidget(control)
        content.setMinimumHeight(1800)
        scroll.setWidget(content)
        scroll.resize(500, 250)
        prevent_wheel_value_changes(content)
        scroll.show()
        self.app.processEvents()
        self.windows.append(scroll)
        return scroll, content, spin, double, combo, slider

    def test_focused_and_unfocused_controls_and_embedded_editors_scroll_the_form(self):
        scroll, content, spin, double, combo, slider = self.form()
        values = (spin.value(), double.value(), combo.currentIndex(), combo.currentText(), slider.value())
        changes = []
        for signal in (spin.valueChanged, double.valueChanged, combo.currentIndexChanged, slider.valueChanged):
            signal.connect(lambda *_: changes.append(True))
        for focused in (False, True):
            for target in (spin, spin.lineEdit(), double, double.lineEdit(), combo, combo.lineEdit(), slider):
                with self.subTest(focused=focused, control=type(target).__name__):
                    scroll.verticalScrollBar().setValue(0)
                    if focused:
                        target.setFocus()
                    else:
                        target.clearFocus()
                    self.wheel(target)
                    self.assertEqual(values, (spin.value(), double.value(), combo.currentIndex(), combo.currentText(), slider.value()))
                    self.assertGreater(scroll.verticalScrollBar().value(), 0)
        self.assertEqual(changes, [], "Scrolling must not emit changes that save or apply settings")
        scroll.verticalScrollBar().setValue(0)
        self.wheel(spin.lineEdit(), angle=0, pixels=-40)
        self.assertGreater(scroll.verticalScrollBar().value(), 0, "Trackpad scrolling still reaches the page")

    def test_keyboard_typing_and_button_clicks_still_edit_values(self):
        _, _, spin, _, combo, slider = self.form()
        spin.setFocus()
        QTest.keyClick(spin, Qt.Key.Key_Up)
        self.assertEqual(spin.value(), 51)
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin.lineEdit(), "37")
        QTest.keyClick(spin.lineEdit(), Qt.Key.Key_Enter)
        self.assertEqual(spin.value(), 37)
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=QPoint(spin.width() - 5, spin.height() // 4))
        self.assertEqual(spin.value(), 38)
        combo.setFocus()
        QTest.keyClick(combo, Qt.Key.Key_Down)
        self.assertEqual(combo.currentIndex(), 51)
        slider.setFocus()
        QTest.keyClick(slider, Qt.Key.Key_Right)
        self.assertEqual(slider.value(), 51)

    def test_opened_dropdown_keeps_normal_choice_scrolling_and_selection(self):
        _, _, _, _, combo, _ = self.form()
        combo.showPopup()
        self.app.processEvents()
        view = combo.view()
        bar = view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(0)
        self.wheel(view.viewport())
        self.assertGreater(bar.value(), 0)
        index = view.indexAt(QPoint(10, view.viewport().height() // 2))
        self.assertTrue(index.isValid())
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(index).center())
        self.assertEqual(combo.currentIndex(), index.row())

    def test_real_settings_scroll_without_changing_or_applying_configuration(self):
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.pages import SettingsPage
        from mnmparse.config import Config

        cfg = Config()
        page = SettingsPage(_MissingEngine(cfg), cfg, self.settings)
        page.resize(1100, 600)
        page.show()
        self.windows.append(page)
        self.app.processEvents()
        before = page.form_config()
        emitted = []
        page.config_changed.connect(lambda *_: emitted.append(True))
        page.overlay_setting_changed.connect(lambda *_: emitted.append(True))
        controls = page.findChildren(QSpinBox) + page.findChildren(QDoubleSpinBox) + page.findChildren(QComboBox) + page.findChildren(QSlider)
        self.assertGreater(len(controls), 20)
        for control in controls:
            with self.subTest(control=control.objectName() or type(control).__name__):
                page._scroll.verticalScrollBar().setValue(0)
                control.setFocus()
                self.wheel(control)
                self.assertEqual(page.form_config(), before)
                self.assertGreater(page._scroll.verticalScrollBar().value(), 0)
        self.assertEqual(emitted, [])

    def test_independent_trigger_setup_profile_crop_and_respawn_forms_are_protected(self):
        from mnmparse.app.crop_picker import CropPicker
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.profile_tools import ProfileTools
        from mnmparse.app.respawn_timer_dialog import RespawnTimerDialog
        from mnmparse.app.setup_wizard import SetupWizard
        from mnmparse.app.triggers_page import TriggersPage
        from mnmparse.config import Config

        cfg = Config()
        engine = _MissingEngine(cfg)
        triggers = TriggersPage(engine, cfg, self.settings)
        triggers.voice.addItems(["Voice A", "Voice B"])
        triggers.voice.setCurrentIndex(1)
        profile = ProfileTools(cfg, Path(self.tmp.name) / "config.json", self.settings, engine)
        profile.profile_name.addItems(["First profile", "Second profile"])
        profile.profile_name.setCurrentIndex(1)
        crop = CropPicker(engine, cfg)
        setup = SetupWizard(engine, cfg)
        setup.crop_page.backend.setCurrentIndex(1)
        respawn = RespawnTimerDialog("Skeleton", 300)
        self.windows.extend([triggers, profile, crop, setup, respawn])
        cases = [
            (triggers.rate, triggers.rate.value),
            (triggers.voice, triggers.voice.currentIndex),
            (triggers.volume.slider, triggers.volume.value),
            (profile.profile_name.lineEdit(), profile.profile_name.currentText),
            (crop._spins["right"].lineEdit(), crop._spins["right"].value),
            (setup.crop_page.backend, setup.crop_page.backend.currentIndex),
            (respawn.minutes, respawn.minutes.value),
        ]
        for control, get_value in cases:
            with self.subTest(control=type(control).__name__):
                before = get_value()
                control.setFocus()
                self.wheel(control)
                self.assertEqual(get_value(), before)

    def test_guard_is_scoped_to_the_form_and_covers_controls_added_later(self):
        from mnmparse.app.widgets import prevent_wheel_value_changes

        scroll, content, *_ = self.form()
        guard = content._settings_wheel_guard
        prevent_wheel_value_changes(content)
        self.assertIs(content._settings_wheel_guard, guard)
        late = QSpinBox(content)
        late.setValue(50)
        self.wheel(late)
        self.assertEqual(late.value(), 50)
        outside = QSpinBox()
        outside.setValue(50)
        self.windows.append(outside)
        self.wheel(outside)
        self.assertEqual(outside.value(), 49, "The guard must not change unrelated windows")

    def test_forms_share_one_app_owned_guard_without_retaining_closed_roots(self):
        from mnmparse.app.widgets import _SettingsWheelGuard, prevent_wheel_value_changes

        references = []
        guards = []
        for _ in range(100):
            root = QWidget()
            prevent_wheel_value_changes(root)
            references.append(weakref.ref(root))
            guards.append(root._settings_wheel_guard)
            root.close()
            del root
        self.assertTrue(all(reference() is None for reference in references))
        self.assertTrue(all(guard is guards[0] for guard in guards))
        self.assertIs(guards[0].parent(), self.app)
        self.assertEqual(self.app.findChildren(_SettingsWheelGuard), [guards[0]])

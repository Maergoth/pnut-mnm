"""Label editor validation and constant-size, passive vector map markers."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QGraphicsScene, QGraphicsView, QWidget

from mnmparse.app.map_labels import MapLabelDialog, MapLabelItem, label_icon
from mnmparse.map_labels import ICONS, MAX_NAME, MAX_NOTES, MapLabel


def label(**changes):
    return replace(MapLabel("0123456789abcdef0123456789abcdef", "https://example.test/Map.png",
                            .25, .75, "Fictional campsite", "Shelter by the lake.", "camp"), **changes)


class MapLabelWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            if isinstance(widget, MapLabelDialog):
                widget.close()
                widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_all_presets_render_distinct_nonempty_vector_icons(self):
        hashes = []
        for kind, _title in ICONS:
            icon = label_icon(kind)
            self.assertFalse(icon.isNull(), kind)
            image = icon.pixmap(24, 24).toImage()
            self.assertFalse(image.isNull())
            self.assertTrue(any(image.pixelColor(x, y).alpha() for x in range(24) for y in range(24)))
            hashes.append(hashlib.sha256(bytes(image.constBits())).hexdigest())
        self.assertEqual(len(set(hashes)), len(ICONS))
        self.assertFalse(label_icon("unsupported").isNull())

    def test_editor_prepopulates_canonical_values_without_mutating_label(self):
        original = label()
        dialog = MapLabelDialog(label=original)
        self.assertEqual(dialog.name_input.text(), original.name)
        self.assertEqual(dialog.notes_input.toPlainText(), original.notes)
        self.assertEqual(dialog.icon_combo.currentData(), "camp")
        dialog.name_input.setText("  Fictional merchant  ")
        dialog.notes_input.setPlainText("  Open at dawn.\r\nBring coins.  ")
        dialog.icon_combo.setCurrentIndex(dialog.icon_combo.findData("merchant"))
        self.assertEqual(dialog.value(), {"name": "Fictional merchant", "notes": "Open at dawn.\nBring coins.", "icon": "merchant"})
        dialog.reject()
        self.assertEqual(original, label())

    def test_editor_validates_required_name_unicode_bounds_and_notes(self):
        dialog = MapLabelDialog()
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.accept()
        self.assertEqual(accepted, [])
        dialog.name_input.setText("\U0001f409" * MAX_NAME)
        self.assertEqual(len(dialog.name_input.text()), MAX_NAME)
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.name_input.setText("x" * (MAX_NAME + 1))
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.name_input.setText("Fictional pin")
        dialog.notes_input.setPlainText("x" * MAX_NOTES)
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.notes_input.insertPlainText("x")
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.notes_input.setPlainText("Line one\nLine two\tTabbed")
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.notes_input.setPlainText("Hidden\x00control")
        self.assertFalse(dialog.save_button.isEnabled())

    def test_editor_keeps_normal_keyboard_focus_even_with_passive_parent(self):
        parent = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        dialog = MapLabelDialog(parent)
        try:
            self.assertFalse(dialog.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
            dialog.show()
            dialog.name_input.setFocus()
            self.app.processEvents()
            QTest.keyClicks(dialog.name_input, "Fictional pin")
            accepted = []
            dialog.accepted.connect(lambda: accepted.append(True))
            QTest.keyClick(dialog.name_input, Qt.Key.Key_Return)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(accepted, [True])
        finally:
            dialog.deleteLater()
            parent.deleteLater()

    def test_marker_remains_24_pixels_and_does_not_accept_map_drag_buttons(self):
        scene = QGraphicsScene()
        view = QGraphicsView(scene)
        marker = MapLabelItem(label())
        scene.addItem(marker)
        marker.setPos(250, 750)
        try:
            self.assertEqual(marker.acceptedMouseButtons(), Qt.MouseButton.NoButton)
            self.assertEqual(marker.zValue(), 10)
            self.assertEqual(marker.boundingRect().center().x(), 0)
            self.assertEqual(marker.boundingRect().center().y(), 0)
            for zoom in (.1, .5, 1, 3, 20):
                view.resetTransform()
                view.scale(zoom, zoom)
                bounds = marker.deviceTransform(view.viewportTransform()).mapRect(marker.boundingRect())
                self.assertAlmostEqual(bounds.width(), 24)
                self.assertAlmostEqual(bounds.height(), 24)
            marker.set_label(label(name="Fictional treasure", icon="treasure"))
            self.assertEqual(marker.label.name, "Fictional treasure")
        finally:
            scene.removeItem(marker)
            marker.deleteLater()
            view.deleteLater()
            scene.deleteLater()

    def test_hover_tooltip_escapes_markup_and_wraps_long_text_without_losing_notes(self):
        marker = MapLabelItem(label(name="<b>Fictional pin</b>", notes='<img src="https://example.test/x">\n' + "W" * 450))
        try:
            tooltip = marker.toolTip()
            self.assertNotIn("<img", tooltip)
            self.assertNotIn("<b>Fictional", tooltip)
            document = QTextDocument()
            document.setHtml(tooltip)
            document.setTextWidth(340)
            self.assertLessEqual(document.idealWidth(), 360)
            text = document.toPlainText().replace("\u200b", "")
            self.assertIn("<b>Fictional pin</b>", text)
            self.assertIn('<img src="https://example.test/x">', text)
            self.assertIn("W" * 450, text)
        finally:
            marker.deleteLater()


if __name__ == "__main__":
    unittest.main()

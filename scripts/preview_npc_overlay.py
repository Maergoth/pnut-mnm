r"""Interactive offline preview, using temporary settings and synthetic NPC data.

From the repository root (PowerShell):
    & .venv/Scripts/python.exe scripts/preview_npc_overlay.py

For a bounded headless smoke check:
    & .venv/Scripts/python.exe scripts/preview_npc_overlay.py --smoke

NPC levels, drop relationships, quests, and reputation here are invented samples.
Only the Staff of Sigils statistics are transcribed from the supplied screenshot.
No game capture, app bootstrap, network requests, or production settings are used.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from mnmparse.app import theme
from mnmparse.app.map_overlay import MapOverlay
from mnmparse.app.overlay import OverlayWindow
from mnmparse.config import Config
from mnmparse.npc_wiki import ItemDetails, NpcDetails


SAMPLE_NAMES = ("Sample NPC · preview data", "Second sample NPC · preview data")
SAMPLES = {
    SAMPLE_NAMES[0]: NpcDetails(
        name=SAMPLE_NAMES[0], level="23–25 (sample)",
        common_drops=("Sample bronze fragment", "Sample cloth scrap"),
        unique_drops=("Staff of Sigils",),
        quests=("Sample quest: The Keeper's Request",),
        reputation=("Sample Watch: +10", "Sample Outcasts: −5"),
    ),
    SAMPLE_NAMES[1]: NpcDetails(
        name=SAMPLE_NAMES[1], level="12 (sample)",
        common_drops=("Sample cracked gem",),
        unique_drops=("Sample unavailable item",),
        quests=("Sample quest: Harbor Supplies",),
        reputation=("Sample Harbor Guard: +5", "Sample Raiders: −10"),
    ),
}
STAFF = ItemDetails(name="Staff of Sigils", lines=(
    "MAGIC", "Slot: PRIMARY SECONDARY", "Weapon DMG: 15   ATK Delay: 33   Skill: BLG",
    "INT: +6 CHA: +2", "Effect:",
    "Runeskin II (On Click. Any Slot. Cast Time: 10s. Level: 30)",
    "Weight: 7.5   Size: MEDIUM", "Class: ENC", "Race: ALL",
))
PREVIEW_STATUS = "Preview data · NPC information and drop relationships are synthetic"


class _NoNetworkMaps:
    def maps(self, *_args, **_kwargs):
        raise AssertionError("The offline preview must not start map workers")

    def image(self, *_args, **_kwargs):
        raise AssertionError("The offline preview must not load map images")


class _PreviewMap(MapOverlay):
    def load(self, **_kwargs) -> None:
        self._loaded_zone = self.current_zone
        self._set_status(
            "Offline map preview\n\nHover over the map header and use the monster icon "
            "to select a sample NPC.\n\nNight Harbor has synthetic preview entries. "
            "No map images or wiki data are downloaded."
        )

    def _update_wiki_link(self) -> None:
        super()._update_wiki_link()
        self.wiki_link.setEnabled(False)
        self.wiki_link.setToolTip("Wiki links are disabled in this offline preview")


class PreviewWindow(QWidget):
    """Controls plus the real overlay and map, isolated from the installed app."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NPC overlay preview · synthetic data")
        self._temporary = tempfile.TemporaryDirectory(prefix="pnut-npc-preview-")
        self.settings = QSettings(str(Path(self._temporary.name) / "preview.ini"),
                                  QSettings.Format.IniFormat)
        self.overlay = OverlayWindow(self.settings, Config(attack_bar=False))
        self.overlay.setWindowTitle("NPC preview · combat overlay")
        self.map = _PreviewMap(self.settings, repository=_NoNetworkMaps())
        self.map.setWindowTitle("NPC preview · map picker")
        self.map.npc_selected.connect(self.select_npc)
        self.map.npc_zone_requested.connect(self.populate_notables)
        self.overlay.npc_panel.item_hovered.connect(self.show_item)
        self.overlay.npc_panel.set_item_details(STAFF)
        self._disposed = False

        layout = QVBoxLayout(self)
        introduction = QLabel(
            "Offline preview only. NPCs, levels, loot associations, quests, and reputation "
            "are invented samples. Staff statistics match the supplied screenshot. "
            "Your installed app and saved preferences are untouched."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)

        selection = QHBoxLayout()
        self.selection = QComboBox()
        self.selection.addItems(SAMPLE_NAMES)
        selection.addWidget(self.selection, 1)
        consider = QPushButton("Consider sample")
        consider.clicked.connect(lambda: self.select_npc(self.selection.currentText()))
        selection.addWidget(consider)
        layout.addLayout(selection)

        fields = QGridLayout()
        self.fields = {}
        for index, (key, label) in enumerate((
            ("enabled", "Show NPC details"), ("level", "Level / level range"),
            ("common_drops", "Common drops"), ("unique_drops", "Unique drops"),
            ("quests", "Required by quests"), ("reputation", "Reputation gained / lost"),
        )):
            control = QCheckBox(label)
            control.setChecked(self.overlay.npc_panel.options[key])
            control.toggled.connect(lambda checked, key=key: self.overlay.npc_panel.configure(**{key: checked}))
            self.fields[key] = control
            fields.addWidget(control, index // 2, index % 2)
        layout.addLayout(fields)
        self.overlay.npc_panel_changed.connect(self.fields["enabled"].setChecked)

        switches = QHBoxLayout()
        locked = QCheckBox("Locked")
        locked.setChecked(self.overlay.locked)
        locked.toggled.connect(self.overlay.set_locked)
        switches.addWidget(locked)
        click_through = QCheckBox("Ignore the mouse")
        click_through.toggled.connect(self.overlay.set_click_through)
        switches.addWidget(click_through)
        self.offline = QCheckBox("Wiki unavailable")
        self.offline.toggled.connect(lambda _checked: self.select_npc(self.overlay.npc_panel._name))
        switches.addWidget(self.offline)
        layout.addLayout(switches)

        font_row = QHBoxLayout()
        font_row.addWidget(QLabel("Font scale"))
        font = QSlider(Qt.Orientation.Horizontal)
        font.setRange(60, 200)
        font.setValue(100)
        font.setAccessibleName("Preview font scale")
        self.font_value = QLabel("100%")
        font.valueChanged.connect(self.set_font_scale)
        font_row.addWidget(font, 1)
        font_row.addWidget(self.font_value)
        layout.addLayout(font_row)

        visibility = QHBoxLayout()
        self.show_overlay = QCheckBox("Overlay visible")
        self.show_overlay.setChecked(True)
        self.show_overlay.toggled.connect(self.overlay.setVisible)
        visibility.addWidget(self.show_overlay)
        self.show_map = QCheckBox("Map visible")
        self.show_map.setChecked(True)
        self.show_map.toggled.connect(self.map.setVisible)
        visibility.addWidget(self.show_map)
        dock = QPushButton("Dock NPC panel")
        dock.clicked.connect(self.overlay.npc_panel.dock)
        visibility.addWidget(dock)
        layout.addLayout(visibility)
        help_text = QLabel(
            "Hover over Staff of Sigils for its tooltip. Unlock to drag the NPC panel off; "
            "double-click it to dock again. Ignore the mouse disables item hovers. "
            "Close this control window to quit the entire preview."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.resize(480, self.sizeHint().height())
        self.overlay.resize(500, 300)
        self.map.resize(520, 380)
        self._position_windows()
        self.map.set_zone("Night Harbor")
        self.populate_notables("Night Harbor")
        self.select_npc(SAMPLE_NAMES[0])

    def _position_windows(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.left() + 20, area.top() + 20)
        self.overlay.move(area.left() + min(530, max(20, area.width() - 520)), area.top() + 20)
        self.map.move(area.left() + 20, area.top() + min(self.height() + 40, max(20, area.height() - 400)))

    def populate_notables(self, zone: str) -> None:
        self.map.set_notable_npcs(
            zone, SAMPLE_NAMES if zone == "Night Harbor" else (),
            "Synthetic preview entries" if zone == "Night Harbor" else "No sample entries for this zone · preview only",
        )

    def select_npc(self, name: str) -> None:
        details = SAMPLES.get(name)
        if details is None:
            return
        panel = self.overlay.npc_panel
        panel.show_npc(name)
        panel.set_details(details, "Wiki unavailable · cached preview data" if self.offline.isChecked() else PREVIEW_STATUS)
        self.selection.setCurrentText(name)

    def show_item(self, name: str) -> None:
        panel = self.overlay.npc_panel
        if name == STAFF.name:
            panel.set_item_details(STAFF)
            panel.set_item_status(name, "Stats from the supplied screenshot · sample drop relationship")
        else:
            panel.set_item_status(name, "Item statistics unavailable · this is a synthetic preview item")

    def set_font_scale(self, value: int) -> None:
        scale = value / 100
        self.overlay.set_font_scale(scale)
        self.map.set_appearance(self.overlay.opacity, scale)
        self.font_value.setText(f"{value}%")

    def show_preview(self) -> None:
        self.show()
        self.overlay.setVisible(self.show_overlay.isChecked())
        self.map.setVisible(self.show_map.isChecked())

    def closeEvent(self, event) -> None:  # noqa: N802
        self.overlay.close()
        self.map.shutdown()
        super().closeEvent(event)

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self.overlay.close()
        self.map.shutdown()
        self.overlay._geometry_timer.stop()
        self.settings.sync()
        self._temporary.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="Build the preview offscreen and quit after 300 ms")
    args = parser.parse_args(argv)
    if args.smoke:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([sys.argv[0]])
    theme.apply_theme(app)
    preview = PreviewWindow()
    try:
        preview.show_preview()
        if args.smoke:
            QTimer.singleShot(300, app.quit)
        return app.exec()
    finally:
        preview.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline guidance and local support information."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from mnmparse import __version__


class HelpPanel(QFrame):
    setup_requested = Signal()
    demo_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel("Quick start and help")
        title.setObjectName("Heading")
        layout.addWidget(title)
        body = QLabel(
            "1. Open Monsters & Memories and keep its Combat chat visible.\n"
            "2. Run setup, enter your character name, select the chat crop and test OCR.\n"
            "3. Start capture. An empty meter is normal until a fight occurs.\n"
            "If capture waits for the game, check the window title. If lines are missing, enlarge "
            "the chat and crop, then retest OCR. Scrolled or covered chat cannot be read reliably.\n\n"
            "Carebear Mode starts on. It shows your information and rounded group averages for "
            "groups of at least three. Damage and healing averages each need three active contributors in that role. "
            "Use the flower button to open Morality Adjustment. "
            "Drag the red lever to request Elitist mode, then confirm the pledge.\n"
            "Session keeps previous runs locally. Stop capture before resuming one. "
            "Settings provides profile backup and restore.\n\n"
            "Shortcuts while PNUT is focused: F5 Start/Stop · Ctrl+1–6 pages · "
            "Ctrl+M mute · Ctrl+O overlay · Ctrl+Shift+M map · Ctrl+Q quit. "
            "The tray also provides capture, overlay and mute controls."
        )
        body.setWordWrap(True)
        layout.addWidget(body)
        buttons = QHBoxLayout()
        for caption, slot in (("Run setup…", self.setup_requested.emit),
                              ("Try synthetic demo", self.demo_requested.emit),
                              ("Copy version and build", self.copy_version)):
            button = QPushButton(caption)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)

    def copy_version(self):
        from mnmparse.provenance import build_info
        info = build_info()
        commit = str(info.get("commit", "source checkout"))[:12]
        QApplication.clipboard().setText(f"PNUT {__version__} · build {commit}")

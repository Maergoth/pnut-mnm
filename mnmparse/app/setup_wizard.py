"""Guided calibration using a staged configuration and explicit capture actions."""
from __future__ import annotations

from dataclasses import replace
import time
from typing import Any

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWizard, QWizardPage, QWidget,
)

from mnmparse.app import theme
from mnmparse.app.crop_picker import CropPicker, button_qss
from mnmparse.app.morality import MoralityPanel
from mnmparse.app.widgets import ToggleSwitch
from mnmparse.app.window_identity import window_title
from mnmparse.config import Config
from mnmparse.privacy import casual_enabled


def _text(message: str) -> QLabel:
    label = QLabel(message)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _body(page: QWizardPage) -> QVBoxLayout:
    """Keep page controls reachable when DPI leaves little logical screen space."""
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea(page)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setObjectName("SetupScroll")
    body = QWidget()
    body.setObjectName("SetupBody")
    layout = QVBoxLayout(body)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    scroll.setWidget(body)
    outer.addWidget(scroll)
    page.body_scroll = scroll
    return layout


def _setup_qss() -> str:
    # QWizard's native Aero style paints white surfaces independently of the
    # application stylesheet. ModernStyle plus explicit palette/backgrounds
    # keep header, footer and content consistently readable on Windows.
    return f"""
    QWizard#PnutSetup, QWizard#PnutSetup QWidget {{
        background: {theme.BG0}; color: {theme.TEXT};
    }}
    QWizard#PnutSetup QLabel {{ background: transparent; color: {theme.TEXT}; }}
    QWizard#PnutSetup QLineEdit, QWizard#PnutSetup QComboBox,
    QWizard#PnutSetup QSpinBox {{
        background: {theme.BG1}; color: {theme.TEXT};
        border: 1px solid {theme.LINE}; border-radius: 6px; padding: 5px;
        selection-background-color: {theme.ACCENT}; selection-color: {theme.BG0};
    }}
    """ + button_qss().replace("QPushButton#", "QWizard#PnutSetup QPushButton#")


class _CalibrationEngine:
    """Preview a chosen source without mutating or starting the live engine."""

    def __init__(self, engine: Any, cfg: Config):
        self.engine, self.cfg = engine, cfg

    def grab_frame(self):
        from mnmparse.capture import make_source
        source = make_source(replace(self.cfg))
        try:
            source.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                frame = source.latest()
                if frame is not None:
                    return frame
                time.sleep(.04)
            raise RuntimeError("No frame arrived. Keep the game open, check its title, or try Desktop capture.")
        finally:
            source.stop()

    def test_ocr(self, frame, crop, cfg):
        return self.engine.test_ocr(frame, crop, cfg)


class _CharacterPage(QWizardPage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard)
        self.owner = wizard
        self.setTitle("1 · Character and mode")
        self.setSubTitle("Enter your character name and choose what PNUT shows.")
        layout = _body(self)
        self.name = QLineEdit(wizard._draft.player_name)
        self.name.setMaxLength(64)
        self.name.setPlaceholderText("Your character name")
        self.name.setAccessibleName("Your character name")
        form = QFormLayout()
        form.addRow("Character", self.name)
        layout.addLayout(form)
        self.morality = MoralityPanel(wizard._draft, self)
        self.morality.mode_requested.connect(wizard._stage_mode)
        self.morality.siren_requested.connect(wizard.siren_requested.emit)
        layout.addWidget(self.morality)
        layout.addStretch()
        self.name.textChanged.connect(self.completeChanged)

    def initializePage(self):  # noqa: N802
        self.name.setFocus(Qt.FocusReason.OtherFocusReason)

    def isComplete(self):  # noqa: N802
        return bool(self.name.text().strip())

    def validatePage(self):  # noqa: N802
        self.owner._draft.player_name = self.name.text().strip()
        self.owner.crop_picker.set_config(self.owner._draft)
        return self.isComplete()


class _CropPage(QWizardPage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard)
        self.owner = wizard
        self.setTitle("2 · Combat chat crop")
        self.setSubTitle("Open Combat chat, capture a frame, and place the rectangle over its text.")
        layout = _body(self)
        fields = QHBoxLayout()
        self.title = QLineEdit(wizard._draft.window_title)
        self.title.setAccessibleName("Game window title")
        self.title.setMaxLength(128)
        fields.addWidget(QLabel("Game title"))
        fields.addWidget(self.title, 1)
        self.backend = QComboBox()
        self.backend.addItem("Window capture", "wgc")
        self.backend.addItem("Desktop capture", "mss")
        self.backend.setCurrentIndex(max(0, self.backend.findData(wizard._draft.capture_backend)))
        self.backend.setAccessibleName("Capture backend")
        fields.addWidget(self.backend)
        self.detect = QPushButton("Find game")
        self.detect.clicked.connect(self._detect)
        fields.addWidget(self.detect)
        layout.addLayout(fields)
        self.status = _text("")
        layout.addWidget(self.status)
        self.privacy_hint = _text("Setup preview shows chat. It is cleared when setup closes.")
        layout.addWidget(self.privacy_hint)
        layout.addWidget(wizard.crop_picker, 1)
        self.ocr_status = _text("Test OCR to continue.")
        self.ocr_status.setAccessibleName("Setup OCR validation result")
        layout.addWidget(self.ocr_status)
        self.title.textChanged.connect(self._source_changed)
        self.backend.currentIndexChanged.connect(self._source_changed)

    def _source_changed(self, *_args):
        owner = self.owner
        owner._draft.window_title = self.title.text().strip()
        owner._draft.capture_backend = str(self.backend.currentData())
        owner._frame_size = None
        owner._invalidate_ocr()
        owner.crop_picker.set_frame(None)
        owner.crop_picker.set_config(owner._draft)
        self.completeChanged.emit()

    def _detect(self):
        from mnmparse.capture import find_game_window_info
        try:
            window = find_game_window_info(self.title.text().strip())
        except Exception:  # Native unavailable/error belongs to this calibration step.
            self.status.setText("Game detection unavailable. Check the title or try Capture frame.")
            return
        self.status.setText("Game found. Capture a frame." if window is not None
                            else "Game not found. Open it and check the title.")

    def isComplete(self):  # noqa: N802
        return bool(self.title.text().strip() and self.owner._frame_size is not None
                    and self.owner._has_ocr_proof())

    def validatePage(self):  # noqa: N802
        if not self.isComplete():
            return False
        left, top, right, bottom = self.owner.crop_picker.crop()
        width, height = self.owner._frame_size
        valid = 0 <= left < right <= width and 0 <= top < bottom <= height
        if not valid:
            self.status.setText("Keep the entire Combat chat rectangle inside the captured frame.")
            return False
        self.owner._draft.crop = (left, top, right, bottom)
        return True


class _OptionsPage(QWizardPage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard)
        self.owner = wizard
        self.setTitle("3 · Common options")
        layout = _body(self)
        form = QFormLayout()
        form.setVerticalSpacing(18)
        for key, caption, selected in (
            ("revenge_enabled", "Revenge List", wizard._draft.revenge_enabled),
            ("display_map", "Display map", wizard._display_map_default),
            ("attack_bar", "Auto attack bar", wizard._draft.attack_bar),
            ("export_auto", "Copy to clipboard after fight", wizard._draft.export_auto),
        ):
            switch = ToggleSwitch()
            switch.setAccessibleName(caption)
            switch.setChecked(bool(selected))
            setattr(self, key, switch)
            form.addRow(caption, switch)
            switch.toggled.connect(self._stage_options)
        self.revenge_enabled.setToolTip("PvP only. Names are hidden in Carebear Mode.")
        self.attack_bar.setToolTip("Shown below the overlay.")
        self.export_auto.setToolTip("Copies finished fights involving your group.")
        layout.addLayout(form)
        layout.addStretch()

    def _stage_options(self, *_args):
        for key in ("revenge_enabled", "attack_bar", "export_auto"):
            if hasattr(self, key):
                setattr(self.owner._draft, key, getattr(self, key).isChecked())

    def validatePage(self):  # noqa: N802
        self._stage_options()
        return True


class _ReadyPage(QWizardPage):
    def __init__(self, wizard: "SetupWizard"):
        super().__init__(wizard)
        self.owner = wizard
        self.setTitle("4 · Ready to capture")
        layout = _body(self)
        self.summary = _text("")
        layout.addWidget(self.summary)
        self.next_step = _text("")
        layout.addWidget(self.next_step)
        layout.addStretch()

    def initializePage(self):  # noqa: N802
        cfg = self.owner._draft
        self.summary.setText(f"Character: {cfg.player_name}\nCombat crop: {cfg.crop}\n"
                             f"OCR: {self.owner._ocr_count} readable lines\n"
                             f"Mode: {'Carebear Mode' if casual_enabled(cfg) else 'Elitist Scumbag Mode'}")
        self.next_step.setText("Save setup to start capture." if cfg.start_capture_on_launch
                               else "Save setup, then choose Start capture.")


class SetupWizard(QWizard):
    """Pass ``engine, cfg``; persist/apply only ``finished_config`` after Finish.

    Cancel leaves the original Config untouched and setup_complete false. No
    capture/OCR happens at construction or on page navigation; buttons initiate it.
    """

    finished_config = Signal(object)
    siren_requested = Signal()

    def __init__(self, engine: Any, cfg: Config, parent: QWidget | None = None, *, display_map: bool = True):
        super().__init__(parent)
        self._draft = replace(cfg)
        self._display_map_default = bool(display_map)
        self._frame_size: tuple[int, int] | None = None
        self._ocr_count: int | None = None
        self._tested_signature = None
        self._pending_test_signature = None
        self._pending_capture_signature = None
        self._cancel_pending = False
        self.setObjectName("PnutSetup")
        self.setAccessibleName("PNUT setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setTitleFormat(Qt.TextFormat.PlainText)
        self.setSubTitleFormat(Qt.TextFormat.PlainText)
        self.setPalette(theme._build_palette())
        self.setAutoFillBackground(True)
        self.setStyleSheet(_setup_qss())
        self._adapter = _CalibrationEngine(engine, self._draft)
        self.crop_picker = CropPicker(self._adapter, self._draft, self, calibration_preview=True)
        self.setWindowTitle(window_title("setup"))
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        width = min(1000, max(1, int(available.width() * .94))) if available is not None else 1000
        height = min(680, max(1, int(available.height() * .88))) if available is not None else 680
        self.setMinimumSize(min(800, width), min(540, height))
        self.resize(width, height)
        self.setOption(QWizard.WizardOption.IndependentPages, True)
        self.setButtonText(QWizard.WizardButton.CancelButton, "Set up later")
        self.setButtonText(QWizard.WizardButton.FinishButton, "Save setup")
        self.setButtonText(QWizard.WizardButton.NextButton, "Continue")
        self.setButtonText(QWizard.WizardButton.BackButton, "Back")
        for role in (QWizard.WizardButton.NextButton, QWizard.WizardButton.FinishButton):
            self.button(role).setObjectName("Primary")
        for role in (QWizard.WizardButton.BackButton, QWizard.WizardButton.CancelButton):
            self.button(role).setObjectName("Chip")
        self.character_page = _CharacterPage(self)
        self.crop_page = _CropPage(self)
        self.options_page = _OptionsPage(self)
        self.ready_page = _ReadyPage(self)
        for page in (self.character_page, self.crop_page, self.options_page, self.ready_page):
            page.setTitle("PNUT setup · " + QWizardPage.title(page))
            page.setPalette(self.palette())
            page.setAutoFillBackground(True)
            self.addPage(page)
        self.crop_picker.crop_changed.connect(self._crop_changed)
        self.crop_picker._capture.clicked.connect(self._begin_capture)
        self.crop_picker._test.clicked.connect(self._begin_ocr)
        self.crop_picker.frame_captured.connect(self._frame_captured)
        self.crop_picker.ocr_test_completed.connect(self._ocr_completed)
        # Button roles and page hierarchy now exist; repolish the role-specific
        # selectors after Qt created its own header/footer widgets.
        self.setStyleSheet(_setup_qss())

    def _signature(self):
        cfg = self._draft
        return (cfg.window_title, cfg.capture_backend, self.crop_picker.crop(),
                cfg.ocr_engine, cfg.ocr_scale, cfg.preprocess, cfg.player_name)

    @property
    def display_map(self) -> bool:
        """A staged UI preference; the caller applies it only after saving config."""
        return self.options_page.display_map.isChecked()

    def _has_ocr_proof(self) -> bool:
        return bool(self._ocr_count and self._tested_signature == self._signature())

    def _stage_mode(self, casual: bool):
        if self._cancel_pending:
            return
        self._draft.casual_mode = bool(casual)
        self._draft.casual_mode_confirmed = not casual
        self.character_page.morality.set_config(self._draft)
        self.crop_picker.set_config(self._draft)

    def _begin_capture(self):
        self._pending_capture_signature = (self._draft.window_title, self._draft.capture_backend)
        self._frame_size = None
        self._invalidate_ocr()
        self.crop_page.completeChanged.emit()

    def _frame_captured(self, size: tuple):
        if self._cancel_pending:
            self._clear_calibration_preview()
            return
        signature = (self._draft.window_title, self._draft.capture_backend)
        if self._pending_capture_signature is not None and self._pending_capture_signature != signature:
            self.crop_picker.set_frame(None)
            return
        self._frame_size = tuple(size) if len(size) == 2 and all(int(v) > 0 for v in size) else None
        self._invalidate_ocr()
        self.crop_page.completeChanged.emit()

    def _crop_changed(self, crop: tuple):
        self._draft.crop = tuple(crop)
        self._invalidate_ocr()

    def _invalidate_ocr(self):
        self._ocr_count = None
        self._tested_signature = None
        if hasattr(self, "crop_page"):
            self._update_validation()

    def _begin_ocr(self):
        self._invalidate_ocr()
        self._pending_test_signature = self._signature()

    def _run_ocr(self):
        self._begin_ocr()
        self.crop_picker.run_test_ocr()

    def _ocr_completed(self, count: int):
        if self._cancel_pending:
            self._clear_calibration_preview()
            return
        if self._pending_test_signature != self._signature():
            self._invalidate_ocr()
            return
        self._ocr_count = max(0, int(count))
        self._tested_signature = self._pending_test_signature
        self._update_validation()

    def _update_validation(self):
        count = self._ocr_count
        self.crop_page.ocr_status.setText(
            f"Found {count} readable lines." if count else
            "No readable lines. Adjust the crop or chat font, then test again." if count == 0 else
            "Test OCR to continue.")
        self.crop_page.completeChanged.emit()

    def accept(self):
        if self.currentId() != self.pageIds()[-1] or not self.crop_page.isComplete():
            return
        self._draft.crop = self.crop_picker.crop()
        self._draft.setup_complete = True
        configured = replace(self._draft)
        self._clear_calibration_preview()
        self.finished_config.emit(configured)
        super().accept()

    def reject(self):
        self._cancel_pending = True
        self._clear_calibration_preview()
        # A short-lived calibration worker owns a Qt signal. Keep its owner alive
        # until it returns, while closing the user-facing setup immediately.
        if self.crop_picker._job is not None:
            self.hide()
            QTimer.singleShot(50, self, self._finish_cancel)
            return
        super().reject()

    def _clear_calibration_preview(self):
        """Revoke the local exception and release pixels/results on every exit."""
        # Cancel delayed/open pledge UI even when the draft has already selected
        # full mode; this temporary policy never changes the staged/live config.
        self.character_page.morality.set_config(replace(self._draft, casual_mode=True, casual_mode_confirmed=False))
        self.crop_picker.set_calibration_preview(False)
        self.crop_picker.set_frame(None)
        self.crop_picker.canvas.set_boxes([])
        self._frame_size = None
        self._pending_capture_signature = None
        self._pending_test_signature = None

    def _finish_cancel(self):
        if self.crop_picker._job is not None:
            QTimer.singleShot(50, self, self._finish_cancel)
        else:
            self._clear_calibration_preview()
            super().reject()

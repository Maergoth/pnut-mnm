"""The deliberately theatrical, gated Morality Adjustment control."""
from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import Property, QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFocusEvent, QKeyEvent, QMouseEvent, QPainter, QPen, QPolygonF
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QAbstractButton, QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from mnmparse.app import theme
from mnmparse.privacy import PROMISE, casual_enabled


def reduced_motion(cfg: Any) -> bool:
    if bool(getattr(cfg, "reduced_motion", False)):
        return True
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop\WindowMetrics") as key:
                return str(winreg.QueryValueEx(key, "MinAnimate")[0]) == "0"
        except OSError:
            pass
    return False


class RedModeSwitch(QAbstractButton):
    toggle_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(True)
        self.setAccessibleName("Carebear Mode")
        self.setAccessibleDescription("Drag the red handle right, or press Shift+Right, to request Elitist Scumbag Mode. "
                                      "The teammate promise is required. Click or press Space to return to Carebear Mode.")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(280, 112)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._position = 0.0
        self._dragging = False
        self._drag_start_x = 0.0
        self.motion_enabled = True
        self.animation = QPropertyAnimation(self, b"position", self)
        self.animation.setDuration(260)
        self.clicked.connect(self._clicked)

    def nextCheckState(self) -> None:  # noqa: N802
        # The confirmed presentation policy owns the state. Button clicks must
        # never toggle it before the deliberate gesture and pledge succeed.
        pass

    def _clicked(self) -> None:
        if not self.isChecked():
            self.toggle_requested.emit()

    def _knob_rect(self) -> QRectF:
        body = QRectF(4, 5, self.width() - 8, self.height() - 15)
        x = body.left() + 9 + (body.width() - 98) * self._position
        return QRectF(x, body.top() + 8, 80, 80)

    def _drag_position(self, x: float) -> float:
        return min(1.0, max(0.0, (x - self._drag_start_x) / max(1, self.width() - 106)))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self.isChecked():
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton and self._knob_rect().contains(event.position()):
            self.animation.stop()
            self._dragging = True
            self._drag_start_x = event.position().x()
            self.setDown(True)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._dragging:
            self.position = self._drag_position(event.position().x())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self._dragging:
            if not self.isChecked():
                super().mouseReleaseEvent(event)
            else:
                event.ignore()
            return
        self._dragging = False
        self.setDown(False)
        self.position = self._drag_position(event.position().x())
        # Starting on the handle and moving most of its travel is deliberate;
        # a plain click, click on the far track, or short drag stays Carebear.
        if event.button() == Qt.MouseButton.LeftButton and self.position >= .75:
            self.toggle_requested.emit()
        else:
            self.move_to(True, animate=self.motion_enabled)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            event.accept()
            return
        if self.isChecked():
            if event.key() == Qt.Key.Key_Right and event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                self._dragging = False
                self.setDown(False)
                self.toggle_requested.emit()
            elif event.key() == Qt.Key.Key_Escape and self._dragging:
                self._dragging = False
                self.setDown(False)
                self.move_to(True, animate=self.motion_enabled)
            event.accept()
        elif event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Left):
            self.toggle_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # QAbstractButton's Space-release activation cannot bypass the gesture.
        self.setDown(False)
        event.accept()

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        if self._dragging:
            self._dragging = False
            self.setDown(False)
            self.move_to(True, animate=self.motion_enabled)
        super().focusOutEvent(event)

    def _get_position(self) -> float:
        return self._position

    def _set_position(self, value: float) -> None:
        self._position = value
        self.update()

    position = Property(float, _get_position, _set_position)

    def move_to(self, casual: bool, *, animate: bool = True) -> None:
        self.animation.stop()
        self._dragging = False
        self.setDown(False)
        target = 0.0 if casual else 1.0
        if animate:
            self.animation.setStartValue(self._position)
            self.animation.setEndValue(target)
            self.animation.start()
        else:
            self.position = target

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(4, 5, self.width() - 8, self.height() - 15)
        p.setPen(QPen(QColor("#781925"), 3))
        p.setBrush(QColor("#df3445"))
        p.drawRoundedRect(body, 15, 15)
        channel = body.adjusted(28, 23, -28, -23)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#10141c"))
        p.drawRoundedRect(channel, 24, 24)
        knob = self._knob_rect()
        p.setBrush(QColor("#61131e"))
        p.drawRoundedRect(knob.translated(0, 5), 13, 13)
        p.setPen(QPen(QColor("#ff9586"), 3))
        p.setBrush(QColor("#ce2638"))
        p.drawRoundedRect(knob, 13, 13)
        if self._position >= .5:
            p.setFont(theme.app_font(34))
            p.drawText(knob, Qt.AlignmentFlag.AlignCenter, "😢")
        else:
            p.setPen(QPen(QColor("#ffe3de"), 3))
            centre = knob.center()
            p.drawArc(QRectF(centre.x() - 13, centre.y() - 13, 26, 26), 45 * 16, 270 * 16)
            p.drawLine(QPointF(centre.x(), centre.y() - 19), QPointF(centre.x(), centre.y() - 1))
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.ACCENT), 2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(body.adjusted(-2, -2, 2, 2), 17, 17)


class MoralityPanel(QWidget):
    mode_requested = Signal(bool)
    siren_requested = Signal()

    def __init__(self, cfg: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._casual = casual_enabled(cfg)
        self._pending = False
        self._request_generation = 0
        self._dialog: QDialog | None = None
        self.setObjectName("MoralityAdjustment")
        self.setAccessibleName("Morality Adjustment")
        box = QVBoxLayout(self)
        box.setContentsMargins(22, 18, 22, 18)
        heading = QLabel("Morality Adjustment", self)
        heading.setObjectName("Heading")
        box.addWidget(heading)
        self.description = QLabel("", self)
        self.description.setWordWrap(True)
        box.addWidget(self.description)
        labels = QHBoxLayout()
        labels.addWidget(QLabel("🌼 Carebear Mode 🌼", self))
        labels.addStretch(1)
        right = QLabel("Elitist Scumbag Mode", self)
        right.setWordWrap(True)
        labels.addWidget(right)
        box.addLayout(labels)
        self.interaction_hint = QLabel("Slide right to switch modes. Click to return to Carebear.", self)
        self.interaction_hint.setWordWrap(True)
        box.addWidget(self.interaction_hint)
        self.switch = RedModeSwitch(self)
        box.addWidget(self.switch)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        self.switch.toggle_requested.connect(self.request_toggle)
        self.set_config(cfg)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(theme.BG1))
        p.setPen(QPen(QColor(theme.LINE), 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)
        p.setClipRect(self.rect().adjusted(3, 3, -3, -3))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(237, 190, 66, 20))
        h = self.height()
        for x in range(-h, self.width() + h, 40):
            p.drawPolygon(QPolygonF([QPointF(x, h), QPointF(x + 18, h), QPointF(x + h + 18, 0), QPointF(x + h, 0)]))

    def set_config(self, cfg: Any) -> None:
        self._request_generation += 1
        self._pending = False
        self._cfg = cfg
        self._casual = casual_enabled(cfg)
        self.switch.setEnabled(True)
        self.switch.motion_enabled = not reduced_motion(cfg)
        self.switch.setChecked(self._casual)
        self.switch.setAccessibleName("Carebear Mode" if self._casual else "Elitist Scumbag Mode")
        self.switch.move_to(self._casual, animate=False)
        self.description.setText("Your information and rounded group averages. Other individuals stay hidden."
                                 if self._casual else "Individual information is visible.")
        self.description.setToolTip(
            "Damage/DPS needs three active members whose damage is at least their healing. "
            "Healing/HPS needs three active members whose healing is at least their damage. "
            "Pets count with their owners. Positive ties count in both; unavailable averages show —."
            if self._casual else "")
        self.status.setText("Carebear Mode is active." if self._casual else "Elitist Scumbag Mode is active.")
        if self._casual and self._dialog is not None:
            self._dialog.reject()

    def request_toggle(self) -> None:
        self.switch.setChecked(self._casual)
        if self._pending:
            return
        if not self._casual:
            self.mode_requested.emit(True)
            return
        self._pending = True
        self._request_generation += 1
        generation = self._request_generation
        self.switch.setEnabled(False)
        self.status.setText("Carebear Mode stays active until you confirm.")
        self.switch.move_to(False, animate=not reduced_motion(self._cfg))
        self.siren_requested.emit()
        QTimer.singleShot(560 if not reduced_motion(self._cfg) else 0, self,
                          lambda: self._confirm() if generation == self._request_generation else None)

    def _confirm(self) -> None:
        if not self._pending:
            return
        dialog = QDialog(self)
        self._dialog = dialog
        dialog.setWindowTitle("Switch to Elitist Scumbag Mode?")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        prompt = QLabel("Confirm you " + PROMISE, dialog)
        prompt.setWordWrap(True)
        prompt.setMinimumWidth(320)
        layout.addWidget(prompt)
        pledge = QCheckBox("I make this promise.", dialog)
        pledge.setObjectName("moralityPledge")
        layout.addWidget(pledge)
        buttons = QHBoxLayout()
        keep = QPushButton("Keep Carebear Mode", dialog)
        keep.setDefault(True)
        keep.clicked.connect(dialog.reject)
        agree = QPushButton("I promise — switch modes", dialog)
        agree.setObjectName("moralityAgree")
        agree.setEnabled(False)
        pledge.toggled.connect(agree.setEnabled)
        agree.clicked.connect(dialog.accept)
        buttons.addWidget(keep)
        buttons.addWidget(agree)
        layout.addLayout(buttons)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted and pledge.isChecked()
        self._dialog = None
        dialog.deleteLater()
        self._pending = False
        self.switch.setEnabled(True)
        if accepted:
            self.mode_requested.emit(False)
        self.switch.setChecked(self._casual)
        self.switch.move_to(self._casual, animate=not reduced_motion(self._cfg))
        if self._casual:
            self.status.setText("Carebear Mode is active.")

    def focus_switch(self) -> None:
        self.switch.setFocus(Qt.FocusReason.ShortcutFocusReason)

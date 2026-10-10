"""Settings widget for capture profiles and one validated preference backup."""
from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
import tempfile
from typing import Callable

from PySide6.QtCore import QRect, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from mnmparse.backup import BackupError, backup_profile, restore_profile
from mnmparse.config import Config, load_config, project_path, save_config
from mnmparse.profiles import apply_profile, capture_profile, validate_profiles
from mnmparse.storage import atomic_json

# No actor names, raw trigger content, history, map/view or respawn custom labels.
UI_KEYS = {
    "main/page", "main/maximized", "main/overlay_visible", "main/rect",
    "map/visible", "map/fullscreen", "map/download_on_startup", "map/geometry",
    "app/update_on_startup", "overlay/locked", "overlay/click_through", "overlay/view_mode",
    "overlay/opacity", "overlay/font_scale", "overlay/tab", "overlay/sort_key", "overlay/sort_desc",
    "overlay/geometry", "attack_bar/enabled", "live/list_split",
}
RECT_KEYS = {"main/rect", "map/geometry", "overlay/geometry"}


def _primitive(value):
    if value is None or isinstance(value, (str, bool)):
        return len(value) <= 16384 if isinstance(value, str) else True
    if type(value) in (int, float):
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    return isinstance(value, list) and len(value) <= 256 and all(type(v) in (bool, int, float, str) and _primitive(v) for v in value)


def settings_payload(settings) -> dict:
    values = {}
    for key in UI_KEYS:
        if not settings.contains(key):
            continue
        value = settings.value(key)
        if key in RECT_KEYS and isinstance(value, QRect):
            value = [value.x(), value.y(), value.width(), value.height()]
        if _primitive(value):
            values[key] = value
    return {"schema": 1, "values": values}


def apply_settings(settings, payload: dict) -> None:
    for key, value in payload["values"].items():
        if key not in UI_KEYS or not _primitive(value):
            continue
        if key in RECT_KEYS:
            if not isinstance(value, list) or len(value) != 4 or any(type(n) is not int for n in value) or value[2] <= 0 or value[3] <= 0:
                continue
            value = QRect(*value)
        settings.setValue(key, value)
    settings.sync()


class ProfileTools(QWidget):
    config_changed = Signal(object)
    restored = Signal(object)

    def __init__(self, cfg: Config, config_path: str | Path, settings, engine, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg, self._path, self._settings, self._engine = cfg, Path(config_path), settings, engine
        self._getter: Callable[[], Config] = lambda: self._cfg
        self._trigger_flush: Callable[[], object] | None = None
        self._revenge_flush: Callable[[], object] | None = None
        self._dimensions_getter: Callable[[], tuple[int, int] | None] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        profiles = QGroupBox("Capture profiles", self)
        profiles_layout = QVBoxLayout(profiles)
        detail = QLabel("Save the window, character, chat crop, OCR settings, frame rate and log folder together.", profiles)
        detail.setWordWrap(True)
        profiles_layout.addWidget(detail)
        row = QHBoxLayout()
        self.profile_name = QComboBox(profiles)
        self.profile_name.setEditable(True)
        self.profile_name.setAccessibleName("Capture profile name")
        self.save_button = QPushButton("Save profile", profiles)
        self.apply_button = QPushButton("Apply profile", profiles)
        self.delete_button = QPushButton("Delete profile", profiles)
        for widget in (self.profile_name, self.save_button, self.apply_button, self.delete_button):
            row.addWidget(widget)
        profiles_layout.addLayout(row)
        layout.addWidget(profiles)
        preferences = QGroupBox("Backup and restore preferences", self)
        backup_layout = QVBoxLayout(preferences)
        scope = QLabel("Casual backup: technical settings and capture profiles with generic names. Logs and screenshots are excluded. Restored preferences always start in Casual Mode; the current log folder is kept.", preferences)
        scope.setWordWrap(True)
        backup_layout.addWidget(scope)
        self.full_backup = QCheckBox("Include full preferences, triggers, ownership corrections, learned spellings and PvP reminder names", preferences)
        self.full_backup.setToolTip("Available only after confirming Elitist Scumbag Mode. This archive can contain teammate identities.")
        self.full_backup.toggled.connect(lambda _checked: self._refresh_enabled())
        backup_layout.addWidget(self.full_backup)
        backup_row = QHBoxLayout()
        self.backup_button = QPushButton("Create backup…", preferences)
        self.restore_button = QPushButton("Restore backup…", preferences)
        backup_row.addWidget(self.backup_button)
        backup_row.addWidget(self.restore_button)
        backup_layout.addLayout(backup_row)
        layout.addWidget(preferences)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Profile and backup status")
        layout.addWidget(self.status)
        self.save_button.clicked.connect(lambda: self.save_profile())
        self.apply_button.clicked.connect(lambda: self.apply_capture_profile())
        self.delete_button.clicked.connect(lambda: self.delete_profile())
        self.backup_button.clicked.connect(self._choose_backup)
        self.restore_button.clicked.connect(self._choose_restore)
        state_signal = getattr(engine, "state_changed", None)
        if state_signal is not None:
            state_signal.connect(lambda _state: self._refresh_enabled())
        self.load(cfg)

    def set_config_getter(self, getter: Callable[[], Config]) -> None:
        self._getter = getter

    def set_trigger_flush_callback(self, callback: Callable[[], object]) -> None:
        self._trigger_flush = callback

    def set_revenge_flush_callback(self, callback: Callable[[], object]) -> None:
        self._revenge_flush = callback

    def set_dimensions_getter(self, getter: Callable[[], tuple[int, int] | None]) -> None:
        self._dimensions_getter = getter

    def load(self, cfg: Config) -> None:
        self._cfg = cfg
        self.profile_name.clear()
        try:
            profiles = validate_profiles(cfg.capture_profiles)
        except ValueError:
            profiles = {}
            self.status.setText("Invalid saved profiles were ignored. Save a new profile to replace them.")
        self.profile_name.addItems(list(profiles))
        self.profile_name.setCurrentText(cfg.active_profile or "Default")
        if cfg.casual_mode or not cfg.casual_mode_confirmed:
            self.full_backup.setChecked(False)
        self.full_backup.setEnabled(not cfg.casual_mode and cfg.casual_mode_confirmed)
        self._refresh_enabled()

    def _refresh_enabled(self) -> None:
        stopped = not bool(getattr(self._engine, "is_running", False)) and getattr(self._engine, "state", "stopped") == "stopped"
        self.apply_button.setEnabled(stopped)
        self.restore_button.setEnabled(stopped)
        self.backup_button.setEnabled(stopped or not self.full_backup.isChecked())
        self.apply_button.setToolTip("Stop capture before applying a profile." if not stopped else "Apply this saved capture configuration.")
        self.restore_button.setToolTip("Stop capture before restoring preferences." if not stopped else "Restore a validated preference archive.")

    def _current(self) -> Config:
        cfg = self._getter()
        if not isinstance(cfg, Config) or cfg.problems():
            raise ValueError("Fix the capture settings before saving or backing up preferences.")
        return cfg

    def _stopped(self) -> None:
        if bool(getattr(self._engine, "is_running", False)) or getattr(self._engine, "state", "stopped") != "stopped":
            raise ValueError("Stop capture before applying a profile or restoring preferences.")

    def _dimensions(self) -> tuple[int, int] | None:
        if self._dimensions_getter is not None:
            return self._dimensions_getter()
        diagnosis = getattr(self._engine, "ocr_diagnosis", None)
        if not callable(diagnosis):
            return None
        frames = diagnosis().get("recent_ocr_frames", [])
        if frames:
            dimensions = frames[-1].get("input_dimensions", {})
            if not dimensions.get("cropped") and type(dimensions.get("width")) is int and type(dimensions.get("height")) is int:
                return dimensions["width"], dimensions["height"]
        return None

    def _persist(self, cfg: Config) -> None:
        save_config(cfg, str(self._path))
        self.load(cfg)
        self.config_changed.emit(cfg)

    def save_profile(self, name: str | None = None) -> bool:
        try:
            cfg = self._current()
            name = (name if name is not None else self.profile_name.currentText()).strip()
            try:
                profiles = dict(validate_profiles(cfg.capture_profiles))
            except ValueError:
                profiles = {}
            profiles[name] = capture_profile(cfg, self._dimensions())
            validate_profiles(profiles)
            self._persist(dataclasses.replace(cfg, capture_profiles=profiles, active_profile=name))
            self.status.setText(f"Saved capture profile {name}.")
            return True
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText(str(exc))
            return False

    def apply_capture_profile(self, name: str | None = None) -> bool:
        try:
            self._stopped()
            cfg = self._current()
            name = name if name is not None else self.profile_name.currentText()
            cfg = apply_profile(cfg, name)
            self._persist(cfg)
            self.status.setText(f"Applied capture profile {name}. Check the chat crop before starting capture.")
            return True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.status.setText(str(exc))
            return False

    def delete_profile(self, name: str | None = None) -> bool:
        try:
            cfg = self._current()
            name = name if name is not None else self.profile_name.currentText()
            profiles = dict(cfg.capture_profiles)
            del profiles[name]
            active = "Default" if cfg.active_profile == name else cfg.active_profile
            self._persist(dataclasses.replace(cfg, capture_profiles=profiles, active_profile=active))
            self.status.setText(f"Deleted capture profile {name}.")
            return True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.status.setText(str(exc))
            return False

    def create_backup(self, destination: str | Path, *, allow_sensitive: bool | None = None) -> dict:
        cfg = self._current()
        sensitive = self.full_backup.isChecked() if allow_sensitive is None else allow_sensitive
        if sensitive:
            if cfg.casual_mode or not cfg.casual_mode_confirmed:
                raise BackupError("Full preference backups require confirmed Elitist Scumbag Mode.")
            self._stopped()
        if self._trigger_flush is not None and self._trigger_flush() is False:
            raise BackupError("Could not save the current trigger preferences.")
        flush = getattr(self._engine, "flush_preferences", None)
        if sensitive and callable(flush) and flush() is False:
            raise BackupError("Could not save current ownership corrections and learned spellings.")
        if sensitive and self._revenge_flush is not None:
            try:
                saved = self._revenge_flush()
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                raise BackupError("Could not save the latest Revenge List activity. Retry before creating a full backup.") from exc
            if saved is False:
                raise BackupError("Could not save the latest Revenge List activity. Retry before creating a full backup.")
        with tempfile.TemporaryDirectory(prefix="pnut-preferences-") as temporary:
            config_path = Path(temporary) / "config.json"
            settings_path = Path(temporary) / "settings.json"
            atomic_json(config_path, dataclasses.asdict(cfg))
            atomic_json(settings_path, settings_payload(self._settings))
            # Unsaved technical form edits can name a future log folder. Preference
            # data still belongs to the engine's active configuration.
            active_cfg = getattr(self._engine, "config", None)
            data_root = project_path((active_cfg if isinstance(active_cfg, Config) else self._cfg).log_dir)
            return backup_profile(Path(destination), config_path, self._path.parent / "triggers.json", settings_path,
                                  allow_sensitive=sensitive, party_path=data_root / "party.json", vocab_path=data_root / "vocabulary.json",
                                  revenge_path=self._path.parent / "revenge.json")

    def restore_backup(self, source: str | Path) -> Config:
        self._stopped()
        current = self._current()
        data_root = project_path(current.log_dir)
        with tempfile.TemporaryDirectory(prefix="pnut-restore-") as temporary:
            settings_path = Path(temporary) / "settings.json"
            restore_profile(Path(source), self._path, self._path.parent / "triggers.json", settings_path,
                            party_path=data_root / "party.json", vocab_path=data_root / "vocabulary.json",
                            revenge_path=self._path.parent / "revenge.json", preserve_log_dir=current.log_dir)
            if settings_path.exists():
                apply_settings(self._settings, json.loads(settings_path.read_text(encoding="utf-8-sig")))
        cfg = load_config(str(self._path))
        self.load(cfg)
        self.config_changed.emit(cfg)
        self.restored.emit(cfg)
        self.status.setText("Restored preferences in Casual Mode. The current log folder was kept.")
        return cfg

    def _choose_backup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Create preference backup", "PNUT-preferences.zip", "Preference backups (*.zip)")
        if path:
            try:
                self.create_backup(path)
                self.status.setText("Preference backup saved. Logs and screenshots were excluded.")
            except (OSError, ValueError, TypeError) as exc:
                self.status.setText(str(exc))

    def _choose_restore(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Restore preference backup", "", "Preference backups (*.zip)")
        if path:
            try:
                self.restore_backup(path)
            except (OSError, ValueError, TypeError) as exc:
                self.status.setText(str(exc))

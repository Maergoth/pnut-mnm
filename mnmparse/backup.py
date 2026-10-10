"""Validated portable preference backups; restored profiles always start Casual."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from pathlib import Path
import tempfile
import zipfile

from mnmparse import __version__
from mnmparse.config import Config
from mnmparse.storage import atomic_write

MAX_BACKUP = 8 * 1024 * 1024
SCHEMA = 1


class BackupError(ValueError):
    pass


def _read_preference(path: Path) -> bytes:
    path = Path(path)
    if path.stat().st_size > MAX_BACKUP:
        raise BackupError("Preferences exceed the backup size limit.")
    with path.open("rb") as stream:
        data = stream.read(MAX_BACKUP + 1)
    if len(data) > MAX_BACKUP:
        raise BackupError("Preferences exceed the backup size limit.")
    return data


def _config_bytes(data: dict) -> bytes:
    if not isinstance(data, dict):
        raise BackupError("Backup configuration must be an object.")
    known = {f.name for f in dataclasses.fields(Config)}
    if set(data) - known:
        raise BackupError("Backup configuration contains unsupported settings.")
    try:
        defaults = dataclasses.asdict(Config())
        for key, value in data.items():
            default = defaults[key]
            if isinstance(default, bool) and not isinstance(value, bool):
                raise ValueError(key)
            if isinstance(default, str) and not isinstance(value, str):
                raise ValueError(key)
            if type(default) is int and (type(value) is not int):
                raise ValueError(key)
            if type(default) is float and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError(key)
        if "crop" in data and (not isinstance(data["crop"], (list, tuple)) or len(data["crop"]) != 4
                               or any(type(v) is not int for v in data["crop"])):
            raise ValueError("crop")
        if data.get("config_schema", 1) != 1:
            raise ValueError("schema")
        if "capture_profiles" in data:
            from mnmparse.profiles import validate_profiles
            validate_profiles(data["capture_profiles"])
        candidate = Config(**data)
        problems = candidate.problems()
    except (TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise BackupError("Backup configuration has invalid setting types.") from exc
    if problems:
        raise BackupError("Backup configuration is invalid: " + "; ".join(problems))
    data = dataclasses.asdict(candidate)
    data.update(casual_mode=True, casual_mode_confirmed=False, config_schema=1)
    return (json.dumps(data, indent=2) + "\n").encode()


def backup_profile(destination: Path, config_path: Path, triggers_path: Path,
                   settings_path: Path | None = None, *, allow_sensitive: bool = False,
                   party_path: Path | None = None, vocab_path: Path | None = None,
                   revenge_path: Path | None = None) -> dict:
    """Default backup contains technical configuration only, excluding raw data.

    Trigger expressions/examples, declared UI preferences, ownership corrections
    and learned spellings require a confirmed full-information profile. Logs and
    OCR images are never included. Restoring cannot authorize full-information mode.
    """
    try:
        config = json.loads(_read_preference(Path(config_path)).decode("utf-8-sig")) if Path(config_path).exists() else {}
    except (OSError, ValueError) as exc:
        raise BackupError("Could not read this profile's configuration.") from exc
    if not isinstance(config, dict):
        raise BackupError("Backup configuration must be an object.")
    if allow_sensitive and (config.get("casual_mode", True) or not config.get("casual_mode_confirmed", False)):
        raise BackupError("Full preference backups require confirmed Elitist Scumbag Mode.")
    # Config fields are technical preferences; exported templates can embed peers.
    if not allow_sensitive:
        for key in ("export_line", "export_actor", "export_separator"):
            config.pop(key, None)
        # Profile labels are arbitrary user text; keep their technical settings
        # under generic labels in a Casual archive.
        profiles = config.get("capture_profiles", {})
        if isinstance(profiles, dict):
            labels = {name: f"Profile {i + 1}" for i, name in enumerate(profiles)}
            config["capture_profiles"] = {labels[name]: value for name, value in profiles.items()}
            config["active_profile"] = labels.get(config.get("active_profile"), "Default")
    files = {"config.json": _config_bytes(config)}
    if allow_sensitive and Path(triggers_path).is_file():
        trigger_bytes = _read_preference(Path(triggers_path))
        _validate_triggers(trigger_bytes)
        files["triggers.json"] = trigger_bytes
    if allow_sensitive and settings_path is not None and Path(settings_path).is_file():
        files["settings.json"] = _read_preference(Path(settings_path))
        _validate_preferences(files["settings.json"])
    if allow_sensitive:
        for name, path, validator in (("party.json", party_path, _validate_party),
                                      ("vocabulary.json", vocab_path, _validate_vocabulary),
                                      ("revenge.json", revenge_path, _validate_revenge)):
            if path is not None and Path(path).is_file():
                files[name] = _read_preference(Path(path))
                validator(files[name])
    if sum(len(data) for data in files.values()) > MAX_BACKUP - 8192:
        raise BackupError("Preferences exceed the backup size limit.")
    manifest = {"schema": SCHEMA, "app_version": __version__, "sensitive": allow_sensitive,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as stream:
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, data in files.items():
                archive.writestr(name, data)
        stream.seek(0)
        atomic_write(destination, stream.read())
    return manifest


def _validate_triggers(data: bytes) -> None:
    from mnmparse.triggers import STORE_VERSION, TriggerStore
    try:
        payload = json.loads(data)
        if (not isinstance(payload, dict) or payload.get("version") != STORE_VERSION
                or not isinstance(payload.get("triggers"), list)
                or not all(isinstance(t, dict) for t in payload["triggers"])):
            raise ValueError("invalid trigger payload")
        TriggerStore().load_dict(payload)
    except (TypeError, ValueError, OSError, AttributeError) as exc:
        raise BackupError("Backup triggers are invalid.") from exc


def _validate_preferences(data: bytes) -> None:
    """The UI exports only its explicit safe registry subset as inert JSON."""
    def safe(value, depth=0):
        if depth > 4:
            return False
        if value is None or isinstance(value, (str, bool)):
            return not isinstance(value, str) or len(value) <= 16384
        if type(value) in (int, float):
            return math.isfinite(value)
        return isinstance(value, list) and len(value) <= 256 and all(safe(item, depth + 1) for item in value)
    try:
        payload = json.loads(data)
        values = payload.get("values") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or set(payload) != {"schema", "values"} or payload["schema"] != 1
                or not isinstance(values, dict) or len(values) > 256
                or any(not isinstance(key, str) or not 1 <= len(key) <= 256 or not safe(value)
                       for key, value in values.items())):
            raise ValueError("invalid preferences")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BackupError("Backup UI preferences are invalid.") from exc


def _validate_party(data: bytes) -> None:
    try:
        payload = json.loads(data)
        if (not isinstance(payload, dict) or payload.get("version") != 4
                or set(payload) - {"version", "saved", "seen", "manual_in", "manual_out", "pet_owners"}):
            raise ValueError("unsupported corrections schema")
        for field in ("manual_in", "manual_out"):
            values = payload.get(field, [])
            if not isinstance(values, list) or len(values) > 10000 or any(not isinstance(v, str) or not 1 <= len(v) <= 512 for v in values):
                raise ValueError(field)
        pets = payload.get("pet_owners", {})
        seen = payload.get("seen", {})
        if (not isinstance(pets, dict) or len(pets) > 10000
                or any(not isinstance(k, str) or not isinstance(v, str) or not 1 <= len(k) <= 512 or not 1 <= len(v) <= 512 for k, v in pets.items())
                or not isinstance(seen, dict) or len(seen) > 10000
                or any(not isinstance(k, str) or not 1 <= len(k) <= 512 or type(v) not in (int, float) or not math.isfinite(v) for k, v in seen.items())
                or type(payload.get("saved", 0)) not in (int, float) or not math.isfinite(payload.get("saved", 0))):
            raise ValueError("invalid corrections")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BackupError("Backup ownership corrections are invalid.") from exc


def _validate_vocabulary(data: bytes) -> None:
    from mnmparse.vocab import CATEGORIES
    try:
        payload = json.loads(data)
        if (not isinstance(payload, dict) or set(payload) != {"version", "names", "words"} or payload["version"] != 1
                or not isinstance(payload["names"], dict) or set(payload["names"]) - set(CATEGORIES)):
            raise ValueError("unsupported spelling schema")
        for table in (payload["words"], *payload["names"].values()):
            if (not isinstance(table, dict) or len(table) > 10000
                    or any(not isinstance(k, str) or not 1 <= len(k) <= 512 or type(v) is not int or not 1 <= v <= 2 ** 63 - 1 for k, v in table.items())):
                raise ValueError("invalid spelling counts")
    except (TypeError, ValueError, KeyError) as exc:
        raise BackupError("Backup learned spellings are invalid.") from exc


def _validate_revenge(data: bytes) -> None:
    def valid_name(name):
        # Manual labels are unrestricted text. Automatic attacker eligibility
        # belongs to the live controller, not preference validation.
        return isinstance(name, str) and bool(name.strip()) and len(name) <= 32767

    def valid_time(value):
        return value is None or (type(value) in (int, float) and math.isfinite(value)
                                 and 0 <= value <= 253402300799)

    try:
        payload = json.loads(data)
        if not isinstance(payload, dict) or type(payload.get("schema")) is not int:
            raise ValueError("invalid PvP reminder schema")
        if payload["schema"] == 1:
            if (set(payload) != {"schema", "names"} or not isinstance(payload["names"], list)
                    or len(payload["names"]) > 1000 or any(not valid_name(name) for name in payload["names"])):
                raise ValueError("invalid legacy PvP reminders")
        elif payload["schema"] == 2:
            entries = payload.get("entries")
            if set(payload) != {"schema", "entries"} or not isinstance(entries, list) or len(entries) > 1000:
                raise ValueError("invalid dated PvP reminders")
            names = set()
            for entry in entries:
                if (not isinstance(entry, dict) or set(entry) != {"name", "added_at", "last_activity"}
                        or not valid_name(entry["name"]) or not valid_time(entry["added_at"])
                        or not valid_time(entry["last_activity"]) or entry["name"].casefold() in names):
                    raise ValueError("invalid PvP reminder name or timestamp")
                names.add(entry["name"].casefold())
        else:
            raise ValueError("unsupported PvP reminder schema")
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise BackupError("Backup PvP reminder names or timestamps are invalid.") from exc


def restore_profile(archive_path: Path, config_path: Path, triggers_path: Path,
                    settings_path: Path | None = None, *, party_path: Path | None = None,
                    vocab_path: Path | None = None, revenge_path: Path | None = None,
                    preserve_log_dir: str | None = None) -> dict:
    """Validate the entire archive before writes, then restore with rollback."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            names = [e.filename for e in entries]
            allowed = {"manifest.json", "config.json", "triggers.json", "settings.json", "party.json", "vocabulary.json", "revenge.json"}
            if (len(names) != len(set(names)) or set(names) - allowed or "manifest.json" not in names
                    or "config.json" not in names or sum(e.file_size for e in entries) > MAX_BACKUP
                    or any(e.flag_bits & 1 for e in entries)):
                raise BackupError("Backup contains unsupported entries or exceeds its size limit.")
            files = {name: archive.read(name) for name in names}
        manifest = json.loads(files.pop("manifest.json"))
        if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
                or not isinstance(manifest.get("sensitive"), bool) or not isinstance(manifest.get("files"), dict)
                or set(manifest["files"]) != set(files)):
            raise BackupError("Backup schema is unsupported.")
        if any(hashlib.sha256(data).hexdigest() != manifest["files"][name] for name, data in files.items()):
            raise BackupError("Backup checksum validation failed.")
        if not manifest["sensitive"] and set(files) != {"config.json"}:
            raise BackupError("Casual backup contains sensitive preferences.")
        config_data = json.loads(files["config.json"])
        # The interactive restore can keep its active profile's local data root.
        # Imported paths never choose destinations for correction/spelling files.
        if preserve_log_dir is not None and isinstance(config_data, dict):
            config_data["log_dir"] = preserve_log_dir
        files["config.json"] = _config_bytes(config_data)
        if "triggers.json" in files:
            _validate_triggers(files["triggers.json"])
        for name, validator in (("settings.json", _validate_preferences), ("party.json", _validate_party),
                                ("vocabulary.json", _validate_vocabulary), ("revenge.json", _validate_revenge)):
            if name in files:
                validator(files[name])
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise BackupError(f"Could not validate backup: {exc}") from exc
    targets = {"config.json": Path(config_path), "triggers.json": Path(triggers_path)}
    if settings_path is not None:
        targets["settings.json"] = Path(settings_path)
    if party_path is not None:
        targets["party.json"] = Path(party_path)
    if vocab_path is not None:
        targets["vocabulary.json"] = Path(vocab_path)
    if revenge_path is not None:
        targets["revenge.json"] = Path(revenge_path)
    originals = {}
    changed = []
    try:
        for name, data in files.items():
            if name not in targets:
                continue
            path = targets[name]
            originals[path] = path.read_bytes() if path.exists() else None
            atomic_write(path, data)
            changed.append(path)
    except OSError as exc:
        rollback_errors = []
        for path in reversed(changed):
            try:
                if originals[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, originals[path])
            except OSError:
                rollback_errors.append(path.name)
        if rollback_errors:
            raise BackupError("Preference restore failed and rollback needs attention for: " + ", ".join(rollback_errors)) from exc
        raise
    return manifest

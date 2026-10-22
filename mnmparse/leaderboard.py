"""Anonymous maximum-stat snapshots and the public JSON-lines leaderboard format."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any

READ_URL = "https://raw.githubusercontent.com/Maergoth/pnut-mnm/leaderboard-data/leaderboard.jsonl"
MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 30_000
COUNTERS = ("personal_kills", "personal_deaths", "high_hit", "coin_looted")
MAX_NUMBER = 2**53 - 1
MAX_STAT = 1_000_000_000
ATTRIBUTE_FIELDS = ("strength", "stamina", "agility", "dexterity", "intelligence", "wisdom", "charisma")
RESIST_FIELDS = tuple("resist_" + name for name in
                     ("cold", "corruption", "disease", "electricity", "fire", "holy", "magic", "poison"))
READING_FIELDS = (*ATTRIBUTE_FIELDS, "armor_class", *RESIST_FIELDS)
TOTAL_FIELDS = ("stat_total", "resist_total")
EXTRA_STATS = (*READING_FIELDS, *TOTAL_FIELDS)
RECORD_FIELDS = ("max_health", "max_mana", *EXTRA_STATS)
STAT_LABELS = {"max_health": "Max Health", "max_mana": "Max Mana", "armor_class": "Armor Class",
               "stat_total": "Stat Total", "resist_total": "Resist Total",
               **{name: name.title() for name in ATTRIBUTE_FIELDS},
               **{name: name.removeprefix("resist_").title() + " Resist" for name in RESIST_FIELDS}}
LEGACY_FIELDS = frozenset({"public_key", "submission_id", "alias", "captured_at", "max_health", "max_mana",
                           *COUNTERS, "sequence", "capture_backend", "signature"})
FIELDS = LEGACY_FIELDS | {"schema_version", "message", *EXTRA_STATS}
ADJECTIVES = tuple("Agile Amiable Amber Ample Arctic Bold Brave Breezy Bright Brisk Calm Careful Cheery Clever Cozy Crisp Curious Daring Dazzling Dapper Eager Earnest Fancy Fearless Fleet Friendly Frosty Gentle Glowing Golden Graceful Grand Happy Hardy Honest Icy Jolly Jovial Keen Kind Lively Lucky Merry Mighty Mirthful Nimble Noble Patient Peaceful Plucky Polite Proud Quick Quiet Radiant Ready Rosy Shining Silent Sleepy Smooth Snowy Spry Steady Sunny Swift Tidy Tranquil Valiant Vibrant Warm Wily Wise Zesty".split())
COLORS = tuple("Aqua Azure Beige Black Blue Bronze Brown Coral Crimson Cyan Emerald Gold Gray Green Indigo Ivory Jade Lavender Lilac Lime Magenta Maroon Mint Navy Ochre Olive Orange Peach Pink Plum Purple Red Rose Ruby Rust Saffron Scarlet Silver Slate Tan Teal Turquoise Violet White Yellow".split())
ANIMALS = tuple("Alpaca Antelope Badger Bat Bear Beaver Bison Bobcat Buffalo Camel Caribou Cat Chamois Cheetah Cobra Cougar Coyote Crane Crow Deer Dingo Dolphin Dove Dragon Eagle Egret Elk Falcon Ferret Finch Fox Frog Gazelle Gecko Goat Goose Hare Hawk Hedgehog Heron Horse Hound Ibex Ibis Jaguar Jay Koala Lemur Leopard Lion Lizard Llama Lynx Mantis Marmot Mink Moose Moth Mouse Newt Ocelot Otter Owl Panda Panther Parrot Pelican Penguin Pika Pony Puffin Puma Quail Rabbit Raven Robin Salmon Seal Shark Skink Sloth Sparrow Squid Stoat Swan Tapir Tiger Toad Tortoise Toucan Trout Turtle Viper Walrus Weasel Whale Wolf Wombat Yak Zebra".split())


def utc_stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_stamp(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("Snapshot time must be UTC")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def phrase_key(phrase: str) -> bytes:
    """A phrase never becomes a character name, persisted setting or network field."""
    normalized = unicodedata.normalize("NFKC", phrase).strip()
    if not 8 <= len(normalized) <= 1024:
        raise ValueError("Use a secret phrase of 8 to 1024 characters")
    return hashlib.scrypt(normalized.encode("utf-8"), salt=b"PNUT anonymous leaderboard identity v1",
                          n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def public_key(private_key: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw()


def alias_for_key(key: bytes) -> str:
    digest = hashlib.sha256(key).digest()
    return " ".join(words[int.from_bytes(digest[i*8:(i+1)*8], "big") % len(words)]
                    for i, words in enumerate((ADJECTIVES, COLORS, ANIMALS)))


def daily_id(key: bytes, stamp: str) -> str:
    day = parse_stamp(stamp).date().isoformat()
    return hashlib.sha256(key + b":" + day.encode("ascii")).hexdigest()[:32]


def signing_bytes(record: dict[str, Any]) -> bytes:
    return json.dumps({k: v for k, v in record.items() if k != "signature"},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def valid_alias(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(" ")
    return len(parts) == 3 and all(part in words for part, words in zip(parts, (ADJECTIVES, COLORS, ANIMALS)))


def reading_stats(source: Any) -> dict[str, int | None]:
    """Normalize one observation; totals never borrow another reading's values."""
    get = source.get if isinstance(source, dict) else lambda name: getattr(source, name, None)
    values = {name: value if type(value := get(name)) is int and -MAX_STAT <= value <= MAX_STAT else None
              for name in READING_FIELDS}
    for total, group in (("stat_total", ATTRIBUTE_FIELDS), ("resist_total", RESIST_FIELDS)):
        values[total] = sum(values[name] for name in group) if all(values[name] is not None for name in group) else None
    return values


def validate_reading_stats(record: dict[str, Any]) -> None:
    for name in READING_FIELDS:
        value = record[name]
        if value is not None and (type(value) is not int or not -MAX_STAT <= value <= MAX_STAT):
            raise ValueError("Invalid character stat")
    expected = reading_stats(record)
    for name, group in (("stat_total", ATTRIBUTE_FIELDS), ("resist_total", RESIST_FIELDS)):
        value = record[name]
        if record["schema_version"] == 2:
            if (value is not None and type(value) is not int) or value != expected[name]:
                raise ValueError("Character total must match one complete reading")
        elif value is not None:
            # Individual bests need not coexist. Their sum is only an upper
            # bound, never a replacement for a complete-reading total record.
            if (type(value) is not int or not -len(group) * MAX_STAT <= value <= len(group) * MAX_STAT
                    or expected[name] is None or value > expected[name]):
                raise ValueError("Invalid personal-best total")


def validate_snapshot(record: Any) -> dict[str, Any]:
    """Whitelist public fields; names, paths and current health/mana cannot enter it."""
    if not isinstance(record, dict) or set(record) not in (LEGACY_FIELDS, FIELDS):
        raise ValueError("Unexpected snapshot fields")
    if set(record) == FIELDS:
        from mnmparse.leaderboard_messages import validate_message
        if type(record["schema_version"]) is not int or record["schema_version"] not in (2, 3):
            raise ValueError("Unsupported snapshot version")
        validate_message(record["message"])
        validate_reading_stats(record)
    if not isinstance(record["submission_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", record["submission_id"]):
        raise ValueError("Invalid anonymous submission ID")
    if not isinstance(record["public_key"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["public_key"]):
        raise ValueError("Invalid public key")
    key = bytes.fromhex(record["public_key"])
    if record["alias"] != alias_for_key(key):
        raise ValueError("Invalid anonymous alias")
    parse_stamp(record["captured_at"])
    if record["submission_id"] != daily_id(key, record["captured_at"]):
        raise ValueError("Invalid daily submission ID")
    if record["capture_backend"] != "window":
        raise ValueError("Leaderboard requires Window Capture")
    for field in ("max_health", "max_mana"):
        value = record[field]
        if field == "max_mana" and value is None:
            continue
        if type(value) is not int or not 0 < value <= 1_000_000_000:
            raise ValueError("Invalid maximum stat")
    for field in (*COUNTERS, "sequence"):
        value = record[field]
        if type(value) is not int or not (1 if field == "sequence" else 0) <= value <= MAX_NUMBER:
            raise ValueError("Invalid personal counter")
    if not isinstance(record["signature"], str) or not re.fullmatch(r"[0-9a-f]{128}", record["signature"]):
        raise ValueError("Invalid signature")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(bytes.fromhex(record["signature"]), signing_bytes(record))
    except (InvalidSignature, ValueError):
        raise ValueError("Snapshot signature does not match") from None
    return dict(record)


def make_snapshot(stats: dict[str, Any], counters: dict[str, int], private_key: bytes,
                  sequence: int, *, message: str = "") -> dict[str, Any]:
    """Build the existing version-2 current-reading format without changing its meaning."""
    return _make_signed_snapshot(stats, counters, private_key, sequence, message, 2, reading_stats(stats))


def make_personal_best_snapshot(fresh_context: dict[str, Any], records: dict[str, Any],
                                counters: dict[str, int], private_key: bytes, sequence: int,
                                *, message: str = "") -> dict[str, Any]:
    """Sign protected personal records using a fresh capture's date and backend."""
    best = {field: (records.get(field) or {}).get("value") for field in RECORD_FIELDS}
    stats = {"captured_at": fresh_context["captured_at"], "capture_backend": fresh_context.get("capture_backend"),
             "max_health": best["max_health"], "max_mana": best["max_mana"]}
    return _make_signed_snapshot(stats, counters, private_key, sequence, message, 3,
                                 {field: best[field] for field in EXTRA_STATS})


def _make_signed_snapshot(stats: dict[str, Any], counters: dict[str, int], private_key: bytes,
                          sequence: int, message: str, version: int,
                          character: dict[str, int | None]) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from mnmparse.leaderboard_messages import normalize_message
    key, stamp = public_key(private_key), stats["captured_at"]
    record = {"public_key": key.hex(), "submission_id": daily_id(key, stamp), "alias": alias_for_key(key),
              "captured_at": stamp, "max_health": stats.get("max_health"), "max_mana": stats.get("max_mana"),
              **{field: counters[field] for field in COUNTERS}, "sequence": sequence,
              "capture_backend": stats.get("capture_backend"), "schema_version": version,
              "message": normalize_message(message),
              **character}
    record["signature"] = Ed25519PrivateKey.from_private_bytes(private_key).sign(signing_bytes(record)).hex()
    return validate_snapshot(record)


def clipboard_text(record: dict[str, Any]) -> str:
    record = validate_snapshot(record)
    return _stat_block(record)


def local_clipboard_text(stats: dict[str, Any], status: dict[str, Any]) -> str:
    """Local copying also permits desktop readings; it never creates an upload payload."""
    record = {"alias": status.get("alias"), "captured_at": stats.get("captured_at"),
              "max_health": stats.get("max_health"), "max_mana": stats.get("max_mana"),
              **{field: status.get(field) for field in COUNTERS}, **reading_stats(stats)}
    if not valid_alias(record["alias"]):
        raise ValueError("Anonymous name is unavailable")
    parse_stamp(record["captured_at"])
    for field in ("max_health", "max_mana"):
        value = record[field]
        if field == "max_mana" and value is None:
            continue
        if type(value) is not int or not 0 < value <= 1_000_000_000:
            raise ValueError("Read fresh mini stats before copying")
    if any(type(record[field]) is not int or not 0 <= record[field] <= MAX_NUMBER for field in COUNTERS):
        raise ValueError("Stats history is unavailable")
    return _stat_block(record)


def _stat_block(record: dict[str, Any]) -> str:
    mana = str(record["max_mana"]) if record["max_mana"] is not None else "Unavailable"
    text = (f"{record['alias']} · {record['captured_at']}\n"
            f"Max Health: {record['max_health']}\nMax Mana: {mana}\n"
            f"Kills: {record['personal_kills']}\nDeaths: {record['personal_deaths']}\n"
            f"Highest hit: {record['high_hit']}\nCoin looted (copper): {record['coin_looted']}")
    if any(name in record for name in EXTRA_STATS):
        text += "".join(f"\n{STAT_LABELS[name]}: {record.get(name) if record.get(name) is not None else 'Unavailable'}"
                        for name in EXTRA_STATS)
    return text


def parse_records(text: str, *, strict: bool = False) -> list[dict[str, Any]]:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Leaderboard file is too large")
    result, seen = [], set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = validate_snapshot(json.loads(line))
        except (ValueError, TypeError, KeyError):
            if strict:
                raise ValueError("Invalid leaderboard record") from None
            continue
        if record["submission_id"] not in seen:
            result.append(record)
            seen.add(record["submission_id"])
        if len(result) > MAX_RECORDS:
            raise ValueError("Too many leaderboard records")
    return result


def dump_records(records: list[dict[str, Any]]) -> str:
    if len(records) > MAX_RECORDS:
        raise ValueError("Too many leaderboard records")
    text = "".join(json.dumps(validate_snapshot(record), separators=(",", ":"), ensure_ascii=True) + "\n" for record in records)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Leaderboard file is too large")
    return text

"""Bundled starter triggers, copied from Maergoth's original trigger definitions.

Only these public starters ship with the app. Personal triggers and audio settings stay
in the user's triggers.json. Stable IDs let upgrades preserve edits and deletions.
"""

INVIS_BREAK_ID = "637b7466b6"
INVIS_BREAK_PATTERN = "You begin to feel yourself appearing"
INVIS_BREAK_PATTERN_REVISION = f"{INVIS_BREAK_ID}:full-phrase-v1"
INVIS_BREAK_ALERT_REVISION = f"{INVIS_BREAK_ID}:immediate-alert-v1"
RESIST_ID = "e3b4c8a912"
RESIST_PATTERN = r"\bresists\s+your\b"
FIRST_INSTALL_ONLY = frozenset({RESIST_ID})

PRESETS: tuple[dict, ...] = (
    {
        "id": "a8089904bf", "name": "Gatekick", "pattern": "casting Gate",
        "action": "sound", "sound": "Alert", "volume": 10,
    },
    {
        "id": "357af85f56", "name": "Healkick", "pattern": "begins casting Heal",
        "fuzzy": False, "enabled": False, "action": "sound", "sound": "Alert", "volume": 10,
    },
    {
        "id": INVIS_BREAK_ID, "name": "Invis Break", "pattern": INVIS_BREAK_PATTERN,
        "action": "sound", "sound": "Falling", "speech": "InvisBreak",
        "cooldown_s": 2.0, "timer": False,
        "timer_warn_s": 0.0, "timer_warn_action": "none", "timer_end_action": "none",
    },
    {
        "id": RESIST_ID, "name": "Resist", "pattern": RESIST_PATTERN,
        "mode": "regex", "fuzzy": False, "enabled": True, "action": "speak", "speech": "resist",
        "timer": False, "timer_warn_action": "none", "timer_end_action": "none",
    },
)

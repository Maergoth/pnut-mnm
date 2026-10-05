"""Bundled starter triggers, copied from Maergoth's original trigger definitions.

Only these public starters ship with the app. Personal triggers and audio settings stay
in the user's triggers.json. Stable IDs let upgrades preserve edits and deletions.
"""

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
        "id": "637b7466b6", "name": "Invis Break", "pattern": "to appear",
        "action": "speak", "sound": "Falling", "speech": "InvisBreak",
        "cooldown_s": 2.0, "timer": True, "timer_seconds": 30.0,
        "timer_warn_s": 1.0, "timer_warn_action": "speak",
    },
)

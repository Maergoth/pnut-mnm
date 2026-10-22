"""Bounded, single-line messages for optional public leaderboard snapshots."""
from __future__ import annotations

import re
import unicodedata

MAX_MESSAGE_CHARS = 160
# Keep the Worker expression and cross-runtime fixtures in sync. Whole-word
# matching avoids altering ordinary words such as "class" or "cocktail".
SWEAR_PATTERN = (
    r"(?<!\w)(?ai:motherfuck(?:ers?|ing)?|fuck(?:ers?|ing|ed|s)?|"
    r"bullshit(?:ting|ted|s)?|shit(?:ty|ting|ted|s|heads?)?|bitch(?:es|ing|y)?|"
    r"assholes?|bastards?|cunts?|dicks?|cock(?:s|suckers?)?|"
    r"piss(?:ed|ing|es)?|damn(?:ed|ing)?|hell|ass)(?!\w)"
)
_SWEARS = re.compile(SWEAR_PATTERN)


def normalize_message(value: str) -> str:
    """Normalize spacing and censor common words before the payload is signed.

    This deliberately modest filter does not claim to identify every language,
    insult or deliberate spelling variation. Nothing in the result is markup.
    """
    if not isinstance(value, str):
        raise ValueError("A leaderboard message must be text")
    text = unicodedata.normalize("NFKC", value)
    text = "".join(" " if char.isspace() else char for char in text
                   if char.isspace() or not unicodedata.category(char).startswith("C"))
    text = " ".join(text.split())
    if len(text) > MAX_MESSAGE_CHARS:
        raise ValueError(f"Use a message of {MAX_MESSAGE_CHARS} characters or fewer")
    return _SWEARS.sub(lambda match: "*" * len(match[0]), text)


def validate_message(value: str) -> None:
    """Signed payloads must already contain the canonical public message."""
    if normalize_message(value) != value:
        raise ValueError("Leaderboard message needs normalizing")

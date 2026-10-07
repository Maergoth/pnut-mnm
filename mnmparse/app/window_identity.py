"""Temporary native window titles, independent of branding and saved preferences.

Each role keeps one title for this process so status and zone changes do not rename
the window. Nothing is persisted: starting another process generates fresh titles.
These names are cosmetic and do not make the application undetectable.
"""

from __future__ import annotations

from functools import lru_cache
import secrets


def window_title(role: str = "main") -> str:
    """Return an opaque title for a window role for this application launch."""
    return _role_title(role)


@lru_cache(maxsize=None)
def _role_title(role: str) -> str:
    return secrets.token_hex(12)

"""Compact display of captured copper totals using MnM's 100-to-1 coins."""
from __future__ import annotations


def format_coin(copper: int) -> str:
    """Format the fewest coins, omitting zero parts: 1,050 copper is 10s 50c."""
    copper = int(copper or 0)
    if copper == 0:
        return "0c"
    sign = "-" if copper < 0 else ""
    copper = abs(copper)
    pp, rest = divmod(copper, 1_000_000)
    gp, rest = divmod(rest, 10_000)
    sp, cp = divmod(rest, 100)
    parts = [f"{n}{unit}" for n, unit in ((pp, "p"), (gp, "g"), (sp, "s"), (cp, "c")) if n]
    return sign + " ".join(parts)

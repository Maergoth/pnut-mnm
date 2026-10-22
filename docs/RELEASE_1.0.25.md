# PNUT M&M 1.0.25

- Reader shortcuts remain active during capture when the normal combat overlays are hidden. Hover the combat-chat crop for its settings gear, or the mini-stat crop for settings, Copy and Submit.
- Monitor mapping supports Qt's friendly display names as well as Windows device names, including mixed display scaling. This fixes the reader controls silently failing to appear on affected monitors.
- Copy and Submit retain their existing eligibility checks. Stopping or pausing capture hides the read-area controls; demo and shutdown paths do not show them.
- Ranks includes STR, STA, AGI, DEX, INT, WIS, CHA, AC and all eight resists alongside maximum HP/Mana and lifetime personal counters. **Stat Total** adds the seven attributes; **Resist Total** adds the eight resists. Unread values remain unavailable.
- **Fresh reading** shows current stats. **Local best** keeps individual records and the best complete-reading attribute and resist totals; it never adds independent maxima together. Submissions use current readings.
- Each public snapshot uses three rows, repeating its bold gold anonymous name on every row. Sort by any stat while keeping each entry together. Hover an entry to see its capture timestamp.
- Add an optional message of up to 160 characters before submitting. Common English swear words are replaced with asterisks. The message appears in a chat bubble above the timestamp on hover. The first accepted snapshot and message remain unchanged on same-day retries.

Windows regression, frozen startup and extracted portable-package checks gate publication.

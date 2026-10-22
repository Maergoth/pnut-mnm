# PNUT M&M 1.0.25

- Reader shortcuts remain active during capture when the normal combat overlays are hidden. Hover the combat-chat crop for its settings gear, or the mini-stat crop for settings, Copy and Submit.
- Monitor mapping supports Qt's friendly display names as well as Windows device names, including mixed display scaling. This fixes the reader controls silently failing to appear on affected monitors.
- Copy and Submit retain their existing eligibility checks. Stopping or pausing capture hides the read-area controls; demo and shutdown paths do not show them.
- Ranks includes STR, STA, AGI, DEX, INT, WIS, CHA, AC and all eight resists alongside maximum HP/Mana and lifetime personal counters. **Stat Total** adds the seven attributes; **Resist Total** adds the eight resists. Unread values remain unavailable.
- **Fresh reading** shows current stats. **Local best** keeps individual records and the best complete-reading attribute and resist totals; it never adds independent maxima together. Submissions use current readings.
- Each public snapshot uses three rows, repeating its bold gold anonymous name on every row. Sort by any stat while keeping each entry together. Hover an entry to see its capture timestamp.
- Add an optional message of up to 160 characters before submitting. Common English swear words are replaced with asterisks. The message appears in a chat bubble above the timestamp on hover. The first accepted snapshot and message remain unchanged on same-day retries.

- Fresh installations include **Fizzle**, matching **Your spell fizzles!** with the existing
  Contains/OCR-forgiveness behavior. It immediately plays the bundled Fizzle recording at
  trigger volume 80, with no timer or cooldown.
- The supplied MP3 is included unchanged in portable packages and uses the configured
  master volume and output device. No Downloads path is required.
- Existing trigger profiles, edits and deleted starters are preserved. **Restore starter
  triggers** can add the missing Fizzle starter explicitly.

Windows regression, native startup and extracted portable-package checks gate publication.

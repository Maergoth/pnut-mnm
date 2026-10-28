# PNUT M&M 1.0.31

- **Session > Harvested** shows the total number of items obtained from successful
  harvest messages. Expand it for counts by material, such as **Wood: 2** and
  **Resin: 1**, in the main app and Session overlay.
- Each **You harvest [item]!** result contributes one item. Wood and Resin
  reported together count as two obtained items. Repeated results count again
  when confirmed as new messages by the existing reader.
- Harvest counts are available with **Count personal lines in Session** off.
  They persist across restarts and archived sessions, and appear in session JSON
  exports and imported PNUT logs.
- Older saved sessions start with no harvest totals. Reimporting their original
  PNUT logs can recover successful harvest messages those logs contain.
- Resist and slain-NPC lines can avoid a wrap wait when an earlier confirmed,
  punctuated message proves the final `l`, `I`, `1` or `|` was a misread `!`.
  Real names, spell ranks, and uncertain wrapped lines retain their existing
  handling. Two-read confirmation and duplicate protection are unchanged.
- **Net XP (est.)** and XP/hour now subtract confirmed backward bar movement
  and add resurrection recovery. Level-up chat still confirms rollovers;
  unexplained drops beyond the 25%-of-a-level death limit, allowing for bar-pixel
  resolution, remain unmeasured. Negative totals survive session recovery and exports.

# PNUT M&M 1.0.19

- Map downloads and updates keep working for known maps when the wiki API returns temporary 403 or other transport errors. PNUT uses saved map lists or bundled public wiki links and checks the image CDN's current file hashes before downloading. Updates explain when new-map discovery is unavailable, and failed downloads preserve cached files.
- Wiki API requests run one at a time and pause briefly after server errors. The wiki's "map coming soon" placeholder is no longer treated as a map.
- Same-hand hits within 0.05 seconds, including Double Attack, count as one swing for the auto-attack timer. Damage totals and explicit offhand/bow timing remain separate.
- Right-click the auto-attack bar and choose **Weapon Delay** to enter tenths of a second: **36 = 3.6 seconds**, **0 = automatic**. This contributes one initial interval sample when a hand has no timing reference. Observed swings take over naturally; changing the setting does not replace learned timing. The preference persists and is included in preference backups.

Installed copies can use **Settings > App updates**. For a portable copy, extract `PNUT-MnM-1.0.19-windows.zip` into a writable folder and run `PNUT M&M.exe` with `_internal` beside it.

# PNUT M&M 1.0.11

- **Rolling XP/hour:** Session uses a time-weighted estimate of the last hour, refreshed about every 30 seconds. Idle before the first gaining interval is excluded; breaks after gains lower the rate. After an hour without gains, the next gaining interval starts fresh. Measured session totals remain available, and older archived rates are labeled **XP session**.
- **XP detection:** an unreadable HP value no longer rejects a recognized XP bar. Brief OCR misses preserve the comparison; longer gaps, pausing or changing the stats region reset the recent window.
- **Stat OCR:** the stats region is read at 1.5× size, or your configured OCR scale if higher, for clearer HP readings. XP-bar measurements still use the original color pixels.
- **Unlocked overlays:** new setups start with overlays and the map unlocked. A map with no saved position opens at the primary screen's top-left. Saved lock and window preferences are preserved.
- **Mez history:** awakenings list targets and times. Nearby damage is no longer used to guess who broke mez, because the combat log does not name the breaker. Older saved guesses are excluded from displayed and exported results.
- **Revenge pop-out:** Players, Guilds and their **+** controls use the empty header space, leaving more room for names. The close button and resize grip remain available.
- **Revenge alerts:** saved players must exactly match a parsed combat participant. Item and spell names such as **Wolf Tooth** no longer cause false presence alerts.
- **Session scope:** the overlay labels Session as the whole run across all zones. With **Show other groups** off, Session shows your current group's loot, coin, kills, deaths and crafts; former members and unrecognized nearby players are excluded.

Download `PNUT-MnM-1.0.11-windows.zip`, extract it into a writable folder and run `PNUT M&M.exe` with `_internal` beside it. Installed copies can use **Settings > App updates**.

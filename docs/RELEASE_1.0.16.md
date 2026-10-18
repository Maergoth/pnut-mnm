# PNUT M&M 1.0.16

- **Group pet kill credit:** Session totals include final blows from your pets and manually assigned group pets. Those kills are credited to their owners, so the group and Mine only views retain the appropriate totals.
- **Existing session recovery:** pet ownership corrections reclassify saved kills as well as new ones. Explicit "Your pet" messages retain ownership for later generic kill messages and after session recovery. Existing session archives remain compatible.
- **Accurate death counts:** known pet deaths are excluded from monster kills and player deaths. Similar pet and owner spellings stay separate when ownership explicitly identifies them.
- **Rolling activity rates:** kills/hour, items/hour and coin/hour use the last hour, matching XP/hour's rolling horizon. New sessions use their shorter elapsed time. Events age out during idle periods, while the category totals still cover the whole session. Group coin measures gross loot; Mine only measures your received shares and unsplit personal loot.
- **Complete recent history:** rates use all recent records, including pet final blows, even when only the latest 200 detail rows are displayed. Saved previews retain their rate measurements; older previews without those measurements are labeled as recorded session averages.

Installed copies can use **Settings > App updates**. For a portable copy, extract `PNUT-MnM-1.0.16-windows.zip` into a writable folder and run `PNUT M&M.exe` with `_internal` beside it.

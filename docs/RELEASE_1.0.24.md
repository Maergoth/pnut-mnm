# PNUT M&M 1.0.24

- Hover settings gears float over each overlay's existing content. They do not reserve a column, add padding or enlarge a compact timer/attack bar. Short bars use a smaller gear; hover visibility, click-through and settings shortcuts still apply.
- NPC respawn timers created through the right-click menu show the same monster icon used on the map, centered inside the radial countdown. The icon follows the timer's normal, warning and expired colors, remains visible after expiration and stays when that timer is restarted. Other timers retain their existing display.
- The notable NPC menu puts levels before names. A sparkled sword marks known unique loot; a feather marks NPCs offering quests or required by quests. Cached details appear immediately and missing details load in the background.
- A fresh notable-NPC kill by you, your party or an owned pet plays a short synthesized victory fanfare and opens the NPC card, using only the current captured zone's notable list. The sound has its own toggle under Considered NPC and follows master volume/mute; the card respects its existing display settings. Cached lists work offline. Old logs, demos and duplicate slay/death notices do not replay celebrations.
- The main app's **Zones** page browses maps and notable NPCs independently of the live map. A permanent header provides zone, map-floor and NPC dropdowns; the adjacent NPC pane shares field settings, wiki links and lazy cached item tooltips, and works with overlays hidden or disabled. Its saved zone and map view stay separate from the live overlay.

Windows regression, startup and extracted portable-package checks gate publication. Installed copies update through **Settings > App updates** after this release is published.

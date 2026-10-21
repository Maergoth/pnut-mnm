# PNUT M&M 1.0.20

- Considering a mob opens a detachable NPC reference card, styled with the existing overlay. Level or level range and unique drops are enabled by default. Use **Settings > Considered NPC** to independently show level, common drops, unique drops, required quests and reputation gained/lost.
- Unlock the overlay to drag the NPC panel away. Double-click its header or choose **Snap to overlay** to dock it again. The panel follows the overlay's visibility, lock, click-through, opacity and font scale.
- The map header's **monster icon** lists notable NPCs for the selected zone. Selecting one displays the same information as considering it and respects the same field settings.
- Hover drop names for passive item-stat tooltips. Cached records appear immediately; visible drops warm the item cache in the background, and interactive hovers take priority.
- NPC details, notable-NPC lists and item statistics are saved in `npc_cache/`. Background checks compare wiki revision and template modification metadata before downloading changed content. Wiki errors preserve the existing cache.

The community wiki returned HTTP 503 during development and local acceptance. **First-time lookups still need the wiki to respond**, and current live NPC page formats remain unverified. Cached data stays available during outages; missing data is labeled unavailable. No fabricated NPC records are bundled.

Installed copies can use **Settings > App updates**. For a portable copy, extract `PNUT-MnM-1.0.20-windows.zip` into a writable folder and run `PNUT M&M.exe` with `_internal` beside it.

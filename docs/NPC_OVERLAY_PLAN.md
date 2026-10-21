# NPC overlay implementation and local acceptance

1. Cache public NPC details, zone notable-NPC lists and item statistics on disk.
   Read the cache before requesting revision metadata, fetch rendered content only
   when its revision or template modification timestamp changes, and preserve
   cached records on outages or unrecognized responses.
2. Add a detachable panel using the existing overlay's docking, appearance,
   input and visibility behavior. Default to level and unique drops. Expose
   independent toggles for level, common drops, unique drops, quests and reputation.
3. Add a monster icon to the map header. Populate its menu from the selected
   zone's cached notable NPCs and send selections through the same lookup as a
   parsed consider event. Warm item statistics while displaying drop lists.
4. Verify parser fixtures, revision checks, stale-data handling, asynchronous
   target changes, settings persistence and Qt window behavior. Build a local
   portable candidate and open a draft PR for review and local testing.

## Local acceptance checklist

For an interactive preview while the wiki is unavailable, run
`.venv\Scripts\python.exe scripts/preview_npc_overlay.py` from the source checkout.
It opens the real panel and map controls with explicitly synthetic NPC data,
temporary settings and the supplied Staff of Sigils statistics. It does not
capture the game, contact the wiki or write to your installed app.

- Open the candidate app; enable the overlay. In Settings > Considered NPC,
  confirm only Level / level range and Unique drops are enabled by default.
- Consider a mob with its name visible in the chat log. The NPC panel appears
  under the timer/attack panels. A target-free difficulty line leaves it alone.
- Enable each additional field and confirm the current card changes immediately.
  Save/reopen Settings and restart the candidate to check persistence.
- Unlock the overlay; drag the NPC panel away, then double-click its header or
  choose Snap to overlay. Locking prevents dragging. Global hide/show and mouse
  click-through affect the NPC panel alongside the other docked panels.
- On the map, select a zone and open the monster icon. Selecting a notable NPC
  should show the same card as considering that NPC, with the same field toggles.
- Hover an item. Cached statistics appear immediately in a passive item frame;
  a first lookup shows loading until statistics arrive. The game keeps focus.
- After a successful lookup, close/reopen the candidate without network access.
  Cached details, menu entries and item statistics remain available. Unavailable
  uncached records must not look like authoritative empty loot lists.
- Switch targets/zones rapidly during a slow request. Old responses must not
  replace the current target or the selected zone's menu.

The Miraheze wiki returned HTTP 503 during implementation; its pages also failed
the browser fetch. Tests use representative local MediaWiki HTML and mocked API
responses, including the Staff of Sigils statistics in the supplied reference.
No NPC loot, levels or faction values are fabricated or bundled as live data.
First-time lookups need the wiki to recover; already cached records do not.

Lookups request public page titles (the considered target, zone and item names),
not combat messages, statistics or logs. Records live in `npc_cache/` beside the
candidate app and persist across runs. Successful requests are rechecked at most
once per five minutes per record; failed requests retry after a minute.

The draft PR does not publish a release. Do not merge or deploy until local
acceptance is complete; main's versioned release workflow can publish releases.

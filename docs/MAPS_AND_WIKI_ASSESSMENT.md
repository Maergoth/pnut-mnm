# MnM Atlas and wiki providers

Assessed October 23, 2026. Public documentation, the current Atlas map manifest,
and a small sample of live wiki pages were checked. Authenticated partner data
has not been tested. Counts and examples below are observations on that date.

## Recommendation

Use **MnM Atlas as an optional interactive map provider** in both the floating
map and the main Zones page. Retain PNUT's saved image maps as the default and
offline fallback. Keep the existing Miraheze reference provider for now.

The newer Monsters and Memories Wiki is a promising structured-data provider,
especially for item tooltips and zone NPC browsing. Its documented partner API
fits PNUT better than scraping its rendered pages. A full provider migration
needs approved API access, agreed cache permissions, and coverage checks; the
examples do not establish that either wiki is universally more complete/current.

## Atlas integration

The map header's globe menu selects **Saved maps** or **MnM Atlas (online)**.
The two map views remember this preference independently. Atlas loads only when
selected and visible; its browser freezes while hidden. Existing PNUT notable-NPC
selections also send a name search to Atlas, whose own markers/search can expose
locations missing from PNUT's saved images.

The [official embed guide](https://www.mnmatlas.com/embed/) supports a compact
full browser (`?embed=1`), individual map embeds (`?embed=map`), floors, place and
NPC searches. PNUT uses the compact full browser as a native browser document,
with its map picker/search available. It does not impersonate either wiki's
iframe origin. Atlas wiki cards/links depend on its embedding context and data;
a marker is not a guarantee of a PNUT loot/quest record.

The [current manifest](https://www.mnmatlas.com/data/maps.json) contains a world
map and 27 zone maps, including **Caves of Irem**. Unknown zones fall back to the
world browser. The bundled zone-ID mapping is explicit rather than guessed from
zone names. Atlas remains a live external service; Retry and Use saved maps are
available if loading fails.

Atlas owns its coordinates, map layers, markers and notes. PNUT's existing labels
remain tied to their original image files and are not projected onto Atlas.
Atlas local notes use a persistent browser profile shared between PNUT's two
views, with a 64 MiB HTTP cache. Ordinary browser caching revalidates resources
according to the site's responses; PNUT does not pin a downloaded Atlas version
or mirror its data. Local viewing/notes do not require a separate player login.

Atlas's credits remain visible. **MnM Atlas and the original map authors** retain
their attribution; its [source repository](https://github.com/ocombe/mnmatlas)
documents source-map and third-party credits. Embedding the remote site does not
grant redistribution rights to its code or dataset. PNUT bundles Qt WebEngine
for the interactive view, increasing its download and installed size; the
renderer starts only when Atlas is used. Build checks exercise an offline blank
document and JavaScript in the delivered executable, without contacting Atlas.

The local Windows candidate measures **237.3 MB compressed / 575.8 MB extracted**
(226.3 / 549.1 MiB), compared with the current 1.0.26 release's 108.8 MB download.
This includes 532 ZIP entries after removing unused QML/Quick3D/Graphs modules;
the required native Quick/QML browser dependencies remain. Both the clean build
and extracted ZIP passed the actual browser renderer/JavaScript startup check.
CI's clean build may differ slightly in size. This candidate is not a published
release or a version bump.

## Which wiki better fits PNUT?

| Need | Existing Miraheze provider | New wiki's documented partner API |
| --- | --- | --- |
| NPC level/location | Public MediaWiki API and page parsing already integrated | Structured NPC records and zone NPC lists, stable IDs |
| Loot | Common/Unique/Quest Loot page sections | Linked loot records, conditional drop rates and item flags; item `unique` is not an NPC's Unique Loot category |
| Item hover cards | Parse item-page text/window fields | Explicit `window[]`, numeric stats, effects and class/race fields |
| Quests | Page links and headings | Giver/involved/listed NPC relationships; reward/needed item relationships |
| Reputation | Page information where present | Faction membership; no documented signed per-kill gain/loss values |
| Named locations | Saved images and notable-name extraction | Location prose plus Atlas integration; no documented general coordinate endpoint |
| Updates | Existing cached-first lookups with background revalidation | Paged indexes, `since=`, stable IDs and `updatedAt` support incremental refresh |

See the new wiki's [partner guide](https://monstersandmemories.wiki/api/partner/v1/docs)
and [OpenAPI schema](https://monstersandmemories.wiki/api/partner/v1/openapi.json).
API access requires an administrator-issued key; the documented rate is
120 requests/minute/key with retry headers. PNUT has not requested a key or
embedded one in the desktop app.

The new site's sitemap lists 2,216 NPC URLs, 5,952 item URLs, 145 quests,
30 zones and 112 factions. These are indexed URLs, including incomplete entries,
not verified complete records. Its item browser reports 4,926 entries, illustrating
different populations. These counts alone cannot establish more raw coverage.

Live sample comparisons show why provenance matters:

- [Night Terror on the new wiki](https://monstersandmemories.wiki/npcs/night-terror)
  and [Miraheze](https://monstersandmemories.miraheze.org/wiki/Night_Terror)
  agree on level 8 and A Winged Terror, but differ in loot names/grouping.
- [Staff of Sigils](https://monstersandmemories.wiki/items/staff-of-sigils)
  agrees with [Miraheze's sampled combat stats](https://monstersandmemories.miraheze.org/wiki/Staff_of_Sigils),
  but weight differs (4.75 versus 7.5). No independent game reading resolves it.
- Both live pages for [Enchanted Bronze Plate Breastplate](https://monstersandmemories.wiki/items/enchanted-bronze-plate-breastplate)
  report AC 30/weight 6.0, unlike the earlier supplied screenshot.
- Guard Adnan is absent from both sampled providers. Switching providers alone
  would not supply that NPC's details.

PNUT's existing Night Terror parser also misses a unique-loot entry present in
the Miraheze HTML. That is an extraction gap, not missing source data, and should
be addressed separately from choosing a provider.

## Conditions for a later structured provider

Use the [partner request process](https://monstersandmemories.wiki/contact?subject=Partner%20API%20request),
not an undocumented bulk scrape. The site's [copyright policy](https://monstersandmemories.wiki/copyright)
and [robots instructions](https://monstersandmemories.wiki/robots.txt) direct fan
tools to the partner API. Its guide requires a linked **Data from the Monsters
and Memories Wiki** attribution wherever its data appears, and direct links for
tagged records. Atlas's own partnership does not grant PNUT API access.

Subject to approved cache terms, continue showing cached records immediately,
then revalidate in the background. Initially index all metadata: imported records
can have `updatedAt:null` and are excluded from `since=` results. Subsequent
bounded delta checks should honor retry headers and preserve stale data during
outages. Confirm deletion handling, null timestamps, and whether edits to linked
items advance an NPC's timestamp before relying entirely on deltas. Keep
provider-specific IDs, source links and caches; conflicting values must not
silently overwrite each other. Cache retention/offline redistribution terms need
confirmation before migrating PNUT's existing local caches to this provider.

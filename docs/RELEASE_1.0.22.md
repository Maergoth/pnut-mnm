# PNUT M&M 1.0.22

- Restore NPC, item tooltip and notable NPC lookups against the wiki's current API. Read cached details first, check revision metadata, and replace cached content only after a matching revision is successfully parsed.
- Read the wiki's current mob stat boxes, variant sections and notable NPC tables. Keep common, unique and quest loot separate and preserve available level ranges and reputation entries.
- Recognize named NPC kindness reactions with appended difficulty text, including captures where OCR omits the `--` separator. Bare player difficulty messages do not open the NPC card. A confirmed missing wiki page is reported separately from an outage; Guard Adnan currently has no published page.
- Recognize named NPC pets, manual party coin redistribution, complete `/who` summaries, guild notices and spaced Hardcore scores. Preserve spell expiration and unattributed effect notices without inventing new damage or action credit.

The [October 21 log audit](log-audit-2026-10-21.md) records the fixed-snapshot replay and remaining unreadable entries. First-time lookups still require a successful wiki response; an outage or rejected response preserves existing cached details.

This is a local test candidate. Windows PR/package gates and local acceptance must pass before publication.

# PNUT M&M 1.0.28

- Cached map images display across the full map window again. Resizing and
  switching back from Atlas preserve the image, zoom and image-specific labels.
- **MnM Atlas (online)** is the startup default for Map and Zones. Choose
  **Saved maps (offline)** instead in **Settings > Update maps > Default map**.
  The setting applies immediately to both views and is remembered on restart.
  The globe menu still switches either view temporarily for the current run.
- Named NPCs with connectors, such as **Keeper of the Road**, now parse across
  kills, damage, loot and consider messages. Earlier combat readings also allow
  the final exclamation mark misread as `l` to be repaired without changing the
  original log text or trimming independently observed legitimate names.
- Correctly recognized live notable kills can trigger the existing fanfare and
  NPC details panel according to your settings. Hit deduplication is unchanged.
- Separate mini-stat and chat readers now sample and recognize text on independent
  workers. A slow stat read cannot hold up Resist or other chat alerts, and stat
  warnings can confirm even while chat OCR is busy. The stat worker uses its own
  recognizer and keeps only the newest pending frame, with stale results discarded.
  Chat still requires two confirming reads and uses the same deduplication rules.
  Shared-crop mode continues using one OCR result and excludes verified stat rows
  before chat processing. Capture rate and trigger matching settings are unchanged.

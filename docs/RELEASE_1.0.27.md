# PNUT M&M 1.0.27

- The map header's globe menu offers **MnM Atlas (online)** in both the floating
  map and the main **Zones** browser. Atlas adds interactive maps, floors,
  location markers, searches and its own local notes, including Caves of Irem.
- Selecting a notable NPC from PNUT's monster menu continues opening PNUT's
  consider panel and also searches Atlas for its location. Atlas's own markers
  use Atlas popups; they do not drive PNUT's consider panel.
- **Saved maps** remains the default and offline fallback. Existing labels keep
  their original image-file coordinates. The two map views remember their source
  choices independently; Atlas notes share dedicated local browser storage.
- Interactive maps use the installed **Microsoft WebView2 Runtime**, through a
  small native adapter. PNUT does not bundle a second browser engine. If the
  runtime is missing, the app offers Microsoft's download page or saved maps.
  No player account or sign-in is needed for viewing maps or local Atlas notes.
- Atlas loads only when selected and visible. Loading failures offer Retry and
  Use saved maps. External links open in the system browser; the overlay retains
  its unlocked resize border and drag handle.

The [wiki-provider assessment](MAPS_AND_WIKI_ASSESSMENT.md) documents Atlas and the
newer wiki's structured partner API. PNUT continues using its existing cached
reference data while partner access and cache terms are evaluated.

Windows regression, native renderer/JavaScript startup, executable provenance and
extracted portable-package checks gate publication.

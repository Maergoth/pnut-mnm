# PNUT M&M 1.0.23

- A verified downloaded update shows a visible ten-second restart countdown, including when the main window is hidden. PNUT then saves capture/session state and applies the update automatically. **Restart now** applies immediately; **Later** keeps the download ready in Settings and the tray. Existing builds need one update/restart to gain this behavior.
- An update does not discard a Settings draft or continue after pending changes cannot be saved. Failed installation preparation leaves the running app open. A release that failed and rolled back requires an explicit retry rather than repeatedly restarting automatically.
- Unlock the considered NPC card to resize it vertically, horizontally or from its dotted bottom-right grip. Dimensions are remembered; wider cards use more columns while overflow remains scrollable.
- Click a mob name or drop name to open its wiki page. Item hover tooltips remain available; click-through ignores these links.
- Items present in both loot categories appear only as common drops, including previously cached records. Fields without available wiki data do not add headings or placeholder rows.
- Hover the combat overlay, timers, auto-attack bar, considered NPC card, map or Revenge List popout to reveal a top-left gear. Clicking opens that panel's relevant settings in the main app. Unique loot is displayed before common loot.

Windows regression, startup and extracted portable-package checks gate publication. Installed copies can update through **Settings > App updates** once this release is published.

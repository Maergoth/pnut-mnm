# PNUT M&M 1.0.14

- **Reliable timer matching:** live chat triggers run in the background with independent search allowances. Compiling a pattern, checking earlier triggers and Windows scheduling delays no longer steal a simple expression's matching time. Scheduling delays are retried without disabling the trigger; genuinely expensive expressions still have safety protection and slowdown guidance.
- **Accurate delayed countdowns:** timers and repeat suppression use the time PNUT received the chat line. A result delivered two seconds late starts with two fewer seconds remaining. Expired delayed timers do not receive fresh countdowns or play obsolete start cues, and queued results are discarded after relevant edits, deletion, timer clearing or shutdown.
- **One overlay visibility switch:** Hide Overlay hides the map, detached Revenge List, timer and attack panels, notifications and visual alerts. Show Overlay restores the panels you left open, preserving their positions and map fullscreen state. Panels you closed individually remain closed, and new activity cannot reveal hidden overlay windows.
- **Useful slowdown diagnostics:** logs record queue delays, matching duration and actual matcher CPU use without recording chat text or trigger patterns. A full pending queue produces an explicit warning.

Installed copies can use **Settings > App updates** once this release is published. For a portable copy, extract `PNUT-MnM-1.0.14-windows.zip` into a writable folder and run `PNUT M&M.exe` with `_internal` beside it.

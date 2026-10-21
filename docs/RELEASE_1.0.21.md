# PNUT M&M 1.0.21

- Unlock the overlay to resize the NPC card horizontally from either side. Its width is remembered independently of the combat overlay. Long lists flow into additional columns when space allows, with scrolling for overflow.
- NPC cards fade and hide within five seconds. Hovering, scrolling, resizing or using the context menu keeps the card visible; leaving starts a fresh five-second countdown. Considering the same NPC again reopens it. Background wiki updates do not reopen dismissed cards.
- The low-health warning covers a wider area that scales with display resolution and continues until a valid HP reading reaches the configured threshold. Unreadable HP preserves the warning without repeating its sound. Pausing or stopping capture, losing the capture source, or disabling health monitoring clears the warning and requires fresh confirmation.
- Reduced motion uses a steady low-health warning and dismisses NPC cards without a fade animation.

The feature changes passed the Windows PR gate, including native-window and portable-package startup checks, and local user acceptance. The final version-specific release gate is pending.

The previously accepted wiki outage limitation remains: first-time NPC and item lookups need the community wiki to respond. Existing cached records remain available during outages; current live NPC page formats remain unverified.

Installed copies can use **Settings > App updates**. For a portable copy, extract `PNUT-MnM-1.0.21-windows.zip` into a writable folder and run `PNUT M&M.exe` with `_internal` beside it.

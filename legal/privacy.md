# PNUT M&M Privacy Policy

Effective date: October 23, 2026

PNUT M&M processes captured game text on your computer. PNUT has no built-in telemetry or automatic upload of screenshots, combat logs, settings, or diagnostic reports to the project maintainers. Some features download maps or updates from external services. The optional leaderboard publishes an anonymous stat snapshot only when you choose Submit, as described below.

## Capture and local processing

PNUT captures the game window or a configured desktop region, then recognizes text in the selected Combat chat area using on-device Windows OCR or the optional local RapidOCR engine. Captured text can include player names, chat messages, combat events, loot, and zone names. If you select a different area, its visible content can also be processed. Desktop-region capture can include other windows covering that region.

Frames and text are used locally for calibration previews, combat summaries, session tracking, and triggers or timers. PNUT does not send captures to a cloud OCR service. It reads displayed pixels rather than game process memory or network traffic.

## Local files and settings

PNUT saves configuration, raw recognized combat messages, parsed event logs, learned names and vocabulary, party information, and session or OCR tracking state. Application logs can contain copied combat summaries, errors, and file paths. Trigger definitions may include custom text and sound-file paths. Display preferences, map state, and remembered respawn durations are saved in native application settings, including the Windows registry through Qt QSettings.

Files normally reside beside the application or in its configured log folder. Downloaded maps are cached locally; update packages are staged in a local application-data or temporary folder. Ordinary configuration and log files can be read by other software with access to their folder. Access by other users, software, backup tools, or cloud-synced folders depends on your computer and storage choices. Leaderboard identity, local cumulative totals, individual stat records with capture times, and cached public entries are stored separately beside the selected configuration.

Combat and event logs are not automatically deleted. The application log rotates. Previous sessions are stored in a local database with a configurable retention period (30 days by default), and the recent editable window is bounded (100 fights by default). Update staging and owned old-install folders have bounded cleanup. You can stop capture, change the capture area or log folder, and remove saved files or native settings with PNUT closed.

Carebear Mode is on by default. Performance views and exports show your own information and eligible rounded group averages, hiding other members' individual results to prevent direct comparisons. Ordinary tools still show captured chat, trigger definitions and matches, timer labels, and Revenge List entries. Carebear does not change capture, OCR or parsing input. Switching into Elitist Scumbag Mode requires a deliberate switch gesture and confirmation. Original local logs and recovery data remain available on disk. Turning Carebear on cannot recall information already copied, exported, spoken, or shared outside PNUT.

Each damage/DPS average requires at least three active damage contributors whose damage is at least their healing. Each healing/HPS average requires at least three active healing contributors whose healing is at least their damage. Positive ties count in both; inactive members count in neither, and assigned pets count with their owners. Unavailable role averages show — and are omitted from exports.

Choosing Capture frame shows a local game image so you can position the Combat chat rectangle. This preview and OCR test results can show other players' chat in either mode. The setup preview is cleared when setup closes.

Revenge List is an optional PvP feature, disabled by default and available in either mode when enabled. Eligible incoming attackers can prompt a manual save for 30 seconds, once per attacker per session. Automatic prompts use a letters-only name heuristic that excludes articles, spaces and symbols; manual entries accept any nonempty name or label. Only entries you explicitly add are saved, together with addition and latest observed attack times. The main list, overlay tab and optional pop-out show the newest activity first, filtered to 30 days and 100 entries by default; either display limit can be changed or set to All. Filtering does not delete saved entries. Older name-only lists retain unknown dates until new activity supplies one. Valid-looking named NPCs can also match the automatic heuristic.

Preference backups are available in either mode. The default backup contains configuration and named capture profiles; the full option also includes triggers, declared UI preferences, ownership corrections, learned spellings and Revenge List entries. Logs and screenshots are excluded. Restoring starts in Carebear Mode and keeps the current log folder.

## External connections

Map features request zone pages and map images from the Monsters & Memories Wiki at `monstersandmemories.miraheze.org` and its image hosts, `static.wikitide.net` and `static.miraheze.org`. Showing a map or following a zone change can download a missing map automatically. Refreshes and the optional startup download also make requests. These requests identify the requested zone or image.

Selecting the optional MnM Atlas provider opens `www.mnmatlas.com` inside PNUT. Its map URLs identify the selected zone and, when used, the NPC search name. The site and its resource providers receive your IP address and ordinary browser request metadata under their own policies. PNUT does not send captured frames, chat, combat logs or settings to Atlas. Its dedicated local `atlas_cache/` folder holds browser storage, including Atlas notes and cookies, plus a bounded HTTP cache. This storage is shared by PNUT's map views and retained when you hide the map; you can remove it with PNUT closed. PNUT's saved image labels remain separate and are not transferred to Atlas. Opening Atlas in your external browser uses that browser's storage and privacy settings instead.

NPC reference features request public page titles from the same wiki: a considered target's name, the selected map zone, and item names in its displayed drops. Named NPCs and players cannot always be distinguished from the captured text, so considering a player can also request that name. Disable Show NPC details in Settings to stop automatic considered-target lookups. Opening a map still checks that zone's notable-NPC list. Details are cached locally in `npc_cache/`; cached records appear before background revision checks, and unchanged records do not download their content again. These requests do not include combat messages, logs, screenshots, or performance results.

Update checks and downloads contact GitHub and its release-file hosts for the `Maergoth/pnut-mnm` repository. The request includes the PNUT version in its User-Agent. Startup checks run when that option is enabled.

These services receive your IP address and normal request metadata, and apply their own privacy practices. PNUT's map and update requests do not upload combat logs, screenshots, or settings. Links opened in your browser are also subject to the destination's practices.

## Optional public leaderboard

The Ranks page and mini-stat reader offer separate Copy and Submit actions. Copy only places your current anonymous summary on the system clipboard. Submit publishes one snapshot per anonymous identity per UTC day. It requires a fresh mini-stat reading from Window Capture. Desktop Capture can be used for local copying but does not contribute leaderboard totals or eligible submissions. Opening PNUT or the Ranks page does not submit data or refresh the public file automatically.

A submitted snapshot contains your persistent three-word anonymous name and identity, update timestamp, personal-best maximum health and mana, armor class, seven attributes (STR, STA, AGI, DEX, INT, WIS and CHA), eight resists (Cold, Corruption, Disease, Electricity, Fire, Holy, Magic and Poison), attribute and resist totals, cumulative personal kills and deaths, highest personal hit, and gross corpse coin looted in copper before party splitting. Individual personal bests can come from different readings. Each total is the highest observed complete reading of its group, rather than a sum of independent records; it remains unavailable until a complete reading is observed. A fresh Window Capture reading establishes submission eligibility and the update time. Copy uses current values, without substituting the stored personal bests. Weight, endurance, current health and mana, character names, screenshots, captured chat messages and combat logs are not added automatically. Public records also include identifiers used to recognize the same identity and daily submission. The same identity links your snapshots across days.

You can add an optional public message of up to 160 characters to a submission. Common English swear words are replaced with asterisks. Other people see the message when hovering your entry, above the update timestamp. The first accepted daily snapshot and message are retained on retries. This text comes from what you type; avoid including names or other personal information you do not want made public.

Submit contacts the hosted service at `pnut-leaderboard-upload.maergoth.chatgpt.site`, running through Sites on Cloudflare. That service publishes the record to the public `Maergoth/pnut-mnm` GitHub repository's `leaderboard-data` branch. Refresh public entries reads that public file from GitHub; cached entries remain available locally. These providers receive the connection's IP address and normal request metadata under their own practices. No external player sign-in is required.

An optional secret phrase recreates your anonymous identity on another installation. PNUT does not save or upload the phrase. Reusing a phrase recreates the same identity; changing identity starts new local totals. Refreshing public entries can restore that identity's previously published totals and personal-best records. Recovered stat record times refer to the public snapshot's update time, since it may combine individual records from different readings. Session resets do not erase the local lifetime totals or previously published snapshots.

Submitted records are public and have no automatic expiry. Anyone can read, copy or redistribute them. Removing an entry from the current leaderboard does not remove GitHub history or copies held by others. You can choose not to submit, and can remove local leaderboard files with PNUT closed; that does not recall already published records. Contact the project through the support channels below for a public-entry removal request.

## Clipboard, exports, and audio

PNUT can copy combat summaries to the system clipboard, including automatically after a completed group fight when automatic export is enabled; new profiles default this option to off. Other applications and clipboard-history or sync features may access that text.

Diagnostic export creates a ZIP at a location you choose. In either mode, a report can include a cropped screenshot, recent recognized messages, player names, settings, file paths, application and operating-system details, and capture or display information. The command-line snapshot option can save a crop locally in either mode. These exports are not uploaded by PNUT.

Timer sharing copies a code to the clipboard or exports a JSON file. Definitions can contain names, patterns, speech text, and sound-file paths; the sound-file contents are not bundled. PNUT does not send the code to game chat for you. Review exports before sharing them.

Optional sounds and speech use local audio files and the system text-to-speech interface. Spoken text may include captured names or messages and can be heard or recorded by others. Your operating system and selected speech engine have their own settings and privacy practices.

## Support and public sharing

Contact the project through [GitHub issues](https://github.com/Maergoth/pnut-mnm/issues) or `@Maergoth` on Discord. Information you send or post is subject to the receiving service's practices and may be used to investigate your report.

GitHub issues are public. Logs, diagnostic ZIPs, screenshots, and timer definitions can reveal your information and other players' messages or names. Review and redact them before posting; do not include passwords, account details, or private conversations. Posting publicly can make copies available beyond the project's control.

See the [Terms and Disclaimer](terms.md) for licensing and use information. This policy describes the current PNUT features; future changes should be reflected in a revised policy and effective date.

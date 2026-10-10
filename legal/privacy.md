# PNUT M&M Privacy Policy

Effective date: October 10, 2026

PNUT M&M processes captured game text on your computer. PNUT has no built-in telemetry or automatic upload of screenshots, combat logs, settings, or diagnostic reports to the project maintainers. Some features download maps or updates from external services, as described below.

## Capture and local processing

PNUT captures the game window or a configured desktop region, then recognizes text in the selected Combat chat area using on-device Windows OCR or the optional local RapidOCR engine. Captured text can include player names, chat messages, combat events, loot, and zone names. If you select a different area, its visible content can also be processed. Desktop-region capture can include other windows covering that region.

Frames and text are used locally for calibration previews, combat summaries, session tracking, and triggers or timers. PNUT does not send captures to a cloud OCR service. It reads displayed pixels rather than game process memory or network traffic.

## Local files and settings

PNUT saves configuration, raw recognized combat messages, parsed event logs, learned names and vocabulary, party information, and session or OCR tracking state. Application logs can contain copied combat summaries, errors, and file paths. Trigger definitions may include custom text and sound-file paths. Display preferences, map state, and remembered respawn durations are saved in native application settings, including the Windows registry through Qt QSettings.

Files normally reside beside the application or in its configured log folder. Downloaded maps are cached locally; update packages are staged in a local application-data or temporary folder. These files are not encrypted by PNUT. Access by other users, software, backup tools, or cloud-synced folders depends on your computer and storage choices.

Combat and event logs are not automatically deleted. The application log rotates. Previous sessions are stored in a local database with a configurable retention period (30 days by default), and the recent editable window is bounded (100 fights by default). Update staging and owned old-install folders have bounded cleanup. You can stop capture, change the capture area or log folder, and remove saved files or native settings with PNUT closed.

Carebear Mode is on by default. App views and exports show your own information and rounded group averages when at least three group members are known. Switching into Elitist Scumbag Mode requires a deliberate switch gesture and confirmation. Carebear Mode obscures presentation; original local logs and recovery data remain available on disk and are not encrypted. Turning it on cannot recall information already copied, exported, spoken, or shared outside PNUT.

Each damage/DPS average requires at least three active damage contributors whose damage is at least their healing. Each healing/HPS average requires at least three active healing contributors whose healing is at least their damage. Positive ties count in both; inactive members count in neither, and assigned pets count with their owners. Unavailable role averages show — and are omitted from exports.

During setup, choosing Capture frame shows a local game image so you can position the Combat chat rectangle. This preview can show other players' chat even in Carebear Mode. It is cleared when setup closes; OCR results and exports remain protected by the selected mode.

Revenge List is an optional PvP feature, disabled by default. Eligible incoming attackers can prompt a manual save for 30 seconds, once per attacker per session. Automatic prompts use a letters-only name heuristic that excludes articles, spaces and symbols; manual entries accept any nonempty name or label. Only entries you explicitly add are saved, together with addition and latest observed attack times. The main list, overlay tab and optional pop-out show the newest activity first, filtered to 30 days and 100 entries by default; either display limit can be changed or set to All. Filtering does not delete saved entries. Older name-only lists retain unknown dates until new activity supplies one. Names are hidden in Carebear Mode; valid-looking named NPCs can also match the automatic heuristic.

## External connections

Map features request zone pages and map images from the Monsters & Memories Wiki at `monstersandmemories.miraheze.org` and its image hosts, `static.wikitide.net` and `static.miraheze.org`. Showing a map or following a zone change can download a missing map automatically. Refreshes and the optional startup download also make requests. These requests identify the requested zone or image.

Update checks and downloads contact GitHub and its release-file hosts for the `Maergoth/pnut-mnm` repository. The request includes the PNUT version in its User-Agent. Startup checks run when that option is enabled.

These services receive your IP address and normal request metadata, and apply their own privacy practices. PNUT's map and update requests do not upload combat logs, screenshots, or settings. Links opened in your browser are also subject to the destination's practices.

## Clipboard, exports, and audio

PNUT can copy combat summaries to the system clipboard, including automatically after a completed group fight when automatic export is enabled; new profiles default this option to off. Other applications and clipboard-history or sync features may access that text.

Diagnostic export creates a ZIP at a location you choose. Carebear reports include technical settings and counters, without OCR text or pixels. Confirmed full-information reports can include a cropped screenshot, recent recognized messages, player names, settings, file paths, application and operating-system details, and capture or display information. The command-line snapshot option also requires confirmed full mode before saving a crop locally. These exports are not uploaded by PNUT.

Timer sharing copies a code to the clipboard or exports a JSON file. Definitions can contain names, patterns, speech text, and sound-file paths; the sound-file contents are not bundled. PNUT does not send the code to game chat for you. Review exports before sharing them.

Optional sounds and speech use local audio files and the system text-to-speech interface. Spoken text may include captured names or messages and can be heard or recorded by others. Your operating system and selected speech engine have their own settings and privacy practices.

## Support and public sharing

Contact the project through [GitHub issues](https://github.com/Maergoth/pnut-mnm/issues) or `@Maergoth` on Discord. Information you send or post is subject to the receiving service's practices and may be used to investigate your report.

GitHub issues are public. Logs, diagnostic ZIPs, screenshots, and timer definitions can reveal your information and other players' messages or names. Review and redact them before posting; do not include passwords, account details, or private conversations. Posting publicly can make copies available beyond the project's control.

See the [Terms and Disclaimer](terms.md) for licensing and use information. This policy describes the current PNUT features; future changes should be reflected in a revised policy and effective date.

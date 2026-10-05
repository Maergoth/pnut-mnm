# Development guide

[User guide](../README.md) · [File audit](FILE_AUDIT.md)

### Building and publishing Windows releases

Run `build_exe.ps1 -Clean` for a clean dependency analysis. The spec limits native DLL
searches to the selected Python environment and Windows; third-party tools in the
caller's PATH must never supply DLLs to the app. Version 0.1.0 accidentally collected a
Poppler ICU library whose exports were incompatible with the Windows ICU expected by Qt.

The build script must pass the produced executable's `--smoke-test --report PATH` check.
It creates temporary offscreen windows and settings without game capture. A successful
PyInstaller build or a process that remains alive is not sufficient validation: a crash
dialog can keep a failed process alive too.

Then run `.venv\Scripts\python.exe scripts/package_release.py`. It creates the versioned
ZIP under `dist/releases/`, extracts it into a temporary directory, and runs that extracted
EXE with a minimal Windows PATH. Publish only the ZIP and its `.sha256` file after this
check passes. Machine-specific install metadata and smoke reports are not release assets.

Installed copies are updated through PNUT's **Update** feature. Development and release
tasks must not replace binaries or change files in a user's installed App folder; publish
the tested release to GitHub so the app can download it.


### Repository map

```
mnmparse/            library + CLI
  capture.py         FrameSource: WgcWindowSource (windows-capture) / MssRegionSource; read-only lookup of the game's own window
  ocr.py             Windows.Media.Ocr wrapper (sync), RapidOCR fallback, preprocess() (max-channel, scale)
  tracker.py         scrolling-text de-duplication, occlusion guard, wrapped-line joining -> Message; saved state
  maps.py            community wiki map discovery, aliases and disk cache
  trigger_presets.py bundled starter triggers, seeded without overwriting user edits
  grammar.py         Event dataclass, KINDS, the ordered regex RULES for every message shape
  parser.py          OCR-noise repair (normalize_ocr) + parse_line(text) -> Event
  stats.py           Encounter segmentation, per-actor table, OCR-noise name merge (canonical_names)
  interrupts.py      crowd-control registry (categories), cc.json loader, rank/tier matching
  session.py         SessionStats: loot, coin, crafting, party kills and deaths, CC totals, rates
  vocab.py           learned spellings: conflate OCR variants of names, items, zones, abilities
  replay.py          find replayed blocks in old logs, and a restart's repeated opening lines (find_backlog)
  importer.py        re-parse a .log / .jsonl (or several in a row: import_files) into encounters + a session
  swing.py           auto-attack delay from your swings (one bar per weapon type)
  triggers.py        Trigger, match_trigger (fuzzy word matching), TriggerStore (triggers.json), TimerBoard
  trigger_exchange.py validated JSON sharing, legacy timer conversion and duplicate-safe merging
  trigger_chat.py    compact single-trigger chat encoding, checksums and bounded fragment assembly
  export.py          one-line clipboard summaries of a fight: presets, templates, format_snapshot
  party.py           PartyRoster: the viewer's party from the chat and shared fights, manual in/out choices, party.json
  logwriter.py       logs/combat_*.log (EQ style) and logs/events_*.jsonl, each starting with a format header
  config.py          Config dataclass, config.json load/save, PROJECT_ROOT (exe-aware)
  cli.py             calibrate / snapshot / run / parse / replay
  app/               PySide6 desktop app
    main.py          App bootstrap, MainWindow (nav rail, top bar, pages), tray wiring, --selftest-seconds
    engine.py        Engine(QObject): worker thread running the pipeline, Qt signals
    models.py        ActorRow / EncounterSnapshot / build_snapshot (CC crediting lives here)
    docked_panel.py  DockedPanel: base for windows that dock under the overlay (drag off, snap back)
    map_overlay.py  resizable/fullscreen map viewer, asynchronous requests and zone switching
    map_downloads.py  background map updates for Settings and the optional startup download
    app_updates.py  verified GitHub downloads and deferred Windows application replacement
    attack_bar.py    AttackBar: the swing timer window (docked under the timer panel or the overlay)
    timer_panel.py   TimerPanel: trigger countdowns with radial rings, docked under the overlay
    triggers_page.py TriggersPage: trigger list and editor, audio settings
    trigger_share_dialog.py single-message chat copy and explicit received-timer review
    triggers_runtime.py  TriggerRunner (match lines, fire, run timers) and AudioOut (sounds, files, speech)
    overlay.py       OverlayWindow (translucent, topmost; lock = no move/resize, optional click-through)
    widgets.py       MeterTable (model/proxy/delegate, cell tooltips), FeedView, ToggleSwitch, StatusChip
    session_view.py  SessionView (compact for the overlay, full for the Session page)
    pages.py         LivePage (encounter list with zone tabs + meter), SessionPage, FeedPage, SettingsPage, AboutPage
    crop_picker.py   CropPicker (frame preview, draggable rectangle, Test OCR)
    theme.py         palette tokens, QSS, actor_color(), KIND_COLORS, fonts
    tray.py, icon.py
tests/               unittest suite; tests/fixtures/burst_ocr.json is an anonymized OCR recording with
                     other players' names replaced by made-up ones (misreadings kept as misreadings)
cc.json              crowd-control ability table (editable); config.json / config.example.json
requirements.txt     the Python packages (pinned)
PNUT M&M.cmd, make_shortcut.ps1, build_exe.ps1, mnmparser.spec   launch and packaging
assets/              generated icon (UI images and trigger sounds are generated at run time)
```

Run the tests with `.venv\Scripts\python -m unittest discover -s tests -v`. The Qt tests run on
the `offscreen` platform with `QT_QPA_FONTDIR` pointed at `C:\Windows\Fonts`, so text widths are
real; `tests/test_layout.py` builds the main window and the overlay at their minimum and at a
wide size and fails if any button or single-line label is narrower than its text, any widget is
squeezed below its minimum, or any widget spills out of its parent. Layout rules that keep it
green: buttons and labels never get fixed widths smaller than their text; rows of a variable
number of items use `widgets.FlowLayout`; long single-line text uses `widgets.ElidedLabel` (full
text in the tooltip); meter columns carry a `drop` priority; the main window's minimum size is
re-read from its content on every layout request (`MainWindow._sync_minimum_size`).

### Pipeline, step by step

1. **Capture.** `capture.WgcWindowSource` finds the game window through a read-only
   EnumWindows pass (`find_game_window_info`): the exact title in any case and a class in
   `GAME_WINDOW_CLASSES` (`UnityWndClass`); browser and Electron classes
   (`FOREIGN_WINDOW_CLASSES`) are always turned down, and every rejected look-alike is logged
   once with its class and pid. It hands `windows-capture` only that hwnd (the library's own
   name lookup is a substring match) and runs it on a daemon thread; the newest frame
   is kept as a BGR copy under a lock. Every `WINDOW_CHECK_S` (2 s) `window_switch_reason`
   checks that the window still exists and is still the best match; if not, the session ends
   and the engine's existing window-lost path looks the window up again. Per-window capture sees the window even when other
   windows cover it, and never includes our overlay. The `mss` backend is a desktop grab and
   sees whatever is on top; it exists as a fallback only.
2. **Crop and preprocess.** `Config.crop` is in game-window pixels (physical, 3840x2160 here).
   `ocr.preprocess` takes the brightest of B/G/R per pixel; colored combat text (red deaths,
   green buffs) keeps full brightness that way, which fixed the misreads grayscale produced.
   Upscaling gained nothing on this font; keep scale 1.0 unless the font is tiny.
3. **OCR.** Windows.Media.Ocr through the `winrt` projection, about 20 to 30 ms per frame.
   `ocr.OcrLine` carries x, y, height and text per visual line. The engine keeps one persistent
   asyncio loop per thread because recognize_async is a WinRT coroutine.
4. **Tracker** (`tracker.py`, the hard part). Each frame is a list of visual rows. Rows are
   aligned to the previous frame by a vertical offset voted by fuzzy text matches, so the
   tracker knows which rows are new. Each logical row collects text variants over the frames
   it stays visible with a quality tier (the clipped top row never votes; off-margin or
   fragmented reads vote last). A row is emitted after `min_frames` agreeing good readings, or
   with its best text when it scrolls off. The occlusion guard skips frames where most rows are
   short fragments or foreign text sits on the rows (the character sheet case). Wrapped-line
   joining holds an unterminated full-width row and appends following rows while they look like
   continuations (`_is_continuation`: the head ends with a connector such as "of", "for 57",
   "but", "and you receive", a possessive or a damage type, or the tail starts in lower case without being a new
   article-led NPC sentence), up to four rows; a "(Block 6)" marker row is folded back into its
   hit line. Everything else is emitted as a `fragment`.
   Two later additions (2026-10-02). *Consensus*: when the clean readings of a row disagree, it
   waits until one variant was read identically twice (at most `CONSENSUS_MAX_FRAMES`).
   *Replay detection*: every emitted visual row gets a sequence number and the last
   `HISTORY_ROWS` (400) are kept. After a jump the frame is aligned with that history
   (`_history_positions`, offset voting like the frame alignment); a burst of four or more new
   rows in one frame is checked against it as a block (`_check_replayed_block`); and a new
   bottom row under an emitted row whose successors are no longer in the window is compared
   with the rows that followed it originally (`_chain_replay`). Matching is digit-aware:
   texts whose numbers differ never match ("for 68 points" / "for 72 points"), otherwise a
   scrolled-back window aligns as if it scrolled forward. A lone `I`/`l`/`O` counts as a digit
   and a reading without digits (garbled) still matches.
   Since the 2026-10-03 audit: the block check looks only at new rows at the bottom, needs
   an old row directly above them (`_replay_anchor`, shared with `_chain_replay`) and the
   history to go on right after that row, and suppresses only the rows that match their own
   history row (identical group-heal blocks used to be dropped); new rows above known ones
   (faded top lines coming back) go through `_check_replayed_gap`. After a jump, rows seen in
   one frame only are held; if the next frame lines up with the window from before the jump,
   the jump frames were a glitch and are dropped, held rows that show up again merge into
   their window row, and the rest are emitted once a frame aligns (or after
   `JUMP_HOLD_MAX_FRAMES`). Rows revealed after a jump, a scroll-back or an occlusion get times
   spread over the gap (`Message.estimated_ts`). `Tracker.scrolled_back` is true while the
   window is jumped, occluded or shows an old bottom row; state changes are logged at INFO,
   at most once per 10 s per kind. `export_state` / `import_state` save and restore the
   history, sequence counter, last frame and geometry, so a restarted app re-synchronises
   instead of emitting the visible chat; without a state, rows of the first frame are flagged
   `Message.backlog`.
5. **Parser.** `normalize_ocr` repairs noise first (word table, digit look-alikes inside
   amounts, dropped apostrophes, `2()` for 20, stray glyphs), then the ordered `grammar.RULES`
   are tried top to bottom and the first match builds the `Event`. Rule order matters: markers,
   kills, experience, loot/coin/craft, personal, CC results, cannot-attack, heals, resists,
   fizzles, ability misses, ability hits, melee misses, melee hits, casts, interrupts, specific
   status lines, generic "Actor verbs Target.", and finally the status catch-alls. Two
   invariants that bit us: the possessive in `X's Ability hits ...` must be mandatory (optional,
   it split multi-word NPC names), and NPC names stop at function words so "with their bow" and
   "but they absorb" never become part of a name. A line is completed as cut off only when it
   ends in `for <n>`, `for <n> points` or `for <n> points of` (`_WHOLE_LINE_ENDINGS`).
   `NameCompleter.observe` (the engine and the importer call it on every event before it is
   written or counted) also repairs the event in place (`NameCompleter.repair`): a multi-word
   melee actor whose last words are an ability already seen becomes that actor's ability hit
   (a dropped apostrophe), a trailing `l`/`I` on a kill name or resist skill goes when the
   name without it was seen before, and an archer's attempt right after "IMMUNE to" is not an
   attempt.
6. **Stats** segments encounters (heals do not open one; an encounter without a damage event is
   dropped at close; a zone line closes the open one; only the timeout ends a fight, kills
   are recorded against its targets, matched fuzzily) and builds per-actor tables. A hit or
   swing between two NPC names (not the viewer's pet) never opens a fight or moves
   `last_activity`; it joins an open fight as context only. `Stats.roster` (`party.PartyRoster`,
   given `player_name` and a vocabulary-based canonicaliser) collects party members from the
   party lines, group heals and, through `note_fight` at every close, players who shared
   `SHARED_FIGHTS` (3) fights with the viewer since the last join, disband or kick, and
   `EncounterSnapshot.ours` tells the group's fights from other groups'; `canonical_names` merges OCR
   variants of a name into the frequent one (glyph folding for p/D, o/0, l/I, y/v and the
   "ffi" ligature read as "m", plus whole-word prefixes such as "a skeletal" into "a skeletal
   marksman"), never two names `Vocabulary.distinct` knows apart, and picks the closest
   spelling when several frequent names qualify. Zone lines are kept
   as `zone_changes`; a zone line closes the open encounter and every snapshot carries the
   zone it started in (`zone_at`), which the Live page groups by. The engine carries the last
   zone across Stop/Start.
7. **Models** (`app/models.py`) build the display snapshot per encounter, including the CC
   score: every use of an ability in `cc.json` is an attempt; result lines credit the most
   recent attempt within `CREDIT_WINDOW_S` with a matching category, preferring the attempt
   aimed at that victim or whose spell matches the result's "by scintillating lights" phrase.
   Sides are classified on the accumulators before the rows are built, so heals that land on
   an enemy move to `ActorRow.enemy_heals` (`_drop_enemy_heals`); the label is the fight's
   enemies minus the group and outsiders (`_fight_label`, "unknown" when none is left).
   `duration` is the group's own span (`_group_span`: first to last swing or hit by an
   in-group actor, ticking to now while open) and `active_duration` is that span without the
   ticking; `export.format_snapshot` takes `{duration}`, `{dps}` and `{hps}` from it, and
   `merge_snapshots` adds it up.
8. **Session** (`session.py`) accumulates loot, coin, crafting, kills, deaths and CC over the
   run; the engine emits it at most twice a second and at least every five seconds. Slain
   lines are stored raw and classified when the snapshot is built (`_classify_slain`), with
   the party known by then, so a member identified after their death still counts. With a
   roster (`SessionStats(roster=...)`, `set_roster`) that knows anyone, the roster decides the
   party for kills, deaths and crafts; other players' crafts go to `outsider_crafts`. A
   `coin_split` joins the latest coin loot without a split within `COIN_SPLIT_JOIN_S` (5 s) if
   it is not larger; `reward` goes to `rewards`; `coin_received` (splits plus your own loots
   without one) is the "Mine only" coin figure.
8a. **Fused lines and garbled numbers** (`parser.py`). `split_fused` cuts a line whose parse
   swallowed a second message (`looks_fused`: an ability name containing "hits"/"for N",
   a combatant name with a verb in it, a damage type of several words) at the first place
   where `grammar.MESSAGE_START_RX` says a new message begins and the part before it parses
   (or completes, `_complete_truncated`, when it lost its end after the number); the engine
   and the importer run it before parsing, and the tracker's `_is_continuation` uses the same
   regex so it no longer joins a new message onto a line that lost its end. `_garbled_skill`
   leaves a line unread when its ability name has two lower-case non-function words.
   `parse_garbled_amount` reads a hit or heal whose number is unreadable (amount `None`,
   `estimated=True`); with Dummy Fix on, `Stats.estimate_amount` fills in the zone average
   (`Stats._amounts`: sums per zone visit and per session, keyed by attacker + attack, then
   attacker, then attack, then all).
8b. **Vocabulary** (`vocab.py`) counts every name per category (player, npc, item, zone,
   skill) and every chat word; `Vocabulary.canonical` maps a spelling to a better-attested one
   when `close_spellings` holds (same words, one or two edits inside a word, words of three
   letters or less exact, a clipped first word allowed) and the evidence is lopsided (twice
   as frequent, or the differing words four times as common in the chat). After a matching
   article the next word may have lost leading letters ("a eletal warrior"), as long as four
   letters remain. Player and NPC names are learned only from the kinds in `vocab.NAME_KINDS`
   and only when `could_be_name` accepts them (the grammar's name shape, no "you" word, not a
   sentence-start word such as "The"); `load` drops saved entries that fail it.
   `Vocabulary.distinct(category, a, b)` is true when both are well-attested (`min_count`
   readings), different, not merely unproven close spellings and neither a cut-off reading;
   `stats.name_similar` uses it as a veto. Stats, Session and
   the snapshots use the app-wide `vocab.GLOBAL`; the engine loads and saves it as
   `logs\vocabulary.json` and seeds it from the recorded logs on the first run. The importer
   uses a fresh one unless given one (the Live page passes the global one) and re-parses both
   file formats from their text. `replay.find_replays` runs only on files without a format
   header that started before `REPLAY_AWARE_SINCE` (2026-10-02 16:44), and drops only runs of
   `MIN_RUN` (6) lines that repeat an original within 40 lines or 60 s, numbers word by word,
   squeezed into a second while the original took at least two more, with kill, death, loot
   and coin lines matching exactly. `import_files` imports several files in time order with
   one vocabulary and one roster (started over after a break longer than the roster's 6 hours)
   and drops a restart's backlog with `replay.find_backlog` when a file starts within 10
   minutes of the previous one's end.
9. **Engine** (`app/engine.py`) runs 1 to 8 on a Python thread and only emits Qt signals;
   receivers live in the GUI thread. Stop flushes the tracker, closes the open encounter, closes
   the writers and stops the source, in that order. The tracker is kept over a Stop/Start
   within `STATE_MAX_AGE_S` (10 minutes) when the crop and OCR settings are unchanged, and
   otherwise restored from `logs\tracker_state.json` (saved atomically on stop and every
   `STATE_SAVE_MESSAGES` messages); `msg.backlog` rows are neither logged nor counted. As a
   backstop, when the newest `combat_*.log` ended under 10 minutes ago, the first frame's lines
   are held until the chat scrolls (`RESTART_HOLD_S` at most) and those repeating that log's
   last lines are dropped. The zone is saved in `logs\session_state.json`. When the roster
   gains a member, recent fights of the zone visit (`REBUILD_WINDOW_S`, at most
   `REBUILD_MAX_FIGHTS`) are rebuilt and re-sent on `encounter_updated` (never to auto-copy);
   a row is replaced only if its group grew. While `Tracker.scrolled_back` has lasted
   `SCROLLED_BACK_SHOW_S`, the status carries `scrolled_back` and the encounter timeout waits
   (`SCROLLED_BACK_HOLD_MAX_S` at most); a crop of fewer than `MIN_CROP_ROWS` rows is reported
   once on `notice`. Only `unknown` lines, fragments and Dummy Fix estimates count as
   unreadable, and session time in combat counts only fights where `snap.ours`.

### Message grammar

`grammar.py` holds every line shape observed, each rule with a verbatim example, and
`tests/test_parser.py` checks them.
Short version of the families (names: players are capitalized words, NPCs are article plus
lowercase words, "You/Your/YOU" is the viewer):

- Melee: `X crushes Y [with their bow] for N points of damage[ (2 absorbed)][ (Block 6)].`,
  `You try to crush Y, but miss!`, `X tries to bite YOU, but YOU dodge!` (dodge, parry,
  block, riposte), `X slashes Y but they absorb the attack.`
- Abilities: `X's Holy Strike hits Y for N points of Holy Damage.`, `X's Barbed Arrow bleeds Y
  for N points of Bleed Damage.`, `X's ability misses.`, `X tries to cast S on Y, but is
  resisted!`, `Y resists your S!`, `X's spell fizzles.`
- Heals and casts: `X's Heal heals Y|you|them for N Health.`, `X begins casting S.`,
  `X's casting is interrupted.`
- Kills: `You have slain Y!`, `Your party member X has slain Y!`, `Y has been slain by X!`
- Crowd control results: `Y is stunned[ by scintillating lights].`, `Y is mesmerized.`,
  `Y is rooted to the ground.`, `Y is immobilized.`, `Y is blinded.`, `Y is pinned.`,
  `You are bound by a net shot.`, `Y adheres to the ground.` / `Y comes unstuck.`,
  `You are slowed by a snaring shot.`, `Y is no longer stunned.`; debuffs
  `Y's movements begin to slow.`, `Y's heart begins beating irregularly.`,
  `Y reels as vigor flows from their body.`, `Y is chilled to the bone.`; and
  `YOU are temporarily IMMUNE to X's Snaring Shot!` (a status line: the attempt landed nothing).
- Loot: `--X loots [Item] from Y's corpse.--`, `X loots N copper coins from Y's corpse, and
  you receive M copper coins from Y's corpse as your split.`, `X crafts Item(3).`; the wrapped
  second half of a coin line, `M copper coins from Y's corpse as your split.`, is `coin_split`.
- Everyday messages: `You receive Item from <NPC>.` (`reward`, a quest hand-in), `You sell
  Item (x4) for 4 copper coins.` / buy / train (`vendor`), `<NPC> says, "..."` (`chat`),
  `X has leveled up! They are now level 7!` (`level_up`), /con text such as `Y seems
  indifferent to your presence` (`consider`).
- Personal (gated): `Your skill in S has increased! (12)`, `Your faction standing with F got
  better.`, `You gain party experience!`, `X opts into higher level PvP.`, `--You loot [Item]
  from your corpse.--`, camping, harvesting, invites, tells and /who.

To add a shape: put a verbatim example in `tests/test_parser.py`, add a rule in `grammar.py` at the right
position, extend `Event` only if a new field is truly needed, add the line to
`tests/test_parser.py`, and if it is a status line check that the catch-all did not already
classify it. A new kind also needs a colour in `theme.KIND_COLORS` and a Feed group in
`pages.FEED_GROUPS` (a test checks the colour), and if its actor and target are real
combatants, an entry in `vocab.NAME_KINDS` so their names are learned. Unknown lines are written
to the log verbatim, so they can be mined later with a one-off script over `logs/combat_*.log`.

### Data files and settings

- `config.json` (next to the package, or next to the exe when frozen): see `config.example.json`
  and `Config` for every key; `Config.problems()` validates.
- `cc.json`: crowd-control table by category; ranks (`Shield Bash II`) and tiers (`Lesser
  Gust of Wind`) match their base name. Compiled from the wiki for all 18 classes.
- `logs/party.json`: the party roster (each member kept for 6 hours after they were last seen,
  inferred members and shared-fight counts included; format version 2, older files still load)
  and your "Count X as my group" choices (kept).
- `logs/tracker_state.json`: the tracker's recent rows, sequence counter, last frame and
  geometry (plus the crop and OCR settings they belong to), saved on stop and every 50
  messages; used by a restart within 10 minutes. `logs/session_state.json`: the current zone,
  for the same restart. Both are private like everything in `logs/` (git-ignored).
- `triggers.json` (next to `config.json`): the triggers and the trigger audio settings
  (volume, voice, rate, output device). Built-in sounds are generated into `assets/sounds/`.
- QSettings `mnmparse/MnM Parser` (registry, retained for upgrade compatibility): window geometry, overlay geometry/opacity/tab/
  sort/lock, current page, overlay visibility.
- `logs/`: `combat_YYYY-MM-DD_HHMMSS.log` (EQ-style, one message per line) paired with
  `events_YYYY-MM-DD_HHMMSS.jsonl` (one Event per line); the stamp is the first message of
  the segment. A new pair starts at every capture start, after `log_break_minutes` (60) without
  messages, or when the raw log reaches `log_max_mb` (5). A new file starts with a format
  header (`# mnmparse combat log, format 2` in the raw log, `{"mnmparse": "events", "format":
  2}` in the events file) that every reader skips (`logwriter.is_header`); a file without it
  is format 1. `app.log` is the rotating app log,
  `exports/` holds CSV/JSON. `mnmparse.importer` reads both file types (and the older
  one-per-day `combat_YYYY-MM-DD.log`).

### Fixtures and how to grow them

`tests/fixtures/burst_ocr.json` is anonymized per-frame OCR output originally recorded from a
screen (a 99-frame fight). The tracker tests replay it and assert on exact joined messages.
Longer recordings are kept locally (`tests/fixtures/burst2_ocr.json` and up are git-ignored),
because their misreadings of other players' names cannot be scrubbed reliably. To record a new one, grab frames
of the chat crop at 4 to 6 fps with `windows-capture`, OCR them with `ocr.WindowsOcr` and the
max-channel preprocess, and store `{"t_ms", "file", "lines": [{x, y, h, text}]}` per frame
(see the note field of an existing fixture). When the game's wording changes, the fixtures tell
you exactly which rule broke.

### Things that were tried and rejected

- Reading network traffic: the game's user agreement forbids intercepting its protocol, and the
  project's safety rule keeps everything outside the game process.
- Memory reading or any in-process hook: excluded by the project's safety rule.
- Grayscale preprocessing and 2x upscaling: worse or no better than max-channel at 1x.
- A plain "never emit duplicate text" rule in the tracker: legitimately repeated hits must each
  be logged, so de-duplication is by position, not by text.
- Repairing a garbled "begins casting" line by fuzzy matching: most such lines get a clean
  re-read within a second, often after the garbled one, so the repair would count the cast
  twice. The generic status rule only refuses to take "casting" as a verb.
- Counting kicks separately from other interrupts, or showing CC attempts as a column: a kick
  is an interrupt, so there is one CC score, with attempts and the per-type breakdown in the
  hover tooltip and the details panel.

### Safety checklist for changes

Before shipping a change, grep `mnmparse/` for OpenProcess, ReadProcessMemory, SendInput,
keybd_event, mouse_event, PostMessage, SendMessage, PrintWindow, SetForegroundWindow,
SetWindowsHookEx, RegisterHotKey, SetWindowPos, MoveWindow, pcap, npcap, WinDivert and
SOCK_RAW; none may appear. The overlay keeps its flag set (frameless, stays on top, tool, does
not accept focus, transparent for input when locked) and never calls activateWindow or raise_
after the first show. The CLI and the app write only under the project folder (or the exe
folder) and the QSettings key above.

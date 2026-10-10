# PNUT v1 release assessment

Updated October 10, 2026 for **1.0.2**. Carebear now limits individual performance
comparisons while keeping ordinary tools usable. Zone recovery also recognizes complete
local `/who` summaries for known zones and restores current-zone navigation.

The original assessment reviewed 0.9.1 at commit
`7c88e69d3168e63208ab12fdf60d0747a62a435b`. Its lifecycle, regex, persistence and release-gate
findings drove the v1 work. Current evidence combines source review, synthetic fixtures,
offscreen UI checks, the user's local trial and disposable updater tests. Gameplay accuracy
and long-session resource limits require separate human trials.

## Implemented features

| Area | Current behavior | Evidence / limits |
| --- | --- | --- |
| Capture lifecycle | Nonblocking Stop retains worker ownership; apply/restart and quit wait for completion. A stopping worker prevents a second capture worker. | [Engine](../mnmparse/app/engine.py), [Capture](../mnmparse/capture.py), synthetic stalled-worker regressions. Live recovery remains a gameplay check. |
| Triggers | Regex deadlines, matching budgets, input limits and quarantine cover live matching and previews. Invalid stores preserve existing definitions; atomic saves and explicit flush cover immediate quit. Names, patterns, captured values, labels, speech, custom sounds and sharing work in either mode. | [Triggers](../mnmparse/triggers.py), [Runtime](../mnmparse/app/triggers_runtime.py), authored-settings and trigger regressions. Imported timers require review before use. |
| Carebear performance views | Fresh and unconfirmed legacy preferences start in Carebear. Own values and eligible group averages replace individual groupmate performance across meters, Session, history, overlays and performance exports. | [Projection policy](../mnmparse/privacy.py), Carebear/export regressions. Feed text, trigger data and ordinary tools remain available. |
| Setup and help | Character/mode, crop/OCR, then Revenge List, Display map, Auto attack bar and Copy to clipboard after fight. Successful OCR enables Continue. Back preserves choices; Cancel preserves current preferences. | [Setup](../mnmparse/app/setup_wizard.py). Local frame/OCR remain visible in both modes; setup clears its frame on close, including late worker results. Help includes a labeled demo that cannot export fictional data. |
| Profiles and backups | Named profiles preserve character/source/crop/OCR/rate/log settings. Both modes can back up full preferences, triggers, corrections, spellings and Revenge entries. Logs/images are excluded. Restore validates contents, rolls back failed writes, retains the local data root and returns to Carebear. | [Profiles](../mnmparse/profiles.py), [Backup](../mnmparse/backup.py), preference regressions. Apply/restore requires stopped capture. |
| Diagnostics | OCR Diagnosis retains settings, timing, captured messages and available crop images in either mode. Saving a report does not upload it. | [Diagnostics](../mnmparse/diagnostics.py), synthetic OCR report regressions. Uncaptured lines cannot be recovered. |
| History and responsiveness | Cached projections avoid repeated idle aggregation. Imports run off the GUI thread with progress/cancellation. SQLite archives preserve correction evidence; older fights page in and full exports rebuild off-thread. | [Models](../mnmparse/app/models.py), [Session](../mnmparse/session.py), [Archive](../mnmparse/session_archive.py). Interrupted imports are discarded; archive retention does not prune raw logs. |
| Maps and zone recovery | Valid remote hashes reuse unchanged cached maps; downloads are bounded and images decode off-thread. Known-zone entry lines and complete local `/who` summaries identify the current zone; restored state updates map/meters. | [Maps](../mnmparse/maps.py), [Engine](../mnmparse/app/engine.py), zone-navigation regressions. Incomplete, unknown-zone and game-wide summaries are ignored; `/who` does not end an active fight. Manual selection remains available. |
| Coordination and executable cleanup | Installation/profile guards activate existing instances. Atomic writes use unique temporary files. Startup and post-exit helpers remove verified inactive executable copies; launch coordination preserves running copies and unknown files. | [Launch identity](../mnmparse/app/launch_identity.py), [Storage](../mnmparse/storage.py), disposable helper tests. Saved user data is preserved. |
| Update and distribution | Initialization acknowledges matching token/version; failed update boot can restore a validated previous application. Version/commit/dirty provenance must match frozen and extracted smoke reports. Packaging verifies ZIPs before replacing verified artifacts. | [Updater](../mnmparse/app/app_updates.py), [Build](../build_exe.ps1), [Windows workflow](../.github/workflows/windows-release-gates.yml). Published assets require a clean committed build. |

## Mode and Revenge List behavior

**Morality Adjustment** sits below Map updates. The flower/flame top-bar label opens it.
The red lever uses a sad emoji and requires a drag across most of its travel to request
Elitist mode. Individual performance remains filtered until the pledge and confirmation are
accepted. Cancel, Escape and closing keep Carebear. Returning to Carebear immediately removes
individual performance details; normal triggers and other tools keep working. Mute and
reduced animations apply to the switch cue.

Own pets are folded into your performance; Carebear provides self-only pet assignment and
**My pets** controls to clear your links. A known group needs at least three eligible members
for group averages. Damage/DPS additionally requires three active contributors with positive
damage at least equal to their healing; Healing/HPS requires three with positive healing at
least equal to their damage. Positive ties count in both; inactive members count in neither.
Smaller role cohorts show **—** and omit that average from exports. Other non-combat averages
retain their existing group rules. Your damage percentage remains your actual share of the
group total; the group row shows the remaining percentage independently of its average.
Unknown self identity hides performance details until configured. Rounding and cohort rules
reduce simple inference but do not guarantee mathematical anonymity. Performance clipboard,
CSV/JSON/text, automatic output and custom templates use the same projection.

**Revenge List** is off by default and works in either mode when enabled. Disabling it hides
its panel, overlay tab and pop-out and closes pending Add dialogs. Automatic detection needs
known self identity and offers a right-hand **+** once per attacker per session for **30
seconds**. It accepts ASCII-letter names without spaces/symbols and excludes articles, self
and second-person aliases. Named NPCs may still qualify and can be ignored. Manual Add accepts
arbitrary nonempty names or labels; Add/Remove and Pop out remain usable across mode changes.

The main list, overlay and single passive pop-out share newest-first activity ordering.
Default filters are **30 days / 100 entries**; **All** removes each display limit without
deleting saved entries. Names and addition/latest-activity dates persist; chat and damage
are not stored in the list. Legacy undated entries remain visible until activity supplies a
date. Repeated attacks update saved activity through coalesced writes, flushed on quit and
full backup. A new session resets prompt eligibility without deleting saved names.

## Verification and remaining trials

Focused offscreen regressions cover authored trigger settings, original captured matches,
sharing/sender details, Revenge availability across modes, local frame/OCR access, setup
cleanup and disposable startup. Performance projection tests cover role cohorts, individual
row filtering, actual own damage shares and exports. Zone fixtures cover restored state,
initial OCR/backlog evidence and current local `/who` corrections without replaying old
combat or triggers. Release gates additionally require clean frozen/extracted startup and
matching provenance; a source UI check alone does not establish frozen gameplay behavior.

Local automated UI work uses **Qt offscreen**, never live capture or map downloads.
Native probes require explicit `PNUT_NATIVE_UI_TESTS=1` and isolated cleanup. The 1.0.2 build
path is `dist/publish/v1.0.2/PNUT M&M/PNUT M&M.exe`; the portable archive is
`releases/PNUT-MnM-1.0.2-windows.zip`. Published files are linked from
[GitHub Releases](https://github.com/Maergoth/pnut-mnm/releases). Use the
[local checklist](LOCAL_V1_TEST.md) for the actual build under test.

Remaining human/environment trials:

1. A fresh supported Windows installation without development Python/Qt; frozen update and failed-boot recovery; audio/speech devices.
2. Real crop/OCR accuracy, lost-game recovery, slow Stop/Start, rapid/wrapped chat, scroll/occlusion, group/pet changes, named NPCs and healing/CC/loot totals.
3. Lever/pledge cancellation, mute/reduced motion, individual performance protection in every export, usable triggers/tools through mode changes and Revenge default-off behavior.
4. Long sessions/large imports, full-size maps, network/disk failures, DPI/mixed monitors, tray/overlay restoration and assistive technology. Establish latency/memory budgets on representative machines; an eight-hour soak remains proposed.

Pipeline timings have bounded p50/p95 histories, but no process working-set metric yet.
Large maps still allocate full-resolution image surfaces; preview levels or tiles need a
separate design if measured memory warrants them. Captured raw logs remain outside archive
retention, and unseen lost lines cannot be proven complete from the resulting log.

## Original performance evidence and its limits

These measurements were taken before the v1 cache/archive changes and remain baseline evidence only. They are warm local synthetic operations, not current end-to-end gameplay latency claims. Encounter fixtures used two actors without complex vocabulary; session fixtures repeated loot entries. Native image memory, rendering, disk contention and real histories were not included.

| Isolated full rebuild | 1,000 entries | 10,000 entries | 50,000 entries |
| --- | ---: | ---: | ---: |
| Encounter snapshot | 1.57 ms | 14.99 ms | 73.62 ms |
| Session snapshot | 0.93 ms | 7.88 ms | 39.30 ms |

The original parser fixture processed 10,000 mixed lines in about 369 ms (roughly 27,000 lines/second). Its small OCR fixture took about 10.75 ms for Windows OCR and 3.84 ms for preprocessing. Those narrow results favored eliminating repeated aggregation and GUI work over a broad parser rewrite. The implemented caches now avoid repeated raw-history aggregation on idle updates, but no new cross-machine performance improvement is claimed here.

Ancient Crypt's 8400 × 9300 dimensions imply approximately **298 MiB for one decoded 32-bit surface**, before temporary/pixmap allocations; this is a dimension estimate, not measured resident memory. The original isolated 100,000 Event objects with distinct text allocated **33.27 MiB**; that excludes native images and the rest of PNUT. Worker decoding, bounded previews and durable archives address responsiveness/growth pathways but do not replace an endurance and working-set measurement.

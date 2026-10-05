"""Offline trigger walkthrough, with executable examples shared by the help tests."""
from __future__ import annotations

from html import escape


# The sample lines are illustrative inputs, not promises about spell durations.
# Keep each pattern, sample and expected label together so the examples can be tested.
HELP_EXAMPLES = (
    dict(anchor="example-invis", title="Invisibility warning without a countdown", name="Invis warning",
         mode="starts", pattern="You begin to feel yourself appearing",
         sample="You begin to feel yourself appearing.", label="Invisibility is breaking!",
         expected="Invisibility is breaking!", speech="Invisibility is breaking",
         note="Start a timer can stay off. Choose Speak text for a spoken warning, or Nothing for just the popup. "
              "Try Quiet for = 2 s if repeated lines should only alert once. This is a separate example; "
              "you can instead edit the bundled Invis Break trigger."),
    dict(anchor="example-gate", title="An enemy begins casting Gate", name="Gate warning",
         mode="contains", pattern="casting Gate",
         sample="a skeletal vicar begins casting Gate.", label="Gate cast detected!",
         expected="Gate cast detected!", speech="Gate cast detected",
         note="Choose Built-in sound and Alert, or Speak text. This broad pattern catches any matching line "
              "in your captured chat. It does not filter by your target or group."),
    dict(anchor="example-smite", title="Righteous Smite II: show damage from each hit", name="Smite II",
         mode="regex", pattern=r"Your Righteous Smite II hits .+? for (?P<damage>\d+) points of Holy Damage",
         sample="Your Righteous Smite II hits a skeletal knight for 154 points of Holy Damage.",
         label="Righteous Smite II: {damage} damage", expected="Righteous Smite II: 154 damage",
         speech="{damage} damage",
         note="Change II to III in Text and Label for your rank III trigger. Remove the space and II for "
              "the base spell. Keep Start a timer off for damage popups alone. Keep Quiet for at 0 to see "
              "every accepted hit. If you also run the next all-ranks example, disable overlapping old "
              "Smite triggers to avoid two notifications for the same hit."),
    dict(anchor="example-smite-all", title="One Smite trigger for all ranks and the target", name="All Smites",
         mode="regex",
         pattern=r"Your (?P<spell>Righteous Smite(?: [IVX]+)?) hits (?P<target>.+?) for (?P<damage>\d+) points of Holy Damage",
         sample="Your Righteous Smite III hits a skeletal vicar for 262 points of Holy Damage.",
         label="{spell}: {damage} on {target}",
         expected="Righteous Smite III: 262 on a skeletal vicar", speech="{spell}, {damage} damage",
         note="The optional Roman numeral covers the base spell and ranks such as II and III. "
              "The target capture stops at ' for '. You can shorten the label to '{damage} damage' "
              "if the panel is narrow. This shows one hit's number; it does not total several hits."),
    dict(anchor="example-caster", title="Name the caster and the spell", name="Spell warning",
         mode="regex", pattern=r"^(?P<caster>.+?) begins casting (?P<spell>Gate|Heal)\.?$",
         sample="a skeletal vicar begins casting Heal.", label="{caster}: {spell}",
         expected="a skeletal vicar: Heal", speech="{caster} casting {spell}",
         note="The bar between Gate and Heal means either word. Anchors require this complete line; "
              "paste what PNUT actually read into Test before using it. Choose Speak text to hear the captures."),
    dict(anchor="example-mez", title="Crowd control with the affected target in a timer", name="Mez",
         mode="regex", pattern=r"^(?P<target>.+?) is mesmerized\.?$",
         sample="a skeletal knight is mesmerized.", label="Mez: {target}",
         expected="Mez: a skeletal knight", speech="Mez on {target}",
         note="Turn Start a timer on and enter your spell's known duration; 24 seconds is only an example. "
              "Set Warn to 5 s before and Speak text with '{label} soon'. Set When it ends to Speak text "
              "with '{label} ended'. Add another timer keeps multiple targets, but also adds another row "
              "on repeats. Replace and Retain apply to the whole trigger, not separately to each target."),
    dict(anchor="example-buff", title="A fixed reminder after a buff message", name="Rebuff reminder",
         mode="contains", pattern="You feel protected",
         sample="You feel protected.", label="Rebuff reminder", expected="Rebuff reminder",
         speech="Protection refreshed",
         note="This is an illustrative buff line: replace Text with the real message from Feed. Enable "
              "a timer and set its length to the buff's duration. Replace restarts the countdown on a "
              "refresh. Warn 30 seconds before the end if that suits your duration. The timer is an "
              "estimate from the observed line, not a live read of the game's buff state."),
    dict(anchor="example-diagnostic", title="Show the whole line while learning a new trigger", name="Root debug",
         mode="contains", pattern="rooted", sample="a skeletal knight is rooted.",
         label="{line}", expected="a skeletal knight is rooted.", speech="{match}",
         note="Choose Nothing and leave Start a timer off. {line} helps you see exactly what fired. "
              "After confirming the wording, replace it with a shorter label or a named capture. "
              "A long popup is shortened to fit the panel; the original line remains in Recently fired."),
)

_MODE_NAMES = {"contains": "Contains", "starts": "Starts with", "exact": "Whole line", "regex": "Regular expression"}


def _code(value: str) -> str:
    return "<p class='code'><code>" + escape(value) + "</code></p>"


def _example_html(example: dict[str, str]) -> str:
    return (
        f"<a name='{example['anchor']}'></a><h3>{escape(example['title'])}</h3>"
        f"<p><b>Name:</b> {escape(example['name'])} &nbsp; <b>Match:</b> {_MODE_NAMES[example['mode']]}</p>"
        "<p><b>Text</b> (paste this into the matching field):</p>" + _code(example["pattern"])
        + "<p><b>Label:</b></p>" + _code(example["label"])
        + "<p><b>Sample chat line</b> (paste into Test):</p>" + _code(example["sample"])
        + "<p><b>Expected label:</b> " + escape(example["expected"]) + "</p>"
        + "<p><b>Optional Say text:</b> <code>" + escape(example["speech"]) + "</code></p>"
        + "<p>" + escape(example["note"]) + "</p><p><a href='#contents'>Back to contents</a></p>"
    )


TRIGGER_HELP_HTML = r"""
<html><body>
<a name="contents"></a><h1>Timer/trigger help</h1>
<p>Build an alert from a chat line, display useful numbers and names, and add a countdown
when you need one. This guide is included with PNUT and works offline. Select text to copy
an example. Close this guide or press Esc to return to your triggers.</p>
<h2>Table of contents</h2>
<ol>
<li><a href="#basics">How a trigger works</a></li>
<li><a href="#first">Build your first trigger, step by step</a></li>
<li><a href="#matching">Choose a matching mode</a></li>
<li><a href="#variables">Variables in labels and speech</a></li>
<li><a href="#regex">Capture names and numbers with regular expressions</a></li>
<li><a href="#examples">Copy-and-test recipes</a></li>
<li><a href="#timers">Countdowns, overlap rules and colors</a></li>
<li><a href="#audio">Sounds, speech and repeat suppression</a></li>
<li><a href="#testing">Test, preview and tune</a></li>
<li><a href="#sharing">Import, export and in-game sharing</a></li>
<li><a href="#organizing">Organize, save and restore triggers</a></li>
<li><a href="#troubleshooting">Troubleshooting and support</a></li>
</ol>
<p><b>Jump to a recipe:</b> <a href="#example-smite">Smite damage</a> ·
<a href="#example-smite-all">All Smite ranks</a> · <a href="#example-invis">Invisibility</a> ·
<a href="#example-gate">Gate warning</a> · <a href="#example-caster">Caster and spell</a> ·
<a href="#example-mez">Crowd-control timer</a> · <a href="#example-buff">Rebuff reminder</a> ·
<a href="#example-diagnostic">Whole-line popup</a></p>

<a name="basics"></a><h2>1. How a trigger works</h2>
<p>PNUT reads the chat area you selected. Each new chat line is checked against your enabled
triggers. A matching trigger can show a label, play a sound or speak, and optionally start
a timer. It only knows text that reaches that captured chat area; it does not read hidden
game information, select targets, or perform an action in game.</p>
<p>For triggers with <b>Start a timer off</b>, the label appears <b>below the active countdowns</b>
in the overlay's timer panel. It stays
for four seconds and fades over the last second. Up to four recent notifications can appear,
with the newest at the bottom. A trigger with Start a timer on shows only its countdown row,
without a separate fading notification.
The overlay must be visible to see its panel. The panel can be dragged when unlocked and
snapped back through its right-click menu.</p>
<p>Triggers match captured text independently of the combat meter's group filter. A broad
"casting Heal" trigger can fire for nearby people too. Match more specific wording when
you only want your own actions. Several triggers can match the same line.</p>

<a name="first"></a><h2>2. Build your first trigger, step by step</h2>
<ol>
<li>Make sure capture is running and the relevant game chat is inside its capture area.
Find the exact message in PNUT's <b>Feed</b>. Copy a complete message without a saved-log timestamp.</li>
<li>Open <b>Triggers</b> using the bell in the navigation rail. Choose <b>New</b> and give
the trigger a useful Name, such as "Invis warning". Make sure it is enabled.</li>
<li>Under <b>When the chat says</b>, enter <code>You begin to feel yourself appearing</code>
as Text and choose <b>Starts with</b>. This also allows punctuation at the end.</li>
<li>Under <b>Then</b>, set Label to <code>Invisibility is breaking!</code>. Choose
<b>Speak text</b> for Do and enter <code>Invisibility is breaking</code> in Say.
Choose <b>Nothing</b> instead if you only want the popup.</li>
<li>Leave <b>Start a timer</b> off for this first example. Set Quiet for to 2 seconds
if repeats in that interval should be ignored.</li>
<li>Paste <code>You begin to feel yourself appearing.</code> into <b>Test</b>.
The preview should show a match and the label you expect.</li>
<li>Choose <b>Fire this trigger now</b> to try its real popup and sound. Edits save
automatically. Return to the game to test against a newly arriving line.</li>
</ol>
<p>You already have a bundled Invis Break trigger. Edit that one or disable it when trying
this separate example if you do not want both to fire.</p>

<a name="matching"></a><h2>3. Choose a matching mode</h2>
<table cellspacing="6" cellpadding="5" border="1">
<tr><th>Mode</th><th>Use it for</th><th>Example</th></tr>
<tr><td>Contains</td><td>A word or phrase anywhere in the line</td><td><code>casting Gate</code></td></tr>
<tr><td>Starts with</td><td>A message that begins with a known phrase</td><td><code>Your Righteous Smite II</code></td></tr>
<tr><td>Whole line</td><td>A complete, fixed message</td><td><code>You are stunned</code></td></tr>
<tr><td>Regular expression</td><td>Variable names, amounts or alternatives</td><td><code>for (?P&lt;damage&gt;\d+) points</code></td></tr>
</table>
<p>Contains, Starts with and Whole line compare words: case, punctuation and extra spacing
are ignored. They are not arbitrary substring searches inside words. With <b>forgive OCR
typos</b>, longer words may differ by a small number of letters. Words of three letters or
fewer must match exactly. Turn forgiveness off when similar spell names cause unwanted matches.</p>
<p>Regular expressions are case-insensitive but otherwise use the actual line, including
spaces and punctuation. OCR forgiveness does not apply to regex patterns. Unless you add
<code>^</code> and <code>$</code>, a regex can match part of the line. Use <code>\s+</code>
instead of a literal space if the spacing varies.</p>

<a name="variables"></a><h2>4. Variables in labels and speech</h2>
<p>A variable is a name in braces, such as <code>{damage}</code>. PNUT replaces it with
text from that individual match. Labels and immediate Say text share these values:</p>
<table cellspacing="6" cellpadding="5" border="1">
<tr><th>Variable</th><th>Meaning</th></tr>
<tr><td><code>{name}</code></td><td>The trigger's Name</td></tr>
<tr><td><code>{line}</code></td><td>The complete chat line PNUT read</td></tr>
<tr><td><code>{match}</code></td><td>The portion matched by the pattern. Regex preserves the
original substring; ordinary matching returns normalized text, including lowercase.</td></tr>
<tr><td><code>{damage}</code>, <code>{target}</code>, etc.</td><td>A named regex capture you defined,
such as <code>(?P&lt;damage&gt;\d+)</code> or <code>(?P&lt;target&gt;.+?)</code></td></tr>
<tr><td><code>{1}</code>, <code>{2}</code>, etc.</td><td>Regex capture groups in their opening-parenthesis order;
named groups count too. Named variables are usually easier to maintain.</td></tr>
</table>
<p><b>There is no automatic damage variable.</b> First capture the digits in Text using a
regular expression, then put that capture's name in braces in Label. Keep the spelling and
capitalization identical. Matching only "Your Righteous Smite II" does not capture damage.</p>
<p>Leave Label blank to display the trigger name. The filled-in label appears on its
countdown when Start a timer is on, or in a fading notification when it is off.
A countdown keeps the values from the line that started it;
later Replace triggers can supply new values.</p>
<p><b>Warning and end speech have only <code>{label}</code> and <code>{name}</code>.</b>
For example, capture a target into the starting Label <code>Mez: {target}</code>, then set
warning speech to <code>{label} soon</code>. Do not put <code>{target}</code> directly in
warning speech: the original captures are not kept there separately.</p>
<p>Unknown variables remain written as braces rather than becoming empty. An optional group
that did not match also leaves its variable unresolved. Variables insert text only: they do
not calculate totals, evaluate expressions, format decimals, or set a timer's length.
Avoid naming captures <code>name</code>, <code>line</code> or <code>match</code> unless you
intend to replace those built-in values.</p>

<a name="regex"></a><h2>5. Capture names and numbers with regular expressions</h2>
<p>Start with a real line, then replace the parts that change with named captures.
For example: <code>Your Righteous Smite II hits a skeletal knight for 154 points of Holy Damage.</code></p>
<ol>
<li>Keep <code>Your Righteous Smite II hits </code> as literal text.</li>
<li>Use <code>.+?</code> for the enemy's changing name if you do not need to display it.</li>
<li>Keep <code> for </code>, then replace the number with <code>(?P&lt;damage&gt;\d+)</code>.</li>
<li>Keep <code> points of Holy Damage</code>. Choose Regular expression for Match.</li>
<li>Use <code>Righteous Smite II: {damage} damage</code> for Label and verify it with Test.</li>
</ol>
<table cellspacing="6" cellpadding="5" border="1">
<tr><th>Pattern piece</th><th>Meaning</th></tr>
<tr><td><code>\d+</code></td><td>One or more digits</td></tr>
<tr><td><code>[\d,]+</code></td><td>Digits and commas, if the actual amount includes commas</td></tr>
<tr><td><code>\s+</code></td><td>One or more whitespace characters</td></tr>
<tr><td><code>.+?</code></td><td>Some text, stopping as early as the following pattern allows</td></tr>
<tr><td><code>(?P&lt;target&gt;.+?)</code></td><td>Capture that text for <code>{target}</code></td></tr>
<tr><td><code>Gate|Heal</code></td><td>Gate or Heal; group it when embedded in a larger pattern</td></tr>
<tr><td><code>(?: II)?</code></td><td>An optional " II", without creating a capture</td></tr>
<tr><td><code>^</code> and <code>$</code></td><td>Start and end of the line</td></tr>
<tr><td><code>\.</code></td><td>A literal period; a bare dot means any character</td></tr>
</table>
<p>Paste patterns directly: use a single backslash, such as <code>\d+</code>. Do not paste
Python's <code>r"..."</code> wrapper or extra quotation marks. A saved log's timestamp is
not part of the live message. Test both a line that should match and a similar one that should
not. The examples below are starting points; spell durations and some message wording may
differ from what your game chat actually shows.</p>

<a name="examples"></a><h2>6. Copy-and-test recipes</h2>
""" + "".join(_example_html(example) for example in HELP_EXAMPLES) + r"""

<a name="timers"></a><h2>7. Countdowns, overlap rules and colors</h2>
<p>Enable <b>Start a timer</b>, then set Length in minutes and seconds. Use a duration you
have checked in game. Timer duration is fixed for that trigger and does not accept variables.
The timer begins when the message is processed, so missed lines or capture delays affect it.</p>
<table cellspacing="6" cellpadding="5" border="1">
<tr><th>If already running</th><th>What happens on a new match</th><th>Typical use</th></tr>
<tr><td>Replace</td><td>Replace that trigger's old countdown with a fresh one and its new label.
Obsolete queued timer speech is cancelled. The new match can play its immediate audio alert.</td><td>A refreshed buff or debuff</td></tr>
<tr><td>Retain</td><td>Keep the unexpired countdown and ignore the new match entirely,
including sound and speech.</td><td>A reminder that must finish before another starts</td></tr>
<tr><td>Add another timer</td><td>Keep existing countdowns and create another.</td><td>Several simultaneous effects</td></tr>
</table>
<p>Overlap rules identify the <b>trigger</b>, not its label or captured target. One generic Mez
trigger in Replace mode replaces its old target's timer when another target matches. Use
Add another timer for concurrent rows, or separate triggers with specific target patterns.
Quiet for is checked before the overlap rule; a match inside that interval is ignored.</p>
<p><b>Warn</b> is a number of seconds remaining, not elapsed time. Use a positive value less
than Length, then choose a sound, speech or Nothing. A Warn value of 0 disables that warning.
Choose a separate action for <b>When it ends</b>. For speech, use <code>{label}</code> and
<code>{name}</code>. An ended timer briefly flashes 0:00, then disappears.</p>
<p><b>Colors:</b> Normal is green, Warning is amber and Low / ended is red by default.
Choose each color in the editor. Low duration sets the remaining seconds at which the low
color begins; 0 uses it only after expiry. The low color takes priority over warning.
Reset restores the default colors and five-second low threshold.</p>
<p>The panel displays up to eight countdown rows, soonest to expire first, followed by
notifications. Up to 24 countdowns can be tracked; beyond that, the furthest from expiry are
dropped. Right-click a countdown to cancel it or all countdowns. Cancelling a timer
does not erase a separate non-timer trigger's popup; that fades on its own. Always show this panel keeps
the empty panel visible. Running countdowns and popups are temporary; definitions are saved,
but active countdowns are not resumed after restarting PNUT.</p>

<a name="audio"></a><h2>8. Sounds, speech and repeat suppression</h2>
<p><b>Do</b> controls the immediate action: Nothing, Built-in sound, Sound file or Speak text.
Nothing still allows a countdown, or a label popup if Start a timer is off. Built-in sounds need no extra
files. Sound file uses a file on your computer. Speak text fills variables from this match.</p>
<p>The page's top bar controls master Volume, Voice and Speed. Output selects the device for
built-in sounds and sound files; speech uses the system voice output.
Each trigger also has its own Volume; the two volume levels combine. Use a nearby play button
to preview the selected sound or speech. For captured speech, supply a matching Test line first.</p>
<p><b>Quiet for</b> ignores repeats of the same trigger for that many seconds after it fires.
0 means every accepted match. A value of 2 can reduce repeated warnings, but also hides
legitimate rapid events. For a popup for every Smite hit, keep it at 0. Independent triggers
have independent cooldowns. Nothing is aggregated across hits.</p>

<a name="testing"></a><h2>9. Test, preview and tune</h2>
<ol>
<li>Paste one complete message into Test. This checks matching and shows the resolved label
and immediate speech without firing the trigger. Hover over the result for the full text.</li>
<li>Try another damage value or target. Confirm the label changes. Try an unrelated or
similar-spell line and confirm it does not match.</li>
<li>Use Fire this trigger now to exercise the audio and countdown (or popup for a non-timer trigger).
A matching sample supplies capture variables. This explicit preview also works when the
trigger is disabled; it does not enable the trigger for live chat.</li>
<li>Without a matching sample, Fire this trigger now still fires, using your sample or the
pattern as fallback text. Capture variables may remain unresolved. Supply a matching sample
to test captured labels accurately. The manual fire honors the timer overlap rule; a running
Retain timer can therefore suppress it. It bypasses Quiet for.</li>
<li>Check Recently fired for the name and complete line that fired. Then verify a new event
while capture is running. Cancel any test countdowns from the panel's right-click menu.</li>
</ol>
<p>Copy wording from Feed instead of guessing. A missing leading word, clipped row, different
rank, or OCR typo can make a good-looking pattern fail. Capture should include the whole
relevant message. For regex, allow only the variations you have actually observed.</p>

<a name="sharing"></a><h2>10. Import, export and in-game sharing</h2>
<p><b>Import timers…</b> accepts shared JSON files and older triggers.json files. Existing
definitions stay intact; duplicates are skipped and conflicting versions are added as separate
entries. Review and test imported triggers before relying on them. Export timers… can save
the selected entry or all entries, including label, matching, duration, colors and speech.</p>
<p>For game chat, select <b>one</b> trigger and choose <b>Export timers… → Selected timer for
game chat…</b>. Copy its one-line code and paste it into game chat. The receiver needs PNUT
capture running on chat that includes that message. PNUT offers a review before importing;
receiving a code does not automatically install or fire it.</p>
<p>The chat code is limited to 255 characters and never split into multiple messages. Complex
regex recipes or long labels may not fit. Use JSON export for those, or for a collection.
All trigger fields are kept rather than silently truncated. Global voice/output choices
remain local. Custom audio files must be shared separately and selected on the receiving computer.</p>
<p>If a code is not recognized, use a clear chat font or resend it. If OCR changes its contents,
validation can reject it; a JSON file is the dependable fallback. Importing from your older
development folder uses the same Import timers… flow.</p>

<a name="organizing"></a><h2>11. Organize, save and restore triggers</h2>
<p>Use descriptive names and search to keep related triggers together. Edits save
automatically in triggers.json. Disable a trigger to stop live matches while keeping its
settings. Duplicate is useful for adapting a spell rank; change the pattern and label, not
only the name. Export your collection for a portable backup.</p>
<p>Every installation includes Gatekick, Healkick and Invis Break. Healkick starts disabled;
enable it when you want that warning. Invis Break watches
<code>You begin to feel yourself appearing</code> and plays an immediate Falling sound on a
fresh install, without a countdown or delayed warning. Existing installations keep their
chosen immediate audio when the old default countdown is removed.
Restore starter triggers restores missing
starters while keeping existing customizations. An app update preserves your own triggers.
Check for overlapping enabled definitions when you hear duplicate alerts.</p>

<a name="troubleshooting"></a><h2>12. Troubleshooting and support</h2>
<h3>The trigger never fires</h3>
<p>Check that capture is running, the chat is included, and the trigger is enabled. Find the
actual line in Feed and paste it into Test. Check the mode, spelling and rank. A regex ignores
case but not punctuation or spacing. Quiet for or a running Retain timer can suppress a match.</p>
<h3>The popup shows {damage} instead of a number</h3>
<p>Use Regular expression with a named capture such as <code>(?P&lt;damage&gt;\d+)</code> in Text.
Use exactly <code>{damage}</code> in Label. A Contains or Starts with pattern does not create
captures. Test with the complete hit line. Keep captures in the starting Label and use
<code>{label}</code> in warning/end speech.</p>
<h3>The popup is missing, too long, or replaced quickly</h3>
<p>Turn the combat overlay on; its timer panel follows its visibility. Check Start a timer:
timed triggers show their countdown instead of a popup. A non-timer notification lasts
four seconds, and only the four newest are kept. A long label is shortened to fit. Use a shorter
template or make the overlay wider. Under rapid fire, older popups can be replaced before they
fade. Keep Quiet for at 0 if you want each accepted hit, or increase it to reduce noise.</p>
<h3>A new target removes the old timer</h3>
<p>Replace works per trigger, even when the labels contain different targets. Use Add another
timer for concurrent rows. Repeated matches will create repeated timers in that mode too.</p>
<h3>No sound, wrong sound, or an old warning</h3>
<p>Check Do, trigger Volume, master Volume and Output. Check the sound-file path or selected
voice. Test the play button. The trigger's immediate action, timer warning and timer end action
are separate controls. Replace cancels the replaced timer's queued speech; separate triggers
can still have their own alerts. Inspect Recently fired for duplicates.</p>
<h3>My countdown is inaccurate</h3>
<p>Check Length and the message that starts it. PNUT times from the line it read, not from
the game's internal effect state. A late, missed or repeated message can change timing.
An effect ending early is not automatically detected by the countdown.</p>
<p>If you have any questions, contact <b>@Maergoth in Discord</b>. Include your pattern,
matching mode, label and a representative chat line so the problem can be reproduced.</p>
<p><a href="#contents">Back to contents</a></p>
</body></html>
"""

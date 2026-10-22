"""A short release announcement shown once when the main window is in use."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from mnmparse.app import APP_VERSION, theme
from mnmparse.app.app_updates import version_tuple
from mnmparse.app.window_identity import window_title

log = logging.getLogger(__name__)
SEEN_VERSION_KEY = "app/whats_new_version"


@dataclass(frozen=True)
class ReleaseNotes:
    changes: tuple[str, ...]
    settings_label: str
    feature_page: str = ""
    feature_label: str = ""
    settings_target: str = "revenge_enabled"


# Every release adds a record here. A regression check keeps a version bump from
# shipping without its announcement; old records can still describe a rollback.
RELEASE_NOTES: dict[str, ReleaseNotes] = {
    "1.0.22": ReleaseNotes(
        changes=(
            "NPC wiki lookups work with the restored site's API and current page layouts, including levels, drops and notable NPC lists.",
            "Named NPC kindness reactions open the NPC card even when difficulty text follows. Player considers stay out of the card.",
            "More pet, effect and system messages are recognized. Spell expiration notices no longer receive fresh action credit, and manual coin splits do not inflate loot.",
        ),
        settings_label="NPC settings",
        settings_target="npc_enabled",
    ),
    "1.0.21": ReleaseNotes(
        changes=(
            "Resize the NPC card horizontally while unlocked. Width is remembered, and long lists flow into extra columns when space allows.",
            "NPC cards fade after five seconds. Hover to keep one open; leaving starts a fresh countdown.",
            "Low-health warnings now cover wider, screen-scaled edges and pulse until a valid HP reading reaches your threshold again.",
            "Unreadable HP keeps an active warning visible. Reduced motion uses a steady glow; audio keeps its existing cooldown.",
        ),
        settings_label="Mini stat settings",
        settings_target="low_health_threshold",
    ),
    "1.0.20": ReleaseNotes(
        changes=(
            "Consider a mob to show a detachable NPC card. Level and unique drops are on by default.",
            "Choose level, common drops, unique drops, quest requirements and reputation in Settings > Considered NPC.",
            "Use the map header's monster icon to select a notable NPC. Hover item names for available stats.",
            "Cached wiki data appears first and survives outages. First-time lookups need the wiki to respond.",
        ),
        settings_label="NPC settings",
        settings_target="npc_enabled",
    ),
    "1.0.19": ReleaseNotes(
        changes=(
            "Known maps can download and update when the wiki temporarily blocks requests. CDN file hashes avoid downloading unchanged maps again.",
            "Double Attack hits in the same instant count as one swing for the auto-attack timer; damage totals are unchanged.",
            "Right-click the auto-attack bar > Weapon Delay to set a starting estimate: 36 means 3.6 seconds; 0 selects automatic timing.",
            "Weapon Delay contributes one initial timing sample. Actual swings take over, and changing it preserves learned timing.",
        ),
        settings_label="Overlay settings",
        settings_target="attack_bar",
    ),
    "1.0.18": ReleaseNotes(
        changes=(
            "Ancient Crypt and other large maps now load at full resolution. Image-reading failures preserve cached maps.",
            "Mez Breaks has been removed from Session statistics and exports; normal mez tracking is unchanged.",
            "Combat headers now say Group DPS. Aggro events describes reported aggro notices and taunts more clearly.",
            "More game messages are recognized, including mana restoration, dice rolls, trades and skill-up notices.",
        ),
        settings_label="App updates",
        settings_target="app_update_button",
        feature_page="session",
        feature_label="Session",
    ),
    "1.0.17": ReleaseNotes(
        changes=("The Session footer now uses a compact Hourly label without repeated /h suffixes.",),
        settings_label="App updates",
        settings_target="app_update_button",
        feature_page="session",
        feature_label="Session",
    ),
    "1.0.16": ReleaseNotes(
        changes=(
            "Session kill totals now include your pets and assigned group pets, crediting their final blows to their owners.",
            "Pet ownership corrections also update previously recorded kills. Pet deaths do not count as monster kills or player deaths.",
            "Kills/hour, items/hour and coin/hour now use a rolling hour like XP/hour, keeping an old session's idle time from diluting your current pace.",
        ),
        settings_label="App updates",
        settings_target="app_update_button",
        feature_page="session",
        feature_label="Session",
    ),
    "1.0.15": ReleaseNotes(
        changes=(
            "XP/hour now reads the animated orange and yellow XP bar reliably, fixing sessions stuck on XP collecting.",
            "Small bar-size detection changes and briefly missing stat frames preserve XP comparisons.",
            "Local OCR diagnosis reports include mini-stat readings and measurement progress for troubleshooting.",
        ),
        settings_label="Mini stat settings",
        settings_target="player_status_enabled",
        feature_page="session",
        feature_label="Session",
    ),
    "1.0.14": ReleaseNotes(
        changes=(
            "Timer triggers match in the background, keeping the overlay responsive while busy-system delays are retried.",
            "Countdowns account for matching delays, so delayed results do not extend a spell's duration.",
            "Hide Overlay now hides the map, detached Revenge List, timers, attack bar and visual alerts together, restoring your chosen panels when shown again.",
            "New session is at the bottom right of the Session tab beside XP/hour. Rolling XP handles stale readings, small gains and level-up timing more accurately.",
        ),
        settings_label="Overlay settings",
        settings_target="overlay_enabled",
        feature_page="triggers",
        feature_label="Triggers",
    ),
    "1.0.13": ReleaseNotes(
        changes=(
            "Releases now publish automatically with their Windows package and checksum, so Update always finds a complete release.",
            "Trigger regular expressions get more matching time before being disabled: 30 ms each, 150 ms per chat line.",
            "Slowdown warnings and Trigger help now explain that complex regular expressions should be simplified.",
        ),
        settings_label="App update settings",
        settings_target="app_update_button",
        feature_page="triggers",
        feature_label="Triggers",
    ),
    "1.0.11": ReleaseNotes(
        changes=(
            "XP/hour uses a rolling hour, updating every 30 seconds. Idle before your first gain is excluded.",
            "XP readings survive unreadable HP and brief OCR misses.",
            "Mini stats always use at least 1.5× OCR for clearer HP.",
            "New overlays start unlocked; new maps open at the top-left.",
            "Mez awakenings show targets and times, without guessed breakers.",
            "Revenge's compact header makes more room for names.",
            "Revenge alerts match participants, avoiding item names like Wolf Tooth.",
            "Session honors your current group filter; its header indicates all zones.",
        ),
        settings_label="Mini stat settings",
        settings_target="player_status_enabled",
        feature_page="session",
        feature_label="Session",
    ),
    "1.0.10": ReleaseNotes(
        changes=(
            "Setup now selects Combat chat and optional Mini stats from one frame; new setups use a separate stats region.",
            "Choose your low-health threshold and preview its sound beside the dropdown in Mini stat settings.",
            "Low-health confirmation now tolerates small OCR box changes and slower capture cycles.",
            "Incoming PvP attackers show a 30-second + prompt in the floating timers overlay when Revenge List is enabled.",
            "Skill breakdowns now show Damage%, separate from Hit% accuracy.",
            "The Revenge pop-out has a small always-visible close button instead of a hover header.",
        ),
        settings_label="Mini stat settings",
        settings_target="player_status_enabled",
    ),
    "1.0.9": ReleaseNotes(
        changes=(
            "Mini stats can use a separate capture region. Select it in Settings, clear of chat.",
            "Low HP can sound an alert and flash red screen edges. Customize both in Mini stat window settings.",
            "Session estimates XP per hour from bar readings about five minutes apart.",
            "Mez breaks expand by person when a nearby hit identifies the breaker. Uncertain breaks stay separate.",
            "Revenge rows are tighter. Use + beside Players or Guilds to add; click a heading to copy its CSV.",
        ),
        settings_label="Mini stat settings",
        settings_target="player_status_enabled",
        feature_page="session",
        feature_label="Session",
    ),
    "1.0.8": ReleaseNotes(
        changes=(
            "Each Revenge pop-out list now has a + button and a clipboard copy icon.",
            "Use + to enter names or choose CSV from clipboard. Preview entries, then choose Add to save.",
            "CSV now uses one comma-separated row of names. Older CSV imports still work.",
        ),
        settings_label="Revenge settings",
        settings_target="revenge_enabled",
    ),
    "1.0.7": ReleaseNotes(
        changes=(
            "The Revenge pop-out remembers whether it was open, its position and its size after restart.",
            "Removing a player or guild from the pop-out now asks for confirmation.",
            "Each list has clipboard copy and import icons. Preview an import, then choose Add.",
            "What's New catches you up on every release since your last update.",
        ),
        settings_label="Revenge settings",
        settings_target="revenge_enabled",
    ),
    "1.0.6": ReleaseNotes(
        changes=(
            "Turn off Show other groups to hide italic names from your combat meters.",
            "Right-click a player and choose Add pet to assign a hidden italic name.",
            "Assigned pets combine with their owner in Live and overlay meters.",
        ),
        settings_label="Settings",
        settings_target="show_other_groups",
    ),
    "1.0.5": ReleaseNotes(
        changes=(
            "Players and Guilds now sit side by side in the Revenge List.",
            "Each list has Add, From clipboard, and Export CSV controls.",
            "Pop-out controls appear on hover. Resize it using the six-dot grip.",
            "Timers share compact codes. Paste text or open a file in the same Import dialog.",
        ),
        settings_label="Revenge settings",
        feature_page="triggers",
        feature_label="Timer sharing",
    ),
    "1.0.4": ReleaseNotes(
        changes=(
            "The Revenge pop-out shares overlay styling, opacity, text size and move/resize lock.",
            "Saved players appearing in combat chat can trigger a fading alert, soft flash and quiet tone.",
            "Customize Revenge alerts in Settings. Revenge controls stay on their overlay tab.",
            "Settings now group related overlay, Revenge, combat, clipboard and capture options.",
        ),
        settings_label="Revenge settings",
        settings_target="revenge_enabled",
    ),
    "1.0.3": ReleaseNotes(
        changes=(
            "Downloaded updates survive quitting and install when you reopen PNUT.",
            "Updates are verified before installation, preserve saved data and recover if startup fails.",
            "Scrolling through Settings, setup and trigger controls no longer changes values accidentally.",
        ),
        settings_label="App updates",
        settings_target="app_update_button",
    ),
    "1.0.2": ReleaseNotes(
        changes=(
            "Carebear Mode hides individual groupmate performance. Triggers, captured chat and your own information stay available.",
            "Trigger editing and sharing, capture calibration, backups and Revenge work in either mode.",
            "Assign your own pet from your meter row in Carebear Mode.",
            "Maps recognize known-zone /who summaries and synchronize the current zone across sessions and fights.",
        ),
        settings_label="Carebear settings",
        settings_target="morality",
        feature_page="triggers",
        feature_label="Triggers",
    ),
    "1.0.1": ReleaseNotes(
        changes=(
            "Casual Mode is now Carebear Mode, with a clearer teammate promise when switching modes.",
            "Group damage and healing averages compare matching roles, including assigned pets.",
            "Averages need three active contributors. Your damage percentage stays exact; the group shows the remainder.",
            "Personal rebuff timers keep their useful static titles.",
        ),
        settings_label="Carebear settings",
        settings_target="morality",
    ),
    "1.0.0": ReleaseNotes(
        changes=(
            "Guided setup introduces Carebear Mode, chat calibration and common options.",
            "The optional PvP Revenge List adds attacker prompts, manual entries, a pop-out and display limits.",
            "Saved history, capture profiles, preference backups and capture diagnostics help you return to a session.",
            "Capture, imports, storage, map decoding and updates gain reliability improvements. Inactive session executables are cleaned up.",
        ),
        settings_label="Settings",
        settings_target="player_name",
    ),
}


def unseen_releases(version: str, last_seen: str) -> tuple[tuple[str, ReleaseNotes], ...]:
    """Return every crossed release, in order; a fresh install starts at current."""
    notes = RELEASE_NOTES.get(version)
    if notes is None:
        log.warning("What's New notes are missing for version %s", version)
        return ()
    try:
        current = version_tuple(version)
    except ValueError:
        log.warning("What's New has an unsupported version %s", version)
        return ()
    if not last_seen:
        return ((version, notes),)
    try:
        seen = version_tuple(last_seen)
    except ValueError:
        return ((version, notes),)
    if seen >= current:
        return ()
    crossed = []
    for release, release_notes in RELEASE_NOTES.items():
        try:
            number = version_tuple(release)
        except ValueError:
            continue
        if seen < number <= current:
            crossed.append((number, release, release_notes))
    return tuple((release, release_notes) for _number, release, release_notes in sorted(crossed))


class WhatsNewDialog(QDialog):
    settings_requested = Signal()
    settings_target_requested = Signal(str)
    feature_requested = Signal(str)

    def __init__(self, version: str, notes: ReleaseNotes, parent: QWidget | None = None, *,
                 releases: tuple[tuple[str, ReleaseNotes], ...] | None = None):
        super().__init__(parent)
        releases = releases or ((version, notes),)
        self.release_versions = tuple(release for release, _notes in releases)
        self.setObjectName("PnutWhatsNew")
        self.setFont(theme.app_font())
        self.setWindowTitle(window_title("whats-new"))
        self.setAccessibleName("What's New!")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(440)
        self.resize(580, 350 if len(releases) == 1 else 470)
        self.setStyleSheet(f"""
            QDialog#PnutWhatsNew {{ background: {theme.BG0}; color: {theme.TEXT}; }}
            QDialog#PnutWhatsNew QLabel {{ background: transparent; color: {theme.TEXT}; }}
            QDialog#PnutWhatsNew QLabel#Heading {{ font-size: 24px; font-weight: 600; }}
            QDialog#PnutWhatsNew QLabel#ReleaseVersion {{ color: {theme.MUTED}; }}
            QDialog#PnutWhatsNew QFrame#ReleaseChanges {{
                background: {theme.BG1}; border: 1px solid {theme.LINE}; border-radius: 10px;
            }}
            QDialog#PnutWhatsNew QScrollArea, QDialog#PnutWhatsNew QWidget#ReleaseContent {{
                background: {theme.BG0}; border: 0;
            }}
            QDialog#PnutWhatsNew QScrollBar:vertical {{ background: {theme.BG0}; width: 10px; }}
            QDialog#PnutWhatsNew QScrollBar::handle:vertical {{
                background: rgba(255,255,255,0.18); border-radius: 4px; min-height: 24px;
            }}
            QDialog#PnutWhatsNew QScrollBar::add-line:vertical,
            QDialog#PnutWhatsNew QScrollBar::sub-line:vertical {{ height: 0; border: 0; }}
            QDialog#PnutWhatsNew QScrollBar::add-page:vertical,
            QDialog#PnutWhatsNew QScrollBar::sub-page:vertical {{ background: transparent; }}
            QDialog#PnutWhatsNew QPushButton {{
                color: {theme.TEXT}; background: {theme.BG2};
                border: 1px solid {theme.LINE}; border-radius: 8px; padding: 9px 16px;
            }}
            QDialog#PnutWhatsNew QPushButton:hover {{ border-color: {theme.ACCENT}; }}
            QDialog#PnutWhatsNew QPushButton#GotIt {{
                background: {theme.ACCENT}; color: {theme.BG0}; font-weight: 600;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        heading = QLabel("What's New!", self)
        heading.setObjectName("Heading")
        heading.setFont(theme.app_font(24, 600))
        layout.addWidget(heading)
        version_label = QLabel(f"PNUT {version}", self)
        version_label.setObjectName("ReleaseVersion")
        layout.addWidget(version_label)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(self.scroll)
        content.setObjectName("ReleaseContent")
        sections = QVBoxLayout(content)
        sections.setContentsMargins(0, 0, 0, 0)
        sections.setSpacing(12)
        self.change_labels = []
        self.settings_buttons: dict[str, QPushButton] = {}
        self.feature_buttons: dict[str, QPushButton] = {}
        self.feature_button = None
        for release, release_notes in releases:
            card = QFrame(content)
            card.setObjectName("ReleaseChanges")
            changes = QVBoxLayout(card)
            changes.setContentsMargins(16, 14, 16, 14)
            changes.setSpacing(12)
            title = QLabel(f"Version {release}", card)
            title.setFont(theme.app_font(16, 600))
            changes.addWidget(title)
            for change in release_notes.changes:
                label = QLabel(change, card)
                label.setWordWrap(True)
                label.setTextFormat(Qt.TextFormat.PlainText)
                changes.addWidget(label)
                self.change_labels.append(label)
            actions = QHBoxLayout()
            settings_button = QPushButton(release_notes.settings_label, card)
            settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
            settings_button.clicked.connect(
                lambda _checked=False, target=release_notes.settings_target: self._open_settings(target))
            self.settings_buttons[release] = settings_button
            actions.addWidget(settings_button)
            if release_notes.feature_page and release_notes.feature_label:
                feature_button = QPushButton(release_notes.feature_label, card)
                feature_button.setCursor(Qt.CursorShape.PointingHandCursor)
                feature_button.clicked.connect(
                    lambda _checked=False, page=release_notes.feature_page: self._open_feature(page))
                self.feature_buttons[release] = feature_button
                actions.addWidget(feature_button)
            actions.addStretch(1)
            changes.addLayout(actions)
            sections.addWidget(card)
        sections.addStretch(1)
        self.settings_button = self.settings_buttons[version]
        self.feature_button = self.feature_buttons.get(version)
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.got_it_button = QPushButton("Got it", self)
        self.got_it_button.setObjectName("GotIt")
        self.got_it_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.got_it_button.setDefault(True)
        self.got_it_button.clicked.connect(self.accept)
        buttons.addWidget(self.got_it_button)
        layout.addLayout(buttons)

    def _open_settings(self, target: str) -> None:
        self.accept()
        self.settings_requested.emit()
        self.settings_target_requested.emit(target)

    def _open_feature(self, page: str) -> None:
        self.accept()
        self.feature_requested.emit(page)


class WhatsNewController(QObject):
    """Wait for setup and an active main window; acknowledge only a shown dialog."""

    def __init__(self, settings: QSettings, window: QWidget, *,
                 eligible: Callable[[], bool], open_settings: Callable[[], None],
                 open_feature: Callable[[str], None] | None = None,
                 open_settings_target: Callable[[str], None] | None = None,
                 upgrade_from: str = "",
                 version: str = APP_VERSION):
        super().__init__(window)
        self.settings = settings
        self.window = window
        self.version = version
        self.dialog: WhatsNewDialog | None = None
        self._eligible = eligible
        self._open_settings = open_settings
        self._open_feature = open_feature
        self._open_settings_target = open_settings_target
        self._upgrade_from = upgrade_from
        self._stopped = False
        self._presented = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.show_if_needed)
        window.installEventFilter(self)

    def request(self) -> None:
        if not self._stopped and not self._timer.isActive():
            self._timer.start(0)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.window and event.type() in (
            QEvent.Type.Show, QEvent.Type.WindowActivate, QEvent.Type.WindowStateChange,
        ):
            self.request()
        return False

    def show_if_needed(self) -> bool:
        if (self._stopped or self.dialog is not None
                or not self._eligible() or not self.window.isVisible() or self.window.isMinimized()
                or not self.window.isActiveWindow() or QApplication.activeModalWidget() is not None):
            return False
        last_seen = self.settings.value(SEEN_VERSION_KEY, "", type=str)
        try:
            version_tuple(last_seen)
        except ValueError:
            last_seen = self._upgrade_from
        releases = unseen_releases(self.version, last_seen)
        if not releases:
            return False
        dialog = WhatsNewDialog(self.version, releases[-1][1], self.window, releases=releases)
        self.dialog = dialog
        if self._open_settings_target is not None:
            dialog.settings_target_requested.connect(self._open_settings_target)
        else:
            dialog.settings_requested.connect(self._open_settings)
        if self._open_feature is not None:
            dialog.feature_requested.connect(self._open_feature)
        else:
            for button in dialog.feature_buttons.values():
                button.hide()
        dialog.finished.connect(self._dismissed)
        dialog.open()
        self._presented = dialog.isVisible()
        return self._presented

    def _dismissed(self, _result: int) -> None:
        if self._presented and not self._stopped:
            self.settings.setValue(SEEN_VERSION_KEY, self.version)
            self.settings.sync()
        dialog, self.dialog = self.dialog, None
        self._presented = False
        if dialog is not None:
            dialog.deleteLater()

    def stop(self) -> None:
        self._stopped = True
        self._timer.stop()
        if self.dialog is not None:
            self.dialog.reject()

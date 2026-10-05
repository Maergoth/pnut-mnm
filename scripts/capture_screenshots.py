"""Render documentation screenshots from the actual widgets and fictional data.

Run with the development environment, for example:
    python scripts/capture_screenshots.py --map-cache /path/to/map_cache

No engine or capture starts. Settings and any map repository writes are isolated
in a temporary directory. The optional map cache is only read, never refreshed.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = "1"
os.environ["QT_FONT_DPI"] = "96"
if sys.platform == "win32":
    os.environ["QT_QPA_FONTDIR"] = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from mnmparse.app import theme
from mnmparse.app.main import MainWindow, _MissingEngine
from mnmparse.app.models import ActorRow, EncounterSnapshot, SkillRow, actor_color
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.session import SessionStats


def sample_encounter() -> EncounterSnapshot:
    start = datetime(2026, 10, 5, 18, 49).timestamp()
    party = [("Aster", 2680), ("Rowan", 2220), ("Bram", 1770),
             ("Neris", 1210), ("Tamsin", 480), ("Mira", 160), ("Ember", 650)]
    total = sum(damage for _, damage in party)
    rows = []
    for index, (name, damage) in enumerate(party):
        skill_names = ("Slash", "Crusader Strike", "Kick") if index == 0 else ("Pierce", "Strike", "Kick")
        values = [int(damage * .62), int(damage * .28)]
        values.append(damage - sum(values))
        counts = [36, 10, 8]
        skills = [SkillRow(skill, hits, value, round(value / hits * 1.7), value / hits, hits, 0)
                  for skill, value, hits in zip(skill_names, values, counts)]
        rows.append(ActorRow(
            name=name, damage=damage, dps=damage / 180, taken=1640 if index == 0 else 180,
            dtps=(1640 if index == 0 else 180) / 180, heals=2240 if name == "Mira" else 0,
            hps=2240 / 180 if name == "Mira" else 0, healed=1520 if index == 0 else 120,
            swings=60, hits=54, misses=6, hit_pct=90, max_hit=max(s.max_hit for s in skills),
            avg_hit=damage / 54, share=damage / total,
            color=actor_color(name, is_you=index == 0, is_npc=False, is_pet=name == "Ember"),
            is_you=index == 0, is_npc=False, is_pet=name == "Ember", skills=skills,
            utility=7 if index == 0 else 2, cc=3 if index == 0 else 1,
            cc_types={"interrupt": 3} if index == 0 else {},
            aggro=4 if index == 0 else 1, prevented=420 if index == 0 else 0,
            pet_owner="Neris" if name == "Ember" else "", misses_shown=index == 0,
        ))
    return EncounterSnapshot(
        key=str(start), label="an ancient guardian", start=start, end=start + 180,
        duration=180, closed=False, event_count=428, total_damage=total,
        raid_dps=total / 180, killed=[], rows=rows, zone="Infested Crypt",
        zone_since=start - 540, active_duration=180, group_members=[n for n, _ in party[:-1]],
    )


def sample_session(start: float):
    session = SessionStats("Aster", started=start)
    lines = [(0, "You have entered Shaded Dunes."), (600, "You have entered Infested Crypt.")]
    loot = {
        "Aster": ["Bone Chips"] * 4 + ["Cloth Scraps"] * 3 + ["Cracked Gem"] * 2,
        "Rowan": ["Bone Chips"] * 5 + ["Worn Bow"] * 2,
        "Mira": ["Cloth Scraps"] * 3 + ["Cracked Gem"],
    }
    for index, (name, items) in enumerate(loot.items()):
        for offset, item in enumerate(items):
            lines.append((660 + index * 90 + offset * 7,
                          f"--{name} loots [{item}] from a skeletal warrior's corpse.--"))
    for index in range(18):
        name = ("Aster", "Rowan", "Bram")[index % 3]
        lines.append((610 + index * 75, f"Your party member {name} has slain a skeletal warrior!"))
    lines.extend([
        (1700, "You loot 3 silver coins from a skeletal warrior's corpse."),
        (1800, "Rowan loots 46 copper coins from a skeletal warrior's corpse, and you receive 8 copper coins from a skeletal warrior's corpse as your split."),
        (1900, "Mira crafts Cloth Scraps(3)."),
    ])
    for offset, line in sorted(lines):
        session.add(parse_line(line, start + offset, "Aster"))
    for duration in (130, 90, 150, 180):
        session.note_encounter(duration)
    return session.snapshot(now=start + 2400)


def render_main(app, settings, output):
    cfg = Config(player_name="Aster", start_capture_on_launch=False)
    window = MainWindow(_MissingEngine(cfg), None, cfg, settings)
    window.resize(1600, 880)
    window.set_overlay_available(True, lock_available=True)
    window.on_state("running")
    window.show()
    app.processEvents()
    snapshot = sample_encounter()
    for index, label in enumerate(("a skeletal warrior", "a skeletal cleric", "a skeletal knight")):
        start = snapshot.start - 480 + index * 150
        old = replace(snapshot, key=str(start), label=label, start=start, end=start + 100,
                      duration=100, closed=True, killed=[label], kills=1)
        window.on_encounter_closed(old)
    window.on_snapshot(snapshot)
    live = window.page("live")
    live._splitter.setSizes([280, 1200])
    live._pane._splitter.setSizes([350, 250])
    window.show_message("Representative sample encounter data")
    # Let nested chip/scroll-area layout requests settle after their sample data
    # replaces the initial placeholders, just as several GUI turns would in use.
    for _ in range(6):
        app.processEvents()
    window.grab().save(str(output / "combat-session.png"))

    window.on_session(sample_session(snapshot.start - 1800))
    window.show_page("session")
    tree = window.page("session")._view._tree
    items = tree.topLevelItem(0)
    items.setExpanded(True)
    for index in range(items.childCount()):
        items.child(index).setExpanded(index < 2)
    window.resize(1280, 790)
    window.show_message("Representative sample session data")
    app.processEvents()
    window.grab().save(str(output / "session-loot.png"))
    window.prepare_quit()
    window.close()


def render_map(app, settings, output, cache, scratch):
    from mnmparse.app.map_overlay import MapOverlay
    from mnmparse.maps import MapImage, MapRepository

    def cached(key, suffix):
        return cache / (hashlib.sha256(key.encode()).hexdigest() + suffix)

    entries = [MapImage(**entry) for entry in json.loads(cached("Shaded Dunes", ".json").read_text(encoding="utf-8"))]
    data = cached(entries[0].url, ".image").read_bytes()
    window = MapOverlay(settings, MapRepository(scratch / "map-cache"))
    window.set_zone("Shaded Dunes")
    window.resize(1100, 730)
    window._ready(window._token, (entries, data, True), "")
    window.set_locked(False)
    window.set_appearance(0.96, 1.12)
    window.show()
    window._show_header()
    app.processEvents()
    window.grab().save(str(output / "map-overlay.png"))
    print("Map source:", entries[0].source)
    print("Map image:", entries[0].url)
    window.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map-cache", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "images")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    theme.apply_theme(app)
    with tempfile.TemporaryDirectory(prefix="pnut-screenshots-") as folder:
        scratch = Path(folder)
        settings = QSettings(str(scratch / "preview.ini"), QSettings.Format.IniFormat)
        render_main(app, settings, args.output)
        if args.map_cache:
            render_map(app, settings, args.output, args.map_cache, scratch)
        app.processEvents()


if __name__ == "__main__":
    main()

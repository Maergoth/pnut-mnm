"""Viewer harvest counters stay complete across projections and session recovery."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest

from mnmparse.config import Config
from mnmparse.grammar import Event
from mnmparse.parser import parse_line
from mnmparse.privacy import project_session, safe_event_text
from mnmparse.session import SessionSnapshot, SessionStats, filter_group_session, filter_session
from mnmparse.session_archive import SessionArchive


PLAYER = "Harvester"


def harvest(session: SessionStats, item: str = "Wood", ts: float = 1) -> Event:
    event = parse_line(f"You harvest [{item}]!", ts, session.player_name)
    session.add(event)
    return event


class SessionHarvestTests(unittest.TestCase):
    def test_successful_harvests_count_without_personal_lines_enabled(self) -> None:
        for include_personal in (False, True):
            with self.subTest(include_personal=include_personal):
                session = SessionStats(PLAYER, include_personal=include_personal, started=0)
                first = session.snapshot(now=1)
                harvest(session)
                harvest(session, "Resin", 2)
                snap = session.snapshot(now=3)
                self.assertEqual(snap.harvests, 2)
                self.assertEqual(snap.harvests_by_item, [("Resin", 1), ("Wood", 1)])
                self.assertGreater(snap.revision, first.revision)
                self.assertEqual((snap.items, snap.crafts), (0, 0))
                self.assertEqual([entry.kind for entry in snap.recent], ["harvest", "harvest"])

    def test_identical_distinct_successes_are_not_deduplicated_by_session(self) -> None:
        session = SessionStats(PLAYER, started=0)
        # The capture tracker owns confirmation/deduplication. Equal content or
        # timestamps cannot erase separately emitted harvest events.
        for _ in range(4):
            harvest(session, ts=1)
        snap = session.snapshot(now=2)
        self.assertEqual((snap.harvests, snap.harvests_by_item), (4, [("Wood", 4)]))

    def test_other_items_and_unsuccessful_lines_never_count_as_harvests(self) -> None:
        session = SessionStats(PLAYER, include_personal=True, started=0)
        lines = ["You loot [Wood] from a rat's corpse.",
                 "You craft Wood(3).", "You eat Wood.", "You drink Resin.",
                 "You loot Wood from your corpse.", "You receive Resin from Jalwa Noor.",
                 "You fail to harvest [Wood]!", "Friend harvests [Wood]!"]
        for ts, text in enumerate(lines, 1):
            session.add(parse_line(text, ts, PLAYER))
        snap = session.snapshot(now=10)
        self.assertEqual((snap.harvests, snap.harvests_by_item), (0, []))
        self.assertEqual((snap.items, snap.crafts, len(snap.rewards)), (1, 3, 1))

    def test_harvest_kind_only_counts_the_viewer_and_one_success_per_line(self) -> None:
        session = SessionStats(PLAYER, started=0)
        session.add(parse_line("Friend has joined the party.", 1, PLAYER))
        session.add(Event(2, "harvest", "Friend harvests [Wood]!", actor="Friend", item="Wood"))
        session.add(Event(3, "harvest", "You harvest [Resin]!", actor=PLAYER,
                          raw_actor="You", item="Resin", amount=8))
        snap = session.snapshot(now=4)
        self.assertEqual((snap.harvests, snap.harvests_by_item), (1, [("Resin", 1)]))

    def test_legacy_personal_events_require_reparsed_harvest_text(self) -> None:
        session = SessionStats(PLAYER, started=0)
        session.add(Event(1, "personal", "You harvest [Wood]!", item="Wrong cached item"))
        session.add(Event(2, "personal", "You eat Wood.", item="Wood"))
        session.add(Event(3, "personal", "You loot Resin from your corpse.", item="Resin"))
        session.add(Event(4, "personal", "Unrecognized item line", item="Wood"))
        snap = session.snapshot(now=5)
        self.assertEqual((snap.harvests, snap.harvests_by_item), (1, [("Wood", 1)]))

    def test_bounded_snapshots_keep_complete_sorted_per_item_totals(self) -> None:
        session = SessionStats(PLAYER, started=0)
        for ts in range(301):
            harvest(session, "Wood" if ts < 201 else "Resin", ts)
        for limit in (0, 1, 5, None):
            with self.subTest(limit=limit):
                snap = session.snapshot(now=400, detail_limit=limit)
                self.assertEqual(snap.harvests, 301)
                self.assertEqual(snap.harvests_by_item, [("Wood", 201), ("Resin", 100)])
                self.assertLessEqual(len(snap.recent), 200)
                self.assertEqual(filter_session(snap, PLAYER).harvests, 301)

    def test_new_session_resets_without_changing_previous_snapshot(self) -> None:
        old = SessionStats(PLAYER, started=0)
        harvest(old)
        previous = old.snapshot(now=2)
        new = SessionStats(PLAYER, started=2)
        harvest(new, "Resin", 3)
        self.assertEqual((previous.harvests, previous.harvests_by_item), (1, [("Wood", 1)]))
        self.assertEqual(new.snapshot(now=4).harvests_by_item, [("Resin", 1)])

    def test_player_name_change_keeps_original_harvest_ownership(self) -> None:
        session = SessionStats("First", started=0)
        harvest(session)
        previous = session.snapshot(now=2)
        session.player_name = "Second"
        renamed = session.snapshot(now=3)
        self.assertEqual(renamed.harvests, 1)
        self.assertEqual(filter_session(renamed, "Second").harvests, 0)
        harvest(session, "Resin", 4)
        snap = session.snapshot(now=5, detail_limit=0)
        self.assertEqual((snap.harvests, snap.harvests_by_item), (2, [("Resin", 1), ("Wood", 1)]))
        self.assertEqual(filter_session(snap, "First").harvests_by_item, [("Wood", 1)])
        self.assertEqual(filter_session(snap, "Second").harvests_by_item, [("Resin", 1)])
        self.assertEqual(project_session(snap, Config(player_name="Second")).harvests_by_item, [("Resin", 1)])
        self.assertEqual(project_session(snap, Config(player_name="First")).harvests_by_item, [("Wood", 1)])
        self.assertEqual(filter_group_session(snap, "Second").harvests_by_item, [("Resin", 1)])
        snap.party = ["First"]
        grouped = filter_group_session(snap, "Second")
        self.assertEqual(grouped.harvests, 2)
        self.assertEqual(filter_session(grouped, "Second").harvests_by_item, [("Resin", 1)])
        self.assertEqual(previous.harvests_by_item, [("Wood", 1)])

    def test_group_personal_and_carebear_projections_keep_only_viewer_harvests(self) -> None:
        session = SessionStats(PLAYER, started=0)
        for ts, text in enumerate(("Friend has joined the party.", "Other has joined the party."), 1):
            session.add(parse_line(text, ts, PLAYER))
        harvest(session, ts=3)
        harvest(session, "Resin", 4)
        snap = session.snapshot(now=5, detail_limit=1)
        direct = filter_session(snap, PLAYER.lower())
        grouped = filter_group_session(snap, PLAYER.lower())
        configs = [Config(player_name=PLAYER),
                   Config(player_name=PLAYER, casual_mode=False, casual_mode_confirmed=True),
                   Config(player_name=PLAYER, casual_mode=False, casual_mode_confirmed=True,
                          show_other_groups=True)]
        views = [direct, filter_session(direct, PLAYER), grouped,
                 filter_session(grouped, PLAYER), *(project_session(snap, cfg) for cfg in configs)]
        for view in views:
            self.assertEqual((view.harvests, view.harvests_by_item), (2, [("Resin", 1), ("Wood", 1)]))
        for name in ("", "Different"):
            protected = project_session(snap, Config(player_name=name))
            self.assertEqual((protected.harvests, protected.harvests_by_item), (0, []))
        self.assertNotIn("harvests", project_session(snap, configs[0]).group_averages)

    def test_structured_harvest_text_is_safe_and_exports_include_new_fields(self) -> None:
        session = SessionStats(PLAYER, started=0)
        event = harvest(session)
        cfg = Config(player_name=PLAYER)
        self.assertEqual(safe_event_text(event, cfg), "You harvested an item")
        self.assertIsNone(safe_event_text(replace(event, actor="Other"), cfg))
        data = json.loads(json.dumps(asdict(project_session(session.snapshot(now=2), cfg))))
        self.assertEqual(data["harvests"], 1)
        self.assertEqual(data["harvests_by_item"], [["Wood", 1]])


class SessionHarvestArchiveTests(unittest.TestCase):
    def test_recovery_checkpoint_and_bounded_preview_keep_complete_harvests(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions.sqlite3"
            archive = SessionArchive(path)
            session = SessionStats(PLAYER, started=0)
            archive.attach_details(session, "active")
            for ts in range(205):
                harvest(session, "Wood" if ts < 201 else "Resin", ts)
            archive.save_session("active", session)
            reopened = SessionArchive(path)
            recovered = reopened.restore("active")
            self.assertIsInstance(recovered._harvests_by_item["Wood"], Counter)
            self.assertEqual(recovered.snapshot(now=300, detail_limit=1).harvests, 205)
            harvest(recovered, "Resin", 301)
            reopened.checkpoint("active", recovered)
            recovered = SessionArchive(path).restore("active")
            snap = recovered.snapshot(now=302)
            self.assertEqual((snap.harvests, snap.harvests_by_item), (206, [("Wood", 201), ("Resin", 5)]))
            reopened.save_session("active", recovered, ended=303)
            preview = reopened.preview("active", detail_limit=1)
            self.assertEqual(len(preview.recent), 1)
            self.assertEqual(preview.harvests_by_item, snap.harvests_by_item)
            self.assertEqual(filter_session(preview, PLAYER).harvests_by_item, snap.harvests_by_item)
            self.assertEqual(project_session(preview, Config(player_name=PLAYER)).harvests, 206)

    def test_archive_and_bounded_preview_preserve_name_change_allocations(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions.sqlite3"
            archive = SessionArchive(path)
            session = SessionStats("First", started=0)
            harvest(session)
            session.player_name = "Second"
            harvest(session, "Resin", 2)
            archive.save_session("renamed", session)
            reopened = SessionArchive(path)
            for snap in (reopened.restore("renamed").snapshot(now=3, detail_limit=0),
                         reopened.preview("renamed", detail_limit=0)):
                self.assertEqual(snap.harvests, 2)
                self.assertEqual(filter_session(snap, "First").harvests_by_item, [("Wood", 1)])
                self.assertEqual(filter_session(snap, "Second").harvests_by_item, [("Resin", 1)])
                self.assertEqual(filter_group_session(snap, "Second").harvests_by_item, [("Resin", 1)])
                self.assertEqual(project_session(snap, Config(player_name="Second")).harvests_by_item,
                                 [("Resin", 1)])
                self.assertNotIn("_harvest_item_actors", asdict(snap))
            restored = reopened.restore("renamed")
            restored.player_name = "First"
            harvest(restored, "Wood", 4)
            reopened.checkpoint("renamed", restored)
            final = SessionArchive(path).restore("renamed").snapshot(now=5)
            self.assertEqual((final.harvests, final.harvests_by_item), (3, [("Wood", 2), ("Resin", 1)]))
            self.assertEqual(filter_session(final, "First").harvests_by_item, [("Wood", 2)])
            self.assertEqual(filter_session(final, "Second").harvests_by_item, [("Resin", 1)])

    def test_older_archive_and_snapshot_default_to_zero_and_accept_future_harvests(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            archive = SessionArchive(Path(folder) / "sessions.sqlite3")
            archive.save_session("old", SessionStats(PLAYER, started=0))
            with archive._connect() as db:
                state_json, preview_json = db.execute(
                    "SELECT state,preview FROM sessions WHERE id='old'").fetchone()
                state, preview = json.loads(state_json), json.loads(preview_json)
                state.pop("_harvests_by_item")
                preview.pop("_harvest_item_actors")
                preview.pop("harvests")
                preview.pop("harvests_by_item")
                preview["_personal_totals"].pop("harvests")
                preview["_personal_totals"].pop("harvests_by_item")
                db.execute("UPDATE sessions SET state=?,preview=? WHERE id='old'",
                           (json.dumps(state), json.dumps(preview)))
            recovered = archive.restore("old")
            self.assertEqual((recovered.snapshot(now=2).harvests,
                              archive.preview("old").harvests_by_item), (0, []))
            harvest(recovered, "Resin", 3)
            self.assertEqual(recovered.snapshot(now=4).harvests_by_item, [("Resin", 1)])
            # Direct dataclass consumers of an older snapshot get the same defaults.
            preview.pop("_personal_totals")
            self.assertEqual((SessionSnapshot(**preview).harvests,
                              SessionSnapshot(**preview).harvests_by_item), (0, []))


if __name__ == "__main__":
    unittest.main()

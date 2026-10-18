"""Session rates use complete event history within the current rolling hour."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.party import PartyRoster
from mnmparse.privacy import project_session
from mnmparse.session import SessionStats, filter_session
from mnmparse.session_archive import DiskDetails, SessionArchive


PLAYER = "Maergoth"
PEER = "Tamsin"
OUTSIDER = "Septimal"


class SessionRollingRateTests(unittest.TestCase):
    def make_session(self, *, started: float = 0) -> tuple[SessionStats, PartyRoster]:
        roster = PartyRoster(PLAYER)
        roster.set_manual(PEER, True)
        return SessionStats(PLAYER, started=started, roster=roster), roster

    def feed(self, session: SessionStats, text: str, ts: float):
        event = parse_line(text, ts, PLAYER)
        session.add(event)
        return event

    def own_activity(self, session: SessionStats, ts: float, copper: int = 100) -> None:
        self.feed(session, "You have slain a rat!", ts)
        self.feed(session, "You loot [Bone Chips] from a rat's corpse.", ts)
        self.feed(session, f"You loot {copper} copper coins from a rat's corpse.", ts)

    def assert_rates(self, snap, kills: float, items: float, copper: float) -> None:
        self.assertAlmostEqual(snap.kills_per_hour, kills)
        self.assertAlmostEqual(snap.items_per_hour, items)
        self.assertAlmostEqual(snap.coin_per_hour, copper)

    def group(self, snap):
        return project_session(snap, Config(player_name=PLAYER, casual_mode=False,
                                            casual_mode_confirmed=True))

    def test_old_events_leave_rates_without_changing_lifetime_totals(self) -> None:
        session, _roster = self.make_session()
        for ts in (100, 110, 120):
            self.own_activity(session, ts)
        self.own_activity(session, 4000)
        snap = session.snapshot(now=7200)
        self.assertEqual((snap.kills, snap.items, snap.coin_total), (4, 4, 400))
        self.assert_rates(snap, 1, 1, 100)
        self.assertEqual(snap.activity_rates["kind"], "rolling")
        self.assertEqual(snap.activity_rates["window_seconds"], 3600)
        self.assertEqual(snap.activity_rates["as_of"], 7200)
        self.assertEqual(snap.activity_rates["observed_seconds"], 3600)

    def test_denominator_uses_session_elapsed_with_minute_floor_and_hour_cap(self) -> None:
        for elapsed, rate in ((30, 60), (120, 30), (7200, 1)):
            with self.subTest(elapsed=elapsed):
                session, _roster = self.make_session(started=100)
                # Idle before the first event is part of the observation span.
                self.own_activity(session, 100 + elapsed - 10, copper=1)
                snap = session.snapshot(now=100 + elapsed)
                self.assert_rates(snap, rate, rate, rate)
                self.assertEqual(snap.activity_rates["observed_seconds"], min(elapsed, 3600))

    def test_hour_boundary_is_open_at_cutoff_and_closed_at_snapshot_time(self) -> None:
        session, _roster = self.make_session()
        for ts in (3599, 3600, 3600.1, 7200, 7200.1):
            self.own_activity(session, ts, copper=1)
        snap = session.snapshot(now=7200)
        self.assertEqual((snap.kills, snap.items, snap.coin_total), (5, 5, 5))
        self.assert_rates(snap, 2, 2, 2)

    def test_future_events_enter_rates_only_when_snapshot_clock_reaches_them(self) -> None:
        session, _roster = self.make_session(started=100)
        self.own_activity(session, 150, copper=1)
        self.own_activity(session, 300, copper=1)
        earlier = session.snapshot(now=200)
        self.assert_rates(earlier, 36, 36, 36)
        later = session.snapshot(now=400)
        self.assert_rates(later, 24, 24, 24)

    def test_idle_expiry_and_backwards_clock_reproject_without_mutating_prior_snapshot(self) -> None:
        session, _roster = self.make_session()
        self.own_activity(session, 100, copper=1)
        self.own_activity(session, 3800, copper=1)
        first = session.snapshot(now=4000)
        frozen_rates = copy.deepcopy(first.activity_rates)
        self.assert_rates(first, 1, 1, 1)
        expired = session.snapshot(now=7500)
        self.assert_rates(expired, 0, 0, 0)
        self.assertEqual(expired.revision, first.revision)
        backwards = session.snapshot(now=4000)
        self.assert_rates(backwards, 1, 1, 1)
        self.assertEqual(first.activity_rates, frozen_rates)
        self.assertEqual((expired.kills, expired.items, expired.coin_total), (2, 2, 2))

    def test_group_self_and_privacy_rates_follow_ownership_and_coin_basis(self) -> None:
        session, roster = self.make_session()
        for pet, owner in (("Sprout", PLAYER), ("Briar", PEER), ("Thorn", OUTSIDER)):
            roster.set_pet_owner(pet, owner)
        for ts, killer in enumerate((PLAYER, "Sprout", "Briar", "Thorn"), 3900):
            self.feed(session, f"a rat has been slain by {killer}!", ts)
        for actor, count in (("You", 2), (PEER, 3), (OUTSIDER, 4)):
            for index in range(count):
                self.feed(session, f"{actor} loot{'s' if actor != 'You' else ''} [Bone Chips] from a rat's corpse.",
                          4000 + index)
        self.feed(session, "You loot 40 copper coins from a rat's corpse.", 4100)
        self.feed(session, f"{PEER} loots 100 copper coins from a rat's corpse, and you receive 10 copper coins "
                          "from a rat's corpse as your split.", 4200)
        self.feed(session, f"{OUTSIDER} loots 500 copper coins from a rat's corpse.", 4300)
        self.feed(session, f"{PEER} loots 1000 copper coins from a rat's corpse, and you receive 100 copper coins "
                          "from a rat's corpse as your split.", 100)
        raw = session.snapshot(now=7200)
        group = self.group(raw)
        own = filter_session(raw, PLAYER)
        casual = project_session(raw, Config(player_name=PLAYER))
        self.assert_rates(group, 3, 5, 140)
        self.assert_rates(own, 2, 2, 50)
        self.assert_rates(casual, 2, 2, 50)
        self.assertEqual(group.activity_rates["coin_basis"], "gross")
        self.assertEqual(own.activity_rates["coin_basis"], "received")
        self.assertEqual(casual.activity_rates["coin_basis"], "received")
        self.assertEqual(set(casual.activity_rates["by_actor"]), {PLAYER})
        exported = json.dumps(asdict(casual))
        for hidden in (PEER, OUTSIDER, "Sprout", "Briar", "Thorn"):
            self.assertNotIn(hidden, exported)

    def test_bounded_archive_preview_keeps_complete_rates_beyond_two_hundred_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, roster = self.make_session()
            roster.set_pet_owner("Briar", PEER)
            for index in range(250):
                self.own_activity(session, 100 + index, copper=1)
            for index in range(10):
                ts = 1000 + index
                self.feed(session, "a rat has been slain by Briar!", ts)
                self.feed(session, f"{PEER} loots [Bone Chips] from a rat's corpse.", ts)
                self.feed(session, f"{PEER} loots 2 copper coins from a rat's corpse.", ts)
            archive = SessionArchive(Path(directory) / "sessions.sqlite3")
            archive.attach_details(session, "busy")
            archive.save_session("busy", session, ended=3600)
            restored = archive.restore("busy")
            self.assertIsInstance(restored._slain, DiskDetails)
            for source, snap in (("restored", restored.snapshot(now=3600, detail_limit=1)),
                                 ("preview", archive.preview("busy", detail_limit=1))):
                with self.subTest(source=source):
                    self.assertEqual((len(snap.loot), len(snap.kill_entries)), (1, 1))
                    self.assert_rates(self.group(snap), 260, 260, 270)
                    self.assert_rates(filter_session(snap, PLAYER), 250, 250, 250)
                    self.assert_rates(project_session(snap, Config(player_name=PLAYER)), 250, 250, 250)

    def test_late_pet_assignment_and_owner_departure_rebuild_current_hour_rates(self) -> None:
        session, roster = self.make_session()
        self.feed(session, "a rat has been slain by Sprout!", 4000)
        unknown = session.snapshot(now=7200)
        self.assert_rates(self.group(unknown), 0, 0, 0)
        roster.set_pet_owner("Sprout", PLAYER)
        mine = session.snapshot(now=7200)
        self.assert_rates(filter_session(mine, PLAYER), 1, 0, 0)
        roster.set_pet_owner("Sprout", PEER)
        peer = session.snapshot(now=7200)
        self.assert_rates(self.group(peer), 1, 0, 0)
        self.assert_rates(filter_session(peer, PLAYER), 0, 0, 0)
        roster.set_manual(PEER, False)
        self.assert_rates(self.group(session.snapshot(now=7200)), 0, 0, 0)

    def test_wrapped_split_uses_original_coin_timestamp_without_double_counting(self) -> None:
        session, _roster = self.make_session()
        self.feed(session, f"{PEER} loots 100 copper coins from a rat's corpse.", 3599)
        split = self.feed(session, "10 copper coins from a rat's corpse as your split.", 3601)
        self.assertEqual(split.kind, "coin_split")
        recent = session.snapshot(now=3602)
        self.assert_rates(self.group(recent), 0, 0, 100)
        self.assert_rates(filter_session(recent, PLAYER), 0, 0, 10)
        snap = session.snapshot(now=7200)
        self.assertEqual((snap.coin_total, snap.coin_received), (100, 10))
        self.assert_rates(self.group(snap), 0, 0, 0)
        self.assert_rates(filter_session(snap, PLAYER), 0, 0, 0)

    def test_legacy_preview_keeps_session_average_but_restored_raw_history_gets_rolling_rates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, _roster = self.make_session()
            for ts in (100, 110, 120, 4000):
                self.own_activity(session, ts)
            archive = SessionArchive(Path(directory) / "sessions.sqlite3")
            archive.attach_details(session, "legacy")
            archive.save_session("legacy", session, ended=7200)
            self.assert_rates(archive.preview("legacy", detail_limit=1), 1, 1, 100)
            with closing(sqlite3.connect(archive.path)) as db, db:
                preview = json.loads(db.execute("SELECT preview FROM sessions WHERE id='legacy'").fetchone()[0])
                preview.pop("activity_rates", None)
                db.execute("UPDATE sessions SET preview=? WHERE id='legacy'", (json.dumps(preview),))
            legacy = archive.preview("legacy", detail_limit=1)
            self.assertFalse(legacy.activity_rates)
            self.assert_rates(legacy, 2, 2, 200)
            self.assert_rates(filter_session(legacy, PLAYER), 2, 2, 200)
            restored = archive.restore("legacy").snapshot(now=7200, detail_limit=1)
            self.assertEqual((restored.kills, restored.items, restored.coin_total), (4, 4, 400))
            self.assert_rates(restored, 1, 1, 100)


if __name__ == "__main__":
    unittest.main()

"""Party death notices are not final blows, and self views hide nearby deaths."""
from __future__ import annotations

import unittest

from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.privacy import project_session
from mnmparse.session import SessionStats, filter_session


PLAYER = "Viewer"


class SessionPartyDeathTests(unittest.TestCase):
    def session(self, lines):
        session = SessionStats(PLAYER, started=0)
        for ts, text in lines:
            event = parse_line(text, ts, PLAYER)
            self.assertNotEqual(event.kind, "unknown", text)
            session.add(event)
        return session

    def test_passive_party_death_proves_membership_without_counting_a_kill(self):
        session = self.session([
            (10, "Your party member Friend has been slain by Guard Nicolas!"),
        ])
        snapshot = session.snapshot(now=20)
        self.assertEqual(snapshot.party, ["Friend"])
        self.assertNotIn("Guard Nicolas", snapshot.party)
        self.assertEqual(snapshot.kills, 0)
        self.assertEqual(snapshot.kill_entries, [])
        self.assertEqual(snapshot.deaths, 1)
        self.assertEqual(snapshot.deaths_by_player, [("Friend", 1)])
        self.assertEqual(snapshot.outsider_deaths, [])

    def test_passive_party_death_retains_existing_death_deduplication(self):
        session = self.session([
            (10, "Your party member Friend has been slain by Guard Nicolas!"),
            (40, "Your party member Friend has been slain by Guard Nicolas!"),
            (80, "Your party member Friend has been slain by Guard Nicolas!"),
        ])
        snapshot = session.snapshot(now=100)
        self.assertEqual(snapshot.kills, 0)
        self.assertEqual(snapshot.deaths_by_player, [("Friend", 2)])
        self.assertEqual(snapshot.outsider_deaths, [])

    def test_active_party_kill_keeps_explicit_credit_after_party_disbands(self):
        session = self.session([
            (10, "Your party member Friend has slain Grandmaster Obadiah!"),
            (20, "Your party has been disbanded."),
            (30, "You have slain Grandmaster Gildas!"),
        ])
        snapshot = session.snapshot(now=40)
        self.assertEqual(snapshot.party, [])
        self.assertEqual(snapshot.kills, 2)
        self.assertEqual(dict(snapshot.kills_by_killer), {"Friend": 1, PLAYER: 1})
        self.assertEqual(snapshot.deaths_by_player, [])
        self.assertEqual(snapshot.outsider_deaths, [], "Named NPC kills must remain kills only")

    def test_active_party_kill_preserves_normalized_and_clipped_ocr_notices(self):
        canonical = "Your party member Friend has slain Grandmaster Obadiah!"
        for text in (canonical, canonical.replace("Your", "Vour").replace("slain", "stain"),
                     canonical.replace("Your", "our")):
            with self.subTest(text=text):
                session = SessionStats(PLAYER, started=0)
                # Completion can classify a clipped row while retaining its raw
                # text. Explicit party credit must survive that representation.
                event = parse_line(canonical, 10, PLAYER)
                event.text = text
                session.add(event)
                session.add(parse_line("Your party has been disbanded.", 20, PLAYER))
                snapshot = session.snapshot(now=30)
                self.assertEqual(snapshot.party, [])
                self.assertEqual(snapshot.kills, 1)
                self.assertEqual(snapshot.kills_by_killer, [("Friend", 1)])
                self.assertEqual(snapshot.outsider_deaths, [])

    def test_self_projection_clears_nearby_deaths_without_mutating_shared_snapshot(self):
        session = self.session([
            (10, "You have been slain by Guard Nicolas!"),
            (20, "Visitor has been slain by a rat!"),
        ])
        shared = session.snapshot(now=30)
        self.assertEqual(shared.outsider_deaths, [("Visitor", 1)])
        own = filter_session(shared, PLAYER)
        self.assertEqual(own.deaths_by_player, [(PLAYER, 1)])
        self.assertEqual(own.deaths, 1)
        self.assertEqual(own.outsider_deaths, [])
        self.assertEqual(shared.outsider_deaths, [("Visitor", 1)])
        self.assertEqual(session.snapshot(now=31).outsider_deaths, [("Visitor", 1)])

    def test_party_self_and_carebear_projections_keep_their_existing_death_scope(self):
        session = self.session([
            (10, "Your party member Friend has been slain by Guard Nicolas!"),
            (20, "You have been slain by Guard Xavier!"),
            (30, "Visitor has been slain by a rat!"),
        ])
        shared = session.snapshot(now=40)
        self.assertEqual(dict(shared.deaths_by_player), {"Friend": 1, PLAYER: 1})
        self.assertEqual(shared.outsider_deaths, [("Visitor", 1)])
        grouped = project_session(shared, Config(
            player_name=PLAYER, casual_mode=False, casual_mode_confirmed=True,
        ))
        self.assertEqual(dict(grouped.deaths_by_player), {"Friend": 1, PLAYER: 1})
        self.assertEqual(grouped.outsider_deaths, [])
        self.assertEqual(grouped.kills, 0)
        own = filter_session(shared, PLAYER)
        self.assertEqual(own.deaths_by_player, [(PLAYER, 1)])
        self.assertEqual(own.outsider_deaths, [])
        casual = project_session(shared, Config(player_name=PLAYER))
        self.assertEqual(casual.deaths_by_player, [(PLAYER, 1)])
        self.assertEqual(casual.outsider_deaths, [])
        own_casual = filter_session(casual, PLAYER)
        self.assertEqual(own_casual.deaths_by_player, [(PLAYER, 1)])
        self.assertEqual(own_casual.outsider_deaths, [])
        self.assertEqual(shared.outsider_deaths, [("Visitor", 1)])


if __name__ == "__main__":
    unittest.main()

"""Session final blows follow pet ownership without losing raw correction evidence."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from mnmparse.config import Config
from mnmparse.grammar import Event
from mnmparse.parser import parse_line
from mnmparse.party import PartyRoster
from mnmparse.privacy import project_session
from mnmparse.session import SessionStats, filter_session
from mnmparse.session_archive import DiskDetails, SessionArchive


PLAYER = "Maergoth"
PEER = "Tamsin"
OUTSIDER = "Septimal"


class SessionPetKillTests(unittest.TestCase):
    def make_session(self) -> tuple[SessionStats, PartyRoster]:
        roster = PartyRoster(PLAYER)
        roster.set_manual(PEER, True)
        return SessionStats(PLAYER, started=100, roster=roster), roster

    def feed(self, session: SessionStats, text: str, ts: float = 101):
        event = parse_line(text, ts, PLAYER)
        session.add(event)
        return event

    def project_group(self, snap):
        return project_session(snap, Config(player_name=PLAYER, casual_mode=False,
                                            casual_mode_confirmed=True))

    def test_assigned_own_group_and_outsider_pets_follow_owners(self) -> None:
        session, roster = self.make_session()
        for pet, owner in (("Sprout", PLAYER), ("Briar", PEER), ("Thorn", OUTSIDER),
                           ("a charmed rat", PLAYER)):
            roster.set_pet_owner(pet, owner)
        for ts, text in enumerate((
                "a rat has been slain by Sprout!",
                "a bat has been slain by Briar!",
                "a wolf has been slain by Thorn!",
                "Grandmaster Obadiah has been slain by a charmed rat!"), 101):
            self.feed(session, text, ts)

        raw = session.snapshot(now=200)
        self.assertEqual((raw.kills, raw.outsider_kills, raw.deaths), (3, 1, 0))
        self.assertEqual(dict(raw.kills_by_killer), {PLAYER: 2, PEER: 1})
        self.assertEqual(raw.party, [PEER], "Pets must not become extra party members")
        self.assertEqual(self.project_group(raw).kills, 3)
        own = filter_session(raw, PLAYER)
        self.assertEqual(own.kills, 2)
        self.assertEqual(dict(own.kills_by_target), {"a rat": 1, "Grandmaster Obadiah": 1})

        casual = project_session(raw, Config(player_name=PLAYER))
        self.assertEqual(casual.kills, 2, "Keep the existing own-share Carebear policy")
        self.assertEqual(casual.kills_by_killer, [(PLAYER, 2)])
        exported = json.dumps(asdict(casual))
        for hidden in (PEER, OUTSIDER, "Sprout", "Briar", "Thorn", "a charmed rat"):
            self.assertNotIn(hidden, exported)

    def test_late_assignment_and_reassignment_rebuild_existing_kills(self) -> None:
        session, roster = self.make_session()
        self.feed(session, "a rat has been slain by Sprout!")
        unknown = session.snapshot(now=110)
        self.assertEqual((unknown.kills, unknown.outsider_kills), (0, 1))

        roster.set_pet_owner("Sprout", PLAYER)
        mine = session.snapshot(now=111)
        self.assertGreater(mine.revision, unknown.revision)
        self.assertEqual((mine.kills, mine.outsider_kills, filter_session(mine, PLAYER).kills), (1, 0, 1))
        self.assertEqual(mine.kills_by_killer, [(PLAYER, 1)])

        roster.set_pet_owner("Sprout", PEER)
        peer = session.snapshot(now=112)
        self.assertGreater(peer.revision, mine.revision)
        self.assertEqual((peer.kills, peer.outsider_kills, filter_session(peer, PLAYER).kills), (1, 0, 0))
        self.assertEqual(peer.kills_by_killer, [(PEER, 1)])

        roster.set_manual(PEER, False)
        departed = session.snapshot(now=113)
        self.assertEqual((departed.kills, departed.outsider_kills), (0, 1))
        roster.set_manual(PEER, True)
        self.assertEqual(session.snapshot(now=114).kills, 1)

        roster.set_pet_owner("Sprout", OUTSIDER)
        outside = session.snapshot(now=115)
        self.assertEqual((outside.kills, outside.outsider_kills), (0, 1))
        roster.set_pet_owner("Sprout", None)
        self.assertEqual(session.snapshot(now=116).kills, 0)
        self.assertEqual(len(session._slain), 1, "Reclassification must not append duplicate kills")

    def test_bounded_details_keep_complete_owner_and_personal_totals(self) -> None:
        session, roster = self.make_session()
        roster.set_pet_owner("Sprout", PLAYER)
        roster.set_pet_owner("Briar", PEER)
        for index, pet in enumerate(("Sprout",) * 3 + ("Briar",) * 2):
            self.feed(session, f"a rat has been slain by {pet}!", 101 + index)
        snap = session.snapshot(now=200, detail_limit=1)
        self.assertEqual((snap.kills, len(snap.kill_entries)), (5, 1))
        self.assertEqual(snap.kill_entries[0][1], PEER)
        self.assertEqual(dict(snap.kills_by_killer), {PLAYER: 3, PEER: 2})
        self.assertEqual(self.project_group(snap).kills, 5)
        mine = filter_session(snap, PLAYER)
        self.assertEqual((mine.kills, mine.kill_entries, mine.kills_by_target), (3, [], [("a rat", 3)]))
        casual = project_session(snap, Config(player_name=PLAYER))
        self.assertEqual((casual.kills, casual.kill_entries), (3, []))

    def test_your_pet_combat_evidence_attributes_generic_final_blow_retroactively(self) -> None:
        session, _roster = self.make_session()
        self.feed(session, "a rat has been slain by Sprout!", 101)
        self.assertEqual(session.snapshot(now=102).kills, 0)
        pet_action = self.feed(session, "Your pet Sprout slashes a bat for 12 points of damage.", 103)
        final_blow = self.feed(session, "a bat has been slain by Sprout!", 104)
        self.assertTrue(pet_action.is_pet)
        self.assertFalse(final_blow.is_pet)
        snap = session.snapshot(now=110)
        self.assertEqual((snap.kills, snap.outsider_kills), (2, 0))
        self.assertEqual(filter_session(snap, PLAYER).kills, 2)
        self.assertEqual(snap.kills_by_killer, [(PLAYER, 2)])

    def test_explicit_your_pet_final_blow_is_own_kill(self) -> None:
        session, _roster = self.make_session()
        event = self.feed(session, "Your pet Sprout has slain a rat!")
        self.assertEqual(event.kind, "kill")
        self.assertTrue(event.is_pet)
        snap = session.snapshot(now=110)
        self.assertEqual((snap.kills, snap.outsider_kills), (1, 0))
        self.assertEqual(filter_session(snap, PLAYER).kills, 1)

    def test_manual_owner_overrides_inferred_own_pet(self) -> None:
        session, roster = self.make_session()
        self.feed(session, "Your pet Sprout slashes a rat for 12 points of damage.", 101)
        self.feed(session, "a rat has been slain by Sprout!", 102)
        self.assertEqual(filter_session(session.snapshot(now=103), PLAYER).kills, 1)
        roster.set_pet_owner("Sprout", PEER)
        corrected = session.snapshot(now=104)
        self.assertEqual(corrected.kills_by_killer, [(PEER, 1)])
        self.assertEqual((corrected.kills, filter_session(corrected, PLAYER).kills), (1, 0))
        roster.set_pet_owner("Sprout", OUTSIDER)
        outside = session.snapshot(now=105)
        self.assertEqual((outside.kills, outside.outsider_kills), (0, 1))

    def test_ownership_matching_uses_roster_spelling_and_ignores_case(self) -> None:
        session, roster = self.make_session()
        roster.set_pet_owner("Sprout", "TAMSIN")
        self.feed(session, "a rat has been slain by SPROUT!")
        snap = session.snapshot(now=110)
        self.assertEqual((snap.kills, snap.outsider_kills), (1, 0))
        self.assertEqual(snap.kills_by_killer, [(PEER, 1)])
        self.assertEqual(self.project_group(snap).kills, 1)

    def test_known_pet_victims_do_not_inflate_kills_or_player_deaths(self) -> None:
        session, roster = self.make_session()
        roster.set_pet_owner("a charmed rat", PLAYER)
        roster.set_pet_owner("Briar", PEER)
        for ts, text in enumerate((
                "a charmed rat has been slain by a wolf!",
                "Briar has been slain by a wolf!",
                "Briar has been slain by Tamsin!"), 101):
            self.feed(session, text, ts)
        snap = session.snapshot(now=110)
        self.assertEqual((snap.kills, snap.deaths, snap.outsider_kills), (0, 0, 0))
        self.assertEqual(snap.outsider_deaths, [])

    def test_similar_owner_and_pet_names_keep_player_death_distinct(self) -> None:
        for owner in (PEER, PEER.upper(), PEER.lower()):
            with self.subTest(owner=owner):
                session, roster = self.make_session()
                roster.set_pet_owner("Tamsln", PEER)
                owner_death = f"{owner} has been slain by a wolf!"
                if owner.islower():
                    # Lowercase legacy/structured identities need the same protection;
                    # the current text grammar intentionally requires capitalized names.
                    session.add(Event(101, "kill", owner_death, actor="a wolf", target=owner))
                else:
                    event = self.feed(session, owner_death, 101)
                    self.assertEqual(event.kind, "kill")
                for ts, text in enumerate((
                        "Tamsln has been slain by a wolf!",
                        "a rat has been slain by Tamsln!"), 102):
                    self.feed(session, text, ts)
                snap = session.snapshot(now=110)
                self.assertEqual((snap.kills, snap.deaths, snap.outsider_kills), (1, 1, 0))
                self.assertEqual(snap.kills_by_killer, [(PEER, 1)])
                self.assertEqual(snap.deaths_by_player, [(PEER, 1)])
                self.assertEqual(snap.outsider_deaths, [])

    def test_group_pet_killing_viewer_is_player_death(self) -> None:
        session, roster = self.make_session()
        roster.set_pet_owner("Briar", PEER)
        self.feed(session, f"{PLAYER} has been slain by Briar!")
        snap = session.snapshot(now=110)
        self.assertEqual((snap.kills, snap.deaths, snap.outsider_kills), (0, 1, 0))
        self.assertEqual(snap.deaths_by_player, [(PLAYER, 1)])
        self.assertEqual(snap.kills_by_killer, [])
        self.assertEqual(filter_session(snap, PLAYER).deaths, 1)

    def test_passive_own_pet_death_never_teaches_killer_as_pet(self) -> None:
        for passive in ("Your pet Sprout has been slain by a wolf!",
                        "Your pet Sprout has been stain bv a wolf!",
                        "Your pet Sprout has been slain bv a wolf!"):
            with self.subTest(passive=passive):
                session, _roster = self.make_session()
                event = self.feed(session, passive, 101)
                self.assertEqual(event.kind, "kill")
                self.assertTrue(event.is_pet)
                self.assertEqual((event.actor, event.target), ("a wolf", "Sprout"))
                pet_death = session.snapshot(now=102)
                self.assertEqual((pet_death.kills, pet_death.deaths, pet_death.outsider_kills), (0, 0, 0))
                self.assertEqual(pet_death.outsider_deaths, [])

                self.feed(session, f"{PLAYER} has been slain by a wolf!", 103)
                self.feed(session, "a rat has been slain by a wolf!", 104)
                snap = session.snapshot(now=110)
                self.assertEqual((snap.kills, snap.deaths, snap.outsider_kills), (0, 1, 1))
                self.assertEqual(snap.deaths_by_player, [(PLAYER, 1)])
                self.assertEqual(snap.kills_by_killer, [])

    def test_own_pet_killing_party_member_is_player_death(self) -> None:
        session, roster = self.make_session()
        roster.set_pet_owner("Sprout", PLAYER)
        event = self.feed(session, f"Your party member {PEER} has been slain by Sprout!")
        self.assertEqual((event.kind, event.actor, event.target), ("kill", "Sprout", PEER))
        snap = session.snapshot(now=110)
        self.assertEqual((snap.kills, snap.deaths, snap.outsider_kills), (0, 1, 0))
        self.assertEqual(snap.deaths_by_player, [(PEER, 1)])
        self.assertEqual(snap.kills_by_killer, [])

    def test_disk_recovery_keeps_legacy_slain_rows_and_late_pet_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = SessionArchive(Path(directory) / "sessions.sqlite3")
            session, roster = self.make_session()
            roster.set_pet_owner("Sprout", PLAYER)
            roster.set_pet_owner("Briar", PEER)
            archive.attach_details(session, "pets")
            # The four-field format already exists in released archives.
            session._slain.append((101.0, "Sprout", "a rat", False))
            session._slain.append((102.0, "Briar", "a bat", False))
            session._slain.append((103.0, "Thorn", "a wolf", False))
            archive.save_session("pets", session, ended=110)
            # A released archive has no inferred-pet metadata in its state.
            with closing(sqlite3.connect(archive.path)) as db, db:
                state = json.loads(db.execute("SELECT state FROM sessions WHERE id='pets'").fetchone()[0])
                state.pop("_observed_pet_owners", None)
                db.execute("UPDATE sessions SET state=? WHERE id='pets'", (json.dumps(state),))

            restored = archive.restore("pets")
            self.assertIsInstance(restored._slain, DiskDetails)
            snap = restored.snapshot(now=110, detail_limit=1)
            self.assertEqual((snap.kills, snap.outsider_kills, len(snap.kill_entries)), (2, 1, 1))
            self.assertEqual(filter_session(snap, PLAYER).kills, 1)
            restored.roster.set_pet_owner("Thorn", PLAYER)
            corrected = restored.snapshot(now=111, detail_limit=1)
            self.assertEqual((corrected.kills, corrected.outsider_kills), (3, 0))
            self.assertEqual(filter_session(corrected, PLAYER).kills, 2)
            archive.save_session("pets", restored, ended=111)
            preview = archive.preview("pets", detail_limit=1)
            self.assertEqual((preview.kills, filter_session(preview, PLAYER).kills), (3, 2))
            self.assertEqual(archive.restore("pets").snapshot(now=111).kills, 3)

    def test_inferred_own_pet_identity_survives_archive_and_further_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = SessionArchive(Path(directory) / "sessions.sqlite3")
            session, _roster = self.make_session()
            archive.attach_details(session, "pets")
            self.feed(session, "Your pet Sprout slashes a rat for 12 points of damage.", 101)
            self.feed(session, "a rat has been slain by Sprout!", 102)
            archive.save_session("pets", session, ended=110)

            restored = archive.restore("pets")
            self.feed(restored, "a bat has been slain by Sprout!", 111)
            self.feed(restored, "Your pet Fobaf slashes a wolf for 12 points of damage.", 112)
            self.feed(restored, "a wolf has been slain by Fobaf!", 113)
            snap = restored.snapshot(now=120, detail_limit=1)
            self.assertEqual((snap.kills, snap.outsider_kills, filter_session(snap, PLAYER).kills), (3, 0, 3))
            archive.save_session("pets", restored, ended=120)
            self.assertEqual(archive.restore("pets").snapshot(now=120).kills, 3)


if __name__ == "__main__":
    unittest.main()

"""Confirmed context releases OCR bang look-alikes without weakening tracking."""
from __future__ import annotations

import json
import unittest

from mnmparse.tracker import Tracker
from mnmparse.trigger_presets import RESIST_ID
from mnmparse.triggers import TriggerStore
from tests.test_tracker import frame, scrolled


CAST = "You begin casting Beset the Dead."
RESIST = "a gorged ghoul resists your Beset the Deadl"
BASELINE = ["You have entered Night Harbor (East).", "Fixture begins casting Lesser Healing.",
            "You pierce a gorged ghoul for 10 damage.",
            "Stopped attacking."]


class Chat:
    """The public tracker receives one unchanged/scrolling OCR view every 250 ms."""

    def __init__(self, extra_backlog=()):
        self.tracker = Tracker()
        self.history = [*BASELINE, *extra_backlog]
        self.now = 100.0
        self.feed()
        self.backlog = self.feed()

    def feed(self):
        self.now += .25
        return self.tracker.update(frame(scrolled(self.history, size=10)), self.now)

    def append(self, *texts):
        self.history.extend(texts)
        return self.feed()


class ContextualTerminalTrackingTests(unittest.TestCase):
    def learn(self, chat, text=CAST):
        self.assertEqual(chat.append(text), [], "Context requires the usual two trustworthy reads")
        messages = chat.feed()
        self.assertEqual([message.text for message in messages], [text])
        self.assertEqual(messages[0].frames_seen, 2)
        self.assertFalse(messages[0].backlog)
        return messages[0]

    def test_observed_deadl_resist_is_delivered_on_second_read_at_four_fps(self):
        chat = Chat()
        triggers = TriggerStore()
        triggers.install_presets()
        self.learn(chat)
        self.assertEqual(chat.append(RESIST), [])
        first_seen = chat.now
        messages = chat.feed()
        self.assertEqual([message.text for message in messages], [RESIST])
        message = messages[0]
        self.assertEqual(chat.now - first_seen, .25)
        self.assertEqual(message.first_seen, first_seen)
        self.assertEqual(message.frames_seen, 2)
        self.assertTrue(message.fragment, "Contextual completion must retain the existing degraded-evidence flag")
        self.assertFalse(message.backlog)
        # Keep the original OCR text for diagnostics and downstream parsing.
        self.assertEqual(message.text, "a gorged ghoul resists your Beset the Deadl")
        # Match the shipped default without constructing a runner or audio object.
        matches = triggers.matches(message.text)
        self.assertEqual([match.trigger.id for match in matches], [RESIST_ID])
        self.assertEqual(matches[0].trigger.speech, "resist")
        for _ in range(24):
            self.assertEqual(chat.feed(), [], "The old join timeout must not replay the result")
        self.assertEqual(chat.tracker.flush(chat.now), [])

    def test_second_identical_resist_is_a_new_occurrence_after_two_new_reads(self):
        chat = Chat()
        self.learn(chat)
        self.assertEqual(chat.append(RESIST), [])
        first = chat.feed()
        self.assertEqual([message.text for message in first], [RESIST])
        for _ in range(4):
            self.assertEqual(chat.feed(), [])
        self.learn(chat)
        # The first identical resist remains on screen above the second cast.
        self.assertEqual(chat.append(RESIST), [])
        first_seen = chat.now
        second = chat.feed()
        self.assertEqual([message.text for message in second], [RESIST])
        self.assertEqual(second[0].frames_seen, 2)
        self.assertEqual(second[0].first_seen, first_seen)
        self.assertGreater(second[0].first_seen, first[0].first_seen)
        for _ in range(12):
            self.assertEqual(chat.feed(), [])

    def test_correct_and_contextual_terminal_variants_do_not_duplicate_one_row(self):
        chat = Chat()
        self.learn(chat)
        self.assertEqual(chat.append(RESIST), [])
        messages = chat.feed()
        self.assertEqual([message.text for message in messages], [RESIST])
        for terminal in ("!", "l", "!", "I", "!", "1", "!", "|"):
            chat.history[-1] = RESIST[:-1] + terminal
            self.assertEqual(chat.feed(), [], "OCR punctuation jitter must remain the same visual row")
        self.assertEqual(chat.tracker.flush(chat.now), [])

    def test_cast_backlog_does_not_authorize_a_new_terminal_repair(self):
        chat = Chat(extra_backlog=(CAST,))
        self.assertTrue(any(message.text == CAST and message.backlog for message in chat.backlog))
        self.assertEqual(chat.append(RESIST), [])
        self.assertEqual(chat.feed(), [], "An old startup cast is not trusted live context")

    def test_unconfirmed_cast_does_not_authorize_a_terminal_repair(self):
        chat = Chat()
        self.assertEqual(chat.append(CAST), [])
        # Move the cast away before a second reading. Keep the earlier baseline
        # aligned so this is not an unrelated full-window jump or occlusion.
        chat.history[-1] = "Stopped attacking."
        chat.feed()
        chat.feed()
        self.assertEqual(chat.append(RESIST), [])
        self.assertEqual(chat.feed(), [], "A single observed cast must not become terminal evidence")

    def test_flush_of_one_read_cast_does_not_train_terminal_context(self):
        chat = Chat()
        self.assertEqual(chat.append(CAST), [])
        flushed = chat.tracker.flush(chat.now)
        self.assertEqual([message.text for message in flushed], [CAST])
        self.assertEqual(flushed[0].frames_seen, 1)
        self.assertEqual(chat.append(RESIST), [])
        self.assertEqual(chat.feed(), [], "Forced emission does not supply a second trustworthy reading")

    def test_joined_cast_is_not_used_as_literal_single_row_context(self):
        chat = Chat()
        head = "You begin casting Binding of the"
        self.assertEqual(chat.append(head, "Dead."), [])
        self.assertEqual([message.text for message in chat.feed()], [head + " Dead."])
        raw = "a gorged ghoul resists your Binding of the Deadl"
        self.assertEqual(chat.append(raw), [])
        self.assertEqual(chat.feed(), [], "A reconstructed wrapped cast does not train literal terminal evidence")

    def test_unknown_spell_keeps_existing_wrap_timeout(self):
        chat = Chat()
        self.learn(chat)
        unknown = "a gorged ghoul resists your An Unobserved Spelll"
        self.assertEqual(chat.append(unknown), [])
        self.assertEqual(chat.feed(), [])
        for _ in range(8):
            self.assertEqual(chat.feed(), [])
        # The fix must not turn every final lowercase l into punctuation.
        self.assertEqual([message.text for message in chat.tracker.flush(chat.now)], [unknown])

    def test_known_longer_spell_and_roman_rank_are_not_shortened(self):
        for known, raw in (("Beset the Deadl", RESIST),
                           ("HealI", "a gorged ghoul resists your HealI"),
                           ("HealII", "a gorged ghoul resists your HealII"),
                           ("Heal II", "a gorged ghoul resists your Heal II")):
            with self.subTest(known=known):
                chat = Chat()
                self.learn(chat)
                self.learn(chat, "You begin casting Heal.")
                self.learn(chat, f"You begin casting {known}.")
                self.assertEqual(chat.append(raw), [])
                self.assertEqual(chat.feed(), [], "A known original spelling or rank is not an extra bang glyph")
                self.assertEqual([message.text for message in chat.tracker.flush(chat.now)], [raw])

    def test_real_l_ending_npc_name_is_not_given_a_terminal_repair(self):
        chat = Chat()
        self.learn(chat, "a jackal hits YOU for 10 points of damage.")
        raw = "Your party member Fixture has slain a jackal"
        self.assertEqual(chat.append(raw), [])
        self.assertEqual(chat.feed(), [])
        self.assertEqual([message.text for message in chat.tracker.flush(chat.now)], [raw])

    def test_known_npc_with_extra_bang_glyph_is_ready_without_renaming(self):
        chat = Chat()
        self.learn(chat, "Keeper of the Road hits YOU for 10 points of damage.")
        raw = "You have slain Keeper of the Roadl"
        self.assertEqual(chat.append(raw), [])
        messages = chat.feed()
        self.assertEqual([message.text for message in messages], [raw])
        self.assertEqual(messages[0].frames_seen, 2)
        self.assertTrue(messages[0].fragment)

    def test_wrapped_resist_still_joins_and_requires_both_confirmations(self):
        chat = Chat()
        self.learn(chat)
        head = "a gorged ghoul resists your Beset the"
        self.assertEqual(chat.append(head, "Deadl"), [])
        messages = chat.feed()
        self.assertEqual([message.text for message in messages], [RESIST])
        self.assertEqual(messages[0].frames_seen, 2)
        self.assertTrue(messages[0].fragment)
        for _ in range(8):
            self.assertEqual(chat.feed(), [])

    def test_real_wrapped_spell_and_arbitrary_chat_are_not_early_terminated(self):
        for first, second in (
            ("a gorged ghoul resists your Beset the Deadl", "of Greater Power!"),
            ("Fixture says, 'This harmless sentence ends in a jackal", "and continues on the next line.'"),
        ):
            with self.subTest(first=first):
                chat = Chat()
                # No matching live terminal context exists for either head.
                self.assertEqual(chat.append(first), [])
                self.assertEqual(chat.feed(), [])
                self.assertEqual(chat.append(second), [])
                messages = chat.feed()
                self.assertEqual([message.text for message in messages], [first + " " + second])
                self.assertEqual(messages[0].frames_seen, 2)

    def test_restart_history_suppresses_the_old_resist_and_allows_a_new_one(self):
        chat = Chat()
        self.learn(chat)
        self.assertEqual(chat.append(RESIST), [])
        self.assertEqual([message.text for message in chat.feed()], [RESIST])
        state = json.loads(json.dumps(chat.tracker.export_state(now=chat.now)))
        restarted = Tracker()
        self.assertTrue(restarted.import_state(state, now=chat.now + .25))
        chat.tracker = restarted
        for _ in range(8):
            self.assertEqual(chat.feed(), [], "Restarted on-screen content is already emitted history")
        self.learn(chat)
        self.assertEqual(chat.append(RESIST), [])
        messages = chat.feed()
        self.assertEqual([message.text for message in messages], [RESIST])
        self.assertEqual(messages[0].frames_seen, 2)
        self.assertFalse(messages[0].backlog)

    def test_restart_does_not_learn_terminal_context_from_saved_cast_history(self):
        chat = Chat()
        self.learn(chat)
        state = json.loads(json.dumps(chat.tracker.export_state(now=chat.now)))
        restarted = Tracker()
        self.assertTrue(restarted.import_state(state, now=chat.now + .25))
        chat.tracker = restarted
        for _ in range(4):
            self.assertEqual(chat.feed(), [])
        self.assertEqual(chat.append(RESIST), [])
        self.assertEqual(chat.feed(), [], "Saved row replay must not restore or train terminal context")


if __name__ == "__main__":
    unittest.main()

"""The copyable help recipes must work through the same matcher as live triggers."""
from __future__ import annotations

from html.parser import HTMLParser
import unittest

from mnmparse.app.trigger_help_content import HELP_EXAMPLES, TRIGGER_HELP_HTML
from mnmparse.triggers import Trigger, fill_placeholders, match_trigger


class TriggerHelpExamplesTests(unittest.TestCase):
    def test_every_documented_recipe_produces_its_expected_label(self):
        for example in HELP_EXAMPLES:
            with self.subTest(recipe=example["anchor"]):
                trigger = Trigger(name=example["name"], pattern=example["pattern"],
                                  mode=example["mode"], timer_label=example["label"])
                self.assertEqual(trigger.problems(), [])
                match = match_trigger(trigger, example["sample"])
                self.assertIsNotNone(match)
                self.assertEqual(fill_placeholders(trigger.timer_label, match.values()), example["expected"])
                self.assertNotIn("{", fill_placeholders(example["speech"], match.values()))

    def test_smite_recipe_rejects_other_ranks_and_other_players(self):
        example = next(e for e in HELP_EXAMPLES if e["anchor"] == "example-smite")
        trigger = Trigger(pattern=example["pattern"], mode=example["mode"])
        for line in (
            example["sample"].replace("Smite II", "Smite III"),
            example["sample"].replace("Smite II", "Smite"),
            example["sample"].replace("Your", "Someone's"),
            "Your Righteous Smite II was resisted.",
        ):
            with self.subTest(line=line):
                self.assertIsNone(match_trigger(trigger, line))

    def test_all_ranks_recipe_captures_base_spell_and_ranked_spells(self):
        example = next(e for e in HELP_EXAMPLES if e["anchor"] == "example-smite-all")
        trigger = Trigger(pattern=example["pattern"], mode=example["mode"])
        for suffix, damage in (("", "86"), (" II", "154"), (" III", "262")):
            with self.subTest(rank=suffix):
                spell = "Righteous Smite" + suffix
                match = match_trigger(trigger, f"Your {spell} hits a rat for {damage} points of Holy Damage.")
                self.assertEqual(match.groups["spell"], spell)
                self.assertEqual(match.groups["damage"], damage)
                self.assertEqual(match.groups["target"], "a rat")
                self.assertEqual(fill_placeholders(example["label"], match.values()),
                                 f"{spell}: {damage} on a rat")

    def test_rendered_code_preserves_literal_regex_backslashes(self):
        class CodeText(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_code = False
                self.parts = []

            def handle_starttag(self, tag, attrs):
                if tag == "code":
                    self.in_code = True
                    self.parts.append("")

            def handle_endtag(self, tag):
                if tag == "code":
                    self.in_code = False

            def handle_data(self, data):
                if self.in_code:
                    self.parts[-1] += data

        rendered = CodeText()
        rendered.feed(TRIGGER_HELP_HTML)
        for snippet in (r"\d+", r"[\d,]+", r"\s+", r"\.", r"(?P<damage>\d+)"):
            self.assertIn(snippet, rendered.parts)
        for example in HELP_EXAMPLES:
            self.assertIn(example["pattern"], rendered.parts)
            self.assertIn(example["label"], rendered.parts)


if __name__ == "__main__":
    unittest.main()

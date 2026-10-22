"""NPC reference extraction and revision cache behavior, without live wiki access."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from mnmparse.npc_wiki import (
    ItemDetails, NpcDetails, NotableNpcs, WikiDataError, WikiRepository,
    parse_item, parse_npc, parse_notables, wiki_url,
)
from mnmparse import npc_wiki


# Representative MediaWiki structures. These fictional records deliberately do
# not claim to be current game data while the community wiki is unavailable.
NPC_HTML = '''<div class="mw-parser-output">
<table class="infobox"><tbody>
<tr><th>Level range</th><td>12–15</td></tr>
<tr><th>Race</th><td>Goblin</td></tr></tbody></table>
<h2>Loot <span class="mw-editsection">[edit]</span></h2>
<h3>Common</h3><table><tr><th>Item</th><th>Chance</th></tr>
<tr><td><a href="/wiki/Cloth_Scraps">Cloth Scraps</a></td><td>Common</td></tr></table>
<ul><li><a href="/wiki/Rusty_Blade">rusty blade</a></li></ul>
<h3>Unique</h3><ul><li><a href="/wiki/Test_Signet">Test Signet</a> (Rare)</li></ul>
<h2>Required by quests</h2><ul><li><a href="/wiki/A_Fictional_Quest">A Fictional Quest</a></li></ul>
<h2>Reputation gained</h2><ul><li>+5 City Watch</li></ul>
<h2>Reputation lost</h2><ul><li>−10 Test Bandits</li></ul>
<h2>Location</h2><p><a href="/wiki/Night_Harbor">Night Harbor</a></p>
</div>'''

ITEM_HTML = '''<h1>Staff of Sigils</h1><div class="mw-parser-output">
<p>Staff of Sigils<br><img src="/icon.png"><br>MAGIC<br>
Slot: PRIMARY SECONDARY<br><b>Weapon DMG:</b> 15 &nbsp; <b>ATK Delay:</b> 33 &nbsp; <b>Skill:</b> BLG<br>
INT: +6 CHA: +2<br>Effect:<br><a href="/wiki/Runeskin_II">Runeskin II</a> (On Click. Any Slot. Cast Time: 10s. Level: 30)<br>
Weight: 7.5 &nbsp; Size: MEDIUM<br>Class: ENC<br>Race: ALL</p>
<h2>Dropped by</h2><ul><li><a href="/wiki/A_test_npc">A test npc</a></li></ul></div>'''

NOTABLE_HTML = '''<div class="mw-parser-output">
<h2>Notable NPCs</h2><table class="wikitable">
<tr><th>Level</th><th>NPC</th><th>Loot</th></tr>
<tr><td>12</td><td><a href="/wiki/Test_Keeper">Test Keeper</a></td><td><a href="/wiki/Test_Signet">Test Signet</a></td></tr>
</table><h3>More names</h3><ul>
<li><a href="/wiki/A_test_captain">A test captain</a> drops <a href="/wiki/Test_Blade">Test Blade</a></li>
</ul><h2>Common NPCs</h2><ul><li><a href="/wiki/A_rat">A rat</a></li></ul></div>'''


def revision(title="Test Keeper", number=42, *, touched="2026-10-20T10:00:00Z"):
    return {"query": {"pages": [{"pageid": 123, "ns": 0, "title": title, "touched": touched, "revisions": [{"revid": number}]}]}}


def parsed(html=NPC_HTML, title="Test Keeper", number=42):
    return {"parse": {"title": title, "revid": number, "text": html}}


class WikiParserTests(unittest.TestCase):
    def test_npc_uses_infobox_sections_and_separates_loot(self):
        details = parse_npc(NPC_HTML, "Test Keeper")
        self.assertEqual(details.level, "12–15")
        self.assertEqual(details.common_drops, ("Cloth Scraps", "Rusty Blade"))
        self.assertEqual(details.unique_drops, ("Test Signet",))
        self.assertEqual(details.quests, ("A Fictional Quest",))
        self.assertEqual(details.reputation, ("Gained: +5 City Watch", "Lost: −10 Test Bandits"))
        self.assertEqual(details.source, wiki_url("Test Keeper"))

    def test_npc_labelled_rows_and_paragraphs(self):
        html = '''<table><tr><th>Level:</th><td>8-9</td></tr>
        <tr><th>Common drops</th><td><a href="/wiki/Cloth">Cloth</a></td></tr>
        <tr><th>Unique loot</th><td><a href="/wiki/Ring">Ring</a></td></tr>
        <tr><th>Faction hits</th><td>+3 Guardians<br>-5 Bandits</td></tr></table>
        <p>Quests: <a href="/wiki/Local_Quest">Local Quest</a></p>'''
        details = parse_npc(html, "Example")
        self.assertEqual(details.level, "8-9")
        self.assertEqual(details.unique_drops, ("Ring",))
        self.assertEqual(details.reputation, ("+3 Guardians", "-5 Bandits"))
        self.assertEqual(details.quests, ("Local Quest",))

    def test_mob_variants_keep_stats_and_reputation_scoped(self):
        # Current rendered mob templates use td labels in mob-stats-box, and
        # repeat independent sections for each zone on the same wiki page.
        html = '''<div class="mw-parser-output">
        <div class="mob-page-wrapper wiki-mob-variant" data-zone="Test Marsh">
        <table class="mob-stats-box"><tbody>
        <tr><th colspan="2">a test bat</th></tr>
        <tr><td class="mob-label">Level</td><td><span class="mob-value">3</span></td></tr>
        <tr><td class="mob-label">Zone</td><td><a href="/wiki/Test_Marsh">Test Marsh</a></td></tr>
        </tbody></table>
        <h3 class="mob-loot-header">Unique Loot</h3><div class="mob-loot-list"><ul>
        <li><a href="/wiki/Test_Fur">Test Fur</a></li></ul></div>
        <h3 class="mob-loot-header-common">Common Loot</h3><ul>
        <li><a href="/wiki/Test_Tooth">Test Tooth</a></li></ul>
        <h3 class="mob-loot-header-quest">Quest Loot</h3><ul>
        <li><a href="/wiki/Test_Wing">Test Wing</a></li></ul>
        <h3 class="mob-factions-header">Reputation Lost</h3><ul><li>None</li></ul>
        <h3 class="mob-opposing-factions-header">Reputation Gained</h3><ul><li>None</li></ul>
        </div>
        <div class="mob-page-wrapper wiki-mob-variant" data-zone="Test Cave">
        <table class="mob-stats-box"><tbody>
        <tr><td class="mob-label">Level</td><td>5–7</td></tr>
        <tr><td class="mob-label">Zone</td><td><a href="/wiki/Test_Cave">Test Cave</a></td></tr>
        </tbody></table>
        <h3>Unique Loot</h3><ul><li><a href="/wiki/Test_Fur">Test Fur</a></li>
        <li><a href="/wiki/Test_Claw">Test Claw</a></li></ul>
        <h3>Reputation Lost</h3><ul><li>Test Bandits</li></ul>
        <h3>Reputation Gained</h3><ul><li>Test Wardens</li></ul>
        </div><p><a href="/wiki/Unrelated_Page">Unrelated page</a></p>
        <h2>Required by quests</h2><ul><li><a href="/wiki/Test_Quest">Test Quest</a></li></ul>
        </div>'''
        details = parse_npc(html, "a test bat")
        self.assertEqual(details.level, "3 / 5–7")
        self.assertEqual(details.common_drops, ("Test Tooth",))
        self.assertEqual(details.unique_drops, ("Test Fur", "Test Claw"))
        self.assertEqual(details.quests, ("Test Quest",))
        self.assertEqual(details.reputation, ("Lost: Test Bandits", "Gained: Test Wardens"))

    def test_quest_loot_is_not_a_required_quest(self):
        details = parse_npc('''<p>Level: 1</p><h3>Quest Loot</h3><ul>
        <li><a href="/wiki/Test_Token">Test Token</a></li></ul>''', "Example")
        self.assertEqual(details.quests, ())
        self.assertEqual(details.unique_drops, ())
        self.assertEqual(details.common_drops, ())

    def test_quest_table_retains_quest_name_and_ignores_level_column(self):
        html = '''<h2>Required by quests</h2><table>
        <tr><th>Quest</th><th>Level</th></tr>
        <tr><td><a href="/wiki/The_Quest">The Quest</a></td><td>5</td></tr>
        </table>'''
        details = parse_npc(html, "Test Keeper")
        self.assertEqual(details.quests, ("The Quest",))
        self.assertEqual(details.level, "")

    def test_drop_table_names_do_not_become_record_labels(self):
        html = '''<h2>Unique drops</h2><table>
        <tr><th>Item</th><th>Chance</th></tr>
        <tr><td><a href="/wiki/Quest_Signet">Quest Signet</a></td><td>Rare</td></tr>
        <tr><td><a href="/wiki/Faction_Token">Faction Token</a></td><td>Common</td></tr>
        <tr><th>Level</th><td>Very Rare</td></tr>
        </table>'''
        details = parse_npc(html, "Test Keeper")
        self.assertEqual(details.unique_drops, ("Quest Signet", "Faction Token", "Level"))
        self.assertEqual(details.quests, ())
        self.assertEqual(details.reputation, ())
        self.assertEqual(details.level, "")

    def test_section_tables_follow_name_column_and_preserve_plain_names(self):
        html = '''<h2>Common drops</h2><table>
        <tr><th>Chance</th><th>Item</th><th>Notes</th></tr>
        <tr><td>Common</td><td>Quest Token</td><td><a href="/wiki/Other_Mob">Other Mob</a> drops it too</td></tr>
        </table><h2>Related quests</h2><table>
        <tr><th>Level</th><th>Quest</th></tr>
        <tr><td>5</td><td><a href="/wiki/The_Quest">The Quest</a></td></tr>
        </table>'''
        details = parse_npc(html, "Test Keeper")
        self.assertEqual(details.common_drops, ("Quest Token",))
        self.assertEqual(details.quests, ("The Quest",))

    def test_infobox_fields_override_section_context_only_in_actual_infobox(self):
        html = '''<h2>Unique drops</h2><table class="npc-infobox">
        <tr><td>Level</td><td>17</td></tr>
        <tr><th>Quests</th><td><a href="/wiki/Test_Quest">Test Quest</a></td></tr>
        <tr><th>Factions</th><td>+5 City Watch</td></tr>
        <tr><th>Common drops</th><td><a href="/wiki/Cloth">Cloth</a></td></tr>
        </table>'''
        details = parse_npc(html, "Test Keeper")
        self.assertEqual(details.level, "17")
        self.assertEqual(details.quests, ("Test Quest",))
        self.assertEqual(details.reputation, ("+5 City Watch",))
        self.assertEqual(details.common_drops, ("Cloth",))
        self.assertEqual(details.unique_drops, ())

    def test_unknown_loot_is_not_claimed_to_be_unique(self):
        details = parse_npc('<p>Level: 7</p><h2>Loot</h2><p><a href="/wiki/Cloth">Cloth</a></p>', "Example")
        self.assertEqual(details.common_drops, ())
        self.assertEqual(details.unique_drops, ())

    def test_unrelated_subheading_stops_loot_but_equipment_categories_inherit(self):
        html = '''<h2>Unique drops</h2><h3>Weapons</h3>
        <ul><li><a href="/wiki/Test_Blade">Test Blade</a></li></ul>
        <h3>Spawn notes</h3><p><a href="/wiki/A_rat">A rat</a> is a placeholder.</p>
        <h2>Quests</h2><ul><li><a href="/wiki/Test_Quest">Test Quest</a></li></ul>
        <h3>Dialogue</h3><p>A long conversation goes here.</p>'''
        details = parse_npc(html, "Example")
        self.assertEqual(details.unique_drops, ("Test Blade",))
        self.assertEqual(details.quests, ("Test Quest",))

    def test_absent_sections_are_unknown_and_explicit_empty_is_allowed(self):
        self.assertEqual(parse_npc("<p>Level: 7</p>", "Example"), NpcDetails("Example", "7", source=wiki_url("Example")))
        self.assertEqual(parse_npc("<h2>Unique drops</h2><p>None known</p>", "Example").unique_drops, ())
        for html in ("", "<p>Service Unavailable</p>", "<p>This page does not exist</p>"):
            with self.subTest(html=html), self.assertRaises(WikiDataError):
                parse_npc(html, "Example")

    def test_npc_ignores_page_navigation_and_external_links(self):
        html = '''<nav><h2>Unique drops</h2><a href="/wiki/Bad">Bad</a></nav>
        <div class="mw-parser-output"><p>Level: 1</p><h2>Unique drops</h2><ul>
        <li><a href="/wiki/Good">Good</a></li>
        <li><a href="https://example.com/wiki/External">External</a></li></ul></div>'''
        details = parse_npc(html, "Example")
        self.assertEqual(details.unique_drops, ("Good",))
        self.assertNotIn("Bad", details.unique_drops)

    def test_item_stats_match_supplied_wiki_example_as_plain_text(self):
        details = parse_item(ITEM_HTML, "Staff of Sigils")
        self.assertEqual(details.lines, (
            "MAGIC", "Slot: PRIMARY SECONDARY", "Weapon DMG: 15 ATK Delay: 33 Skill: BLG",
            "INT: +6 CHA: +2", "Effect: Runeskin II (On Click. Any Slot. Cast Time: 10s. Level: 30)",
            "Weight: 7.5 Size: MEDIUM", "Class: ENC", "Race: ALL",
        ))
        self.assertTrue(all("<" not in line for line in details.lines))

    def test_item_infobox_and_multiple_flags(self):
        details = parse_item('''<div class="mw-parser-output"><p>MAGIC LORE NO DROP</p>
        <table><tr><th>Slot</th><td>CHEST</td></tr><tr><th>AC</th><td>36</td></tr>
        <tr><th>STR</th><td>+3</td></tr><tr><th>Weight</th><td>5.5</td></tr></table></div>''', "Test Breastplate")
        self.assertEqual(details.lines, ("MAGIC LORE NO DROP", "Slot: CHEST", "AC: 36", "STR: +3", "Weight: 5.5"))
        with self.assertRaises(WikiDataError):
            parse_item("<p>Temporarily unavailable</p>", "Test Breastplate")

    def test_notable_table_uses_npc_column_and_stops_at_next_section(self):
        details = parse_notables(NOTABLE_HTML, "Night Harbor")
        self.assertEqual(details.names, ("Test Keeper", "A test captain"))
        self.assertEqual(parse_notables("<h2>Notable NPCs</h2><p>None known</p>", "Test Zone").names, ())
        with self.assertRaises(WikiDataError):
            parse_notables('<h2>NPCs</h2><a href="/wiki/A_rat">A rat</a>', "Test Zone")

    def test_notables_labelled_row_inside_nested_layout_tables(self):
        html = '''<div class="mw-parser-output"><table><tbody><tr><td>
        <table class="zoneTopTable"><tbody><tr><td><table class="zoneTopTable">
        <tr><th><b>Level:</b></th><td>1–50</td></tr>
        <tr><th><b>Notable NPCs:</b></th><td>
        <a href="/wiki/Test_Keeper">Test Keeper</a>,
        <a href="/wiki/A_test_captain">a test captain</a></td></tr>
        <tr><th>Unique Items:</th><td><a href="/wiki/Test_Signet">Test Signet</a></td></tr>
        <tr><th>Related Quests:</th><td><a href="/wiki/Test_Quest">Test Quest</a></td></tr>
        </table></td></tr></tbody></table></td>
        <td><a href="/wiki/Map_Key">Map Key</a></td></tr></tbody></table>
        <h3>NPCs</h3><ul><li><a href="/wiki/A_common_bat">A common bat</a></li></ul></div>'''
        self.assertEqual(parse_notables(html, "Test Zone").names, ("Test Keeper", "A test captain"))
        empty = '<table><tr><th>Notable NPCs:</th><td>None known</td></tr></table>'
        self.assertEqual(parse_notables(empty, "Test Zone").names, ())

    def test_named_npc_table_header_selects_names_without_adding_stat_headers(self):
        html = '''<table><tr><th>Named NPCs</th><th>Race</th><th>Class</th>
        <th>Level</th><th>Location</th><th>Known Loot</th><th>Description</th></tr>
        <tr><td><a href="/wiki/Test_Keeper">Test Keeper</a></td><td>Human</td>
        <td>Warrior</td><td>10</td><td><a href="/wiki/Test_Tower">Test Tower</a></td>
        <td><a href="/wiki/Test_Signet">Test Signet</a></td><td>Example</td></tr>
        <tr><th>Common NPCs</th><th>Level</th></tr>
        <tr><td><a href="/wiki/A_common_bat">A common bat</a></td><td>1</td></tr></table>'''
        self.assertEqual(parse_notables(html, "Test Zone").names, ("Test Keeper",))
        self.assertEqual(parse_notables('<h2>Notable NPCs</h2>' + html, "Test Zone").names, ("Test Keeper",))

    def test_titles_are_encoded_and_do_not_become_paths(self):
        self.assertTrue(wiki_url("Test Keeper's Ring").endswith("Test_Keeper%27s_Ring"))
        with tempfile.TemporaryDirectory() as temp:
            path = WikiRepository(Path(temp))._path("npc", "../../Name?")
            self.assertEqual(path.parent, Path(temp))
            self.assertNotIn("?", path.name)


class WikiCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = WikiRepository(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def seed_npc(self):
        with patch.object(npc_wiki, "_api", side_effect=[revision(), parsed()]):
            return self.repo.refresh_npc("Test Keeper")

    def test_cache_reads_work_after_restart_without_network(self):
        expected = self.seed_npc()
        other = WikiRepository(self.root)
        with patch.object(npc_wiki, "_api", side_effect=AssertionError("cache reads must be offline")):
            self.assertEqual(other.cached_npc("test_keeper"), expected)
            self.assertIsNone(other.cached_npc("Not Published"))
            self.assertIsNone(other.cached_item("Not Published"))
            self.assertIsNone(other.cached_notables("Not Published"))

    def test_same_revision_does_not_transfer_rendered_page_again(self):
        expected = self.seed_npc()
        with patch.object(npc_wiki, "_api", return_value=revision()) as api:
            self.assertEqual(self.repo.refresh_npc("Test Keeper"), expected)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[0]["action"], "query")

    def test_old_parser_cache_stays_readable_but_reparses_unchanged_wiki_once(self):
        expected = self.seed_npc()
        path = self.repo._path("npc", "Test Keeper")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope.pop("parser_version")
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with patch.object(npc_wiki, "_api", side_effect=AssertionError("cache reads must remain offline")):
            self.assertEqual(self.repo.cached_npc("Test Keeper"), expected)
        before = path.read_bytes()
        with patch.object(npc_wiki, "_api", side_effect=URLError("offline")):
            with self.assertRaises(OSError):
                self.repo.refresh_npc("Test Keeper")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.repo.cached_npc("Test Keeper"), expected)
        changed = NPC_HTML.replace("12–15", "17")
        with patch.object(npc_wiki, "_api", side_effect=[revision(), parsed(changed)]) as api:
            refreshed = self.repo.refresh_npc("Test Keeper")
        self.assertEqual(api.call_count, 2)
        self.assertEqual(refreshed.level, "17")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["parser_version"], npc_wiki.PARSER_VERSION)
        with patch.object(npc_wiki, "_api", return_value=revision()) as api:
            self.assertEqual(self.repo.refresh_npc("Test Keeper"), refreshed)
        self.assertEqual(api.call_count, 1)

    def test_canonical_only_cache_serves_consider_article_offline(self):
        expected = self.seed_npc()
        with patch.object(npc_wiki, "_api", side_effect=AssertionError("cache reads must be offline")):
            self.assertEqual(self.repo.cached_npc("a Test Keeper"), expected)
            self.assertEqual(self.repo.cached_npc("an Test Keeper"), expected)

    def test_template_touch_with_same_revision_refreshes_rendered_content(self):
        self.seed_npc()
        changed = NPC_HTML.replace("12–15", "13–16")
        with patch.object(npc_wiki, "_api", side_effect=[revision(touched="2026-10-21T12:00:00Z"), parsed(changed)]) as api:
            self.assertEqual(self.repo.refresh_npc("Test Keeper").level, "13–16")
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args.args[0]["page"], "Test Keeper")
        self.assertEqual(api.call_args.args[0]["prop"], "text|revid")
        self.assertNotIn("oldid", api.call_args.args[0])

    def test_parse_requests_revision_and_rejects_text_only_response(self):
        with patch.object(npc_wiki, "_api", side_effect=[revision(), {"parse": {"title": "Test Keeper", "text": NPC_HTML}}]) as api:
            with self.assertRaises(WikiDataError):
                self.repo.refresh_npc("Test Keeper")
        self.assertEqual(api.call_args.args[0]["prop"], "text|revid")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_first_letter_normalization_preserves_rest_of_npc_title(self):
        meta = revision("A test bat")
        meta["query"]["normalized"] = [{"from": "a test bat", "to": "A test bat"}]
        with patch.object(npc_wiki, "_api", side_effect=[meta, parsed(title="A test bat")]) as api:
            details = self.repo.refresh_npc("a test bat")
        self.assertEqual(details.name, "A test bat")
        self.assertEqual(api.call_args.args[0]["page"], "A test bat")

    def test_changed_revision_updates_atomic_disk_record(self):
        self.seed_npc()
        with patch.object(npc_wiki, "_api", side_effect=[revision(number=43), parsed(NPC_HTML.replace("12–15", "14"), number=43)]):
            details = self.repo.refresh_npc("Test Keeper")
        self.assertEqual(WikiRepository(self.root).cached_npc("Test Keeper"), details)
        self.assertEqual(details.level, "14")
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_article_fallback_redirect_and_canonical_alias_are_cached(self):
        meta = revision("Test Keeper")
        meta["query"]["pages"].append({"title": "A keeper", "missing": True, "ns": 0})
        meta["query"]["redirects"] = [{"from": "keeper", "to": "Test Keeper"}]
        with patch.object(npc_wiki, "_api", side_effect=[meta, parsed()]) as api:
            expected = self.repo.refresh_npc("a keeper")
        self.assertEqual(api.call_args_list[0].args[0]["titles"], "a keeper|keeper")
        self.assertEqual(self.repo.cached_npc("a keeper"), expected)
        self.assertEqual(self.repo.cached_npc("Test Keeper"), expected)

    def test_new_redirect_alias_saves_existing_canonical_without_page_download(self):
        expected = self.seed_npc()
        meta = revision()
        meta["query"]["redirects"] = [{"from": "Keeper Alias", "to": "Test Keeper"}]
        with patch.object(npc_wiki, "_api", return_value=meta) as api:
            self.assertEqual(self.repo.refresh_npc("Keeper Alias"), expected)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(self.repo.cached_npc("Keeper Alias"), expected)

    def test_outage_missing_page_or_bad_content_preserve_stale_bytes(self):
        expected = self.seed_npc()
        path = self.repo._path("npc", "Test Keeper")
        before = path.read_bytes()
        failures = [
            [URLError("offline")],
            [{"query": {"pages": [{"title": "Test Keeper", "missing": True}]}}],
            [revision(number=43), parsed("<p>Service Unavailable</p>", number=43)],
            [revision(number=43), parsed(NPC_HTML, number=42)],
            [revision(number=43), {"parse": {"title": "Other NPC", "revid": 43, "text": NPC_HTML}}],
        ]
        for failure in failures:
            with self.subTest(failure=failure), patch.object(npc_wiki, "_api", side_effect=failure):
                with self.assertRaises((OSError, WikiDataError)):
                    self.repo.refresh_npc("Test Keeper")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.repo.cached_npc("Test Keeper"), expected)

    def test_failure_for_unknown_record_does_not_create_negative_cache(self):
        with patch.object(npc_wiki, "_api", return_value={"query": {"pages": [{"title": "Unknown", "missing": True}]}}):
            with self.assertRaises(WikiDataError):
                self.repo.refresh_npc("Unknown")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_missing_page_is_distinct_from_malformed_metadata(self):
        missing = {"query": {"pages": [{"title": "Unknown", "missing": True, "ns": 0}]}}
        with patch.object(npc_wiki, "_api", return_value=missing):
            with self.assertRaises(npc_wiki.WikiPageMissing):
                self.repo.refresh_npc("Unknown")
        article_missing = {"query": {"pages": [{"title": "A Unknown", "missing": True}, {"title": "Unknown", "missing": True}]}}
        with patch.object(npc_wiki, "_api", return_value=article_missing):
            with self.assertRaises(npc_wiki.WikiPageMissing):
                self.repo.refresh_npc("a Unknown")
        for response in ({"query": {}}, {"query": {"pages": []}}, {"query": {"pages": [{"title": "Other", "missing": True}]}}):
            with self.subTest(response=response), patch.object(npc_wiki, "_api", return_value=response):
                with self.assertRaises(WikiDataError) as caught:
                    self.repo.refresh_npc("Unknown")
                self.assertNotIsInstance(caught.exception, npc_wiki.WikiPageMissing)
        # Do not call an NPC unpublished if a requested articleless candidate
        # was omitted by a malformed response rather than confirmed missing.
        with patch.object(npc_wiki, "_api", return_value=missing):
            with self.assertRaises(WikiDataError) as caught:
                self.repo.refresh_npc("a Unknown")
        self.assertNotIsInstance(caught.exception, npc_wiki.WikiPageMissing)

    def test_old_api_response_shapes_are_supported(self):
        meta = revision()
        meta["query"]["pages"] = {"123": meta["query"]["pages"][0]}
        html = parsed()
        html["parse"]["text"] = {"*": NPC_HTML}
        with patch.object(npc_wiki, "_api", side_effect=[meta, html]):
            self.assertEqual(self.repo.refresh_npc("Test Keeper").level, "12–15")

    def test_item_and_notable_records_have_separate_durable_caches(self):
        with patch.object(npc_wiki, "_api", side_effect=[revision("Staff of Sigils"), parsed(ITEM_HTML, "Staff of Sigils")]):
            item = self.repo.refresh_item("Staff of Sigils")
        with patch.object(npc_wiki, "_api", side_effect=[revision("Night Harbor"), parsed(NOTABLE_HTML, "Night Harbor")]):
            notables = self.repo.refresh_notables("Night Harbor")
        other = WikiRepository(self.root)
        self.assertEqual(other.cached_item("Staff of Sigils"), item)
        self.assertEqual(other.cached_notables("Night Harbor"), notables)
        self.assertIsNone(other.cached_npc("Staff of Sigils"))

    def test_corrupt_or_untrusted_cache_is_ignored(self):
        path = self.repo._path("npc", "Test Keeper")
        path.write_text("{bad json", encoding="utf-8")
        self.assertIsNone(self.repo.cached_npc("Test Keeper"))
        self.seed_npc()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["data"]["source"] = "https://example.com/evil"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(self.repo.cached_npc("Test Keeper"))

    def test_failed_atomic_replace_preserves_old_cache_but_returns_fresh_data(self):
        old = self.seed_npc()
        with patch.object(npc_wiki, "_api", side_effect=[revision(number=43), parsed(NPC_HTML.replace("12–15", "14"), number=43)]), patch.object(npc_wiki.os, "replace", side_effect=OSError("disk full")), self.assertLogs("mnmparse.npc_wiki", level="WARNING"):
            self.assertEqual(self.repo.refresh_npc("Test Keeper").level, "14")
        self.assertEqual(self.repo.cached_npc("Test Keeper"), old)
        self.assertEqual(len(list(self.root.iterdir())), 1)


class WikiTransportTests(unittest.TestCase):
    def response(self, payload, *, url=npc_wiki.API, headers=None):
        stream = BytesIO(payload)
        stream.geturl = lambda: url
        stream.headers = headers or {}
        return stream

    def test_transport_has_bounded_timeout_and_payload(self):
        response = self.response(b'{"query": {}}')
        with patch.object(npc_wiki, "urlopen", return_value=response) as opener:
            self.assertEqual(npc_wiki._api({"action": "query"}), {"query": {}})
        self.assertEqual(opener.call_args.kwargs["timeout"], 8)
        with patch.object(npc_wiki, "urlopen", return_value=self.response(b"", headers={"Content-Length": str(npc_wiki.MAX_RESPONSE_BYTES + 1)})):
            with self.assertRaises(WikiDataError):
                npc_wiki._api({"action": "query"})

    def test_external_redirect_and_truncated_or_error_payload_are_rejected(self):
        for response in (
            self.response(b"{}", url="https://example.com/api.php"),
            self.response(b"{}", headers={"Content-Length": "20"}),
            self.response(b'{"error": {"code": "missingtitle"}}'),
        ):
            with self.subTest(response=response), patch.object(npc_wiki, "urlopen", return_value=response), self.assertRaises(WikiDataError):
                npc_wiki._api({"action": "query"})


if __name__ == "__main__":
    unittest.main()

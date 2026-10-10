"""Actual CLI output/export boundaries default to Carebear Mode."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mnmparse import cli
from mnmparse.config import Config, save_config
from mnmparse.parser import parse_line
from mnmparse.tracker import Message

PLAYER = "Pidef"
PEERS = ("Tamsin", "Wenna")


class CliPrivacyTests(unittest.TestCase):
    def fixture(self, directory: str, *, full: bool = False, confirmed: bool = True):
        folder = Path(directory)
        cfg = Config(player_name=PLAYER, log_dir=directory,
                     casual_mode=not full, casual_mode_confirmed=confirmed if full else False)
        config = folder / "config.json"
        save_config(cfg, config)
        source = folder / "sample.log"
        lines = ["Tamsin has joined the party.", "Wenna has joined the party.",
                 "You crush a rat for 10 points of damage.", "Tamsin crushes a rat for 20 points of damage.",
                 "Wenna crushes a rat for 30 points of damage.", "You have slain a rat!",
                 "Tamsin says something secret."]
        source.write_text("\n".join(f"[Mon Oct 05 12:00:0{i} 2026] {text}" for i, text in enumerate(lines)), encoding="utf-8")
        return cfg, config, source

    def test_parse_console_and_jsonl_withhold_peer_identity_and_exact_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _cfg, config, source = self.fixture(directory)
            export = Path(directory) / "safe.jsonl"
            args = cli.build_parser().parse_args(["parse", "--config", str(config), "--events", "--jsonl", str(export), str(source)])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.cmd_parse(args), 0)
            combined = output.getvalue() + export.read_text(encoding="utf-8")
            for peer in PEERS:
                self.assertNotIn(peer, combined)
            self.assertIn("Group average (3)", output.getvalue())
            self.assertIn(PLAYER, combined)
            self.assertNotIn("something secret", combined)
            rows = [json.loads(line) for line in export.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(rows)
            self.assertTrue(all(row["actor"] == PLAYER and "target" not in row for row in rows))

    def test_confirmed_full_parse_retains_original_actor_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _cfg, config, source = self.fixture(directory, full=True)
            args = cli.build_parser().parse_args(["parse", "--config", str(config), "--events", str(source)])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.cmd_parse(args), 0)
            self.assertIn("Tamsin", output.getvalue())
            self.assertIn("something secret", output.getvalue())

    def test_snapshot_requires_confirmed_full_before_capture_or_writes(self) -> None:
        for cfg in (Config(), Config(casual_mode=False, casual_mode_confirmed=False)):
            with self.subTest(cfg=cfg), patch.object(cli, "_load_cfg", return_value=cfg), \
                    patch.object(cli, "_require_window") as lookup, contextlib.redirect_stderr(io.StringIO()):
                args = cli.build_parser().parse_args(["snapshot", "--out", "unused.png"])
                self.assertEqual(cli.cmd_snapshot(args), cli.EXIT_USAGE)
                lookup.assert_not_called()

    def test_live_console_uses_structured_own_text_while_raw_capture_stays_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg, _config, _source = self.fixture(directory)
            session = cli._RunSession(cfg, Mock(), Mock(), show_stats=True, ui=None)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                session._handle_message(Message("Tamsin crushes a rat for 20 points of damage.", 100, 2))
                session._handle_message(Message("You crush Tamsin for 10 points of damage.", 101, 2))
                session.finish()
            self.assertNotIn("Tamsin", output.getvalue())
            self.assertIn("You dealt damage (10)", output.getvalue())
            logs = "".join(path.read_text(encoding="utf-8") for path in Path(directory).glob("combat_*.log"))
            self.assertIn("Tamsin", logs)


if __name__ == "__main__":
    unittest.main()

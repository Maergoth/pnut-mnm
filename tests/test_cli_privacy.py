"""Actual CLI output/export boundaries default to Carebear Mode."""
from __future__ import annotations

import contextlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_snapshot_reads_and_saves_input_in_carebear_without_confirmation(self) -> None:
        for cfg in (Config(), Config(casual_mode=False, casual_mode_confirmed=False)):
            with self.subTest(cfg=cfg):
                frame = SimpleNamespace(shape=(20, 30, 3))
                crop = SimpleNamespace(shape=(5, 8, 3))
                engine = SimpleNamespace(read=Mock(return_value=[
                    SimpleNamespace(y=1, x=2, h=3, text="Tamsin crushes a rat for 42 points of damage.")]))
                output = io.StringIO()
                with patch.object(cli, "_load_cfg", return_value=cfg), \
                        patch.object(cli, "_require_window", return_value=True) as lookup, \
                        patch.object(cli, "_make_engine_or_none", return_value=engine), \
                        patch.object(cli, "_grab_one_frame", return_value=frame) as grab, \
                        patch("mnmparse.capture.crop_frame", return_value=crop), \
                        patch("mnmparse.ocr.preprocess", return_value=crop), \
                        patch("cv2.imwrite", return_value=True) as write, \
                        contextlib.redirect_stdout(output):
                    args = cli.build_parser().parse_args(["snapshot", "--out", "unused.png"])
                    self.assertEqual(cli.cmd_snapshot(args), cli.EXIT_OK)
                lookup.assert_called_once_with(cfg)
                grab.assert_called_once_with(cfg)
                engine.read.assert_called_once_with(crop)
                write.assert_called_once_with("unused.png", crop)
                self.assertIn("Tamsin crushes a rat for 42", output.getvalue())

    def test_verbose_diagnostics_and_authored_config_are_not_scrubbed_by_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _cfg, config, _source = self.fixture(directory)
            output = io.StringIO()
            handler = logging.StreamHandler(output)
            handler.setLevel(logging.DEBUG)
            logger = logging.getLogger("mnmparse")
            previous_level = logger.level
            logger.setLevel(logging.DEBUG)
            self.addCleanup(logger.setLevel, previous_level)

            def diagnose(args):
                cli._load_cfg(args.config)
                cli.log.debug("OCR row: %s", "Tamsin 42")
                cli.log.warning("Capture diagnostic: %s", "crop outside frame")
                return cli.EXIT_OK

            with patch.object(cli, "_setup_logging"), \
                    patch.object(cli, "cmd_snapshot", side_effect=diagnose), \
                    patch.object(logging.getLogger(), "handlers", [handler]):
                self.assertEqual(cli.main(["snapshot", "--config", str(config), "--verbose"]), cli.EXIT_OK)
            text = output.getvalue()
            self.assertIn(PLAYER, text)
            self.assertIn(directory, text)
            self.assertIn("OCR row: Tamsin 42", text)
            self.assertIn("Capture diagnostic: crop outside frame", text)

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

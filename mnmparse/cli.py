"""Command-line interface for mnmparse (``python -m mnmparse <cmd>``).

Commands (SPEC section 6):

* ``calibrate``   capture one frame, open the crop picker, save the crop to config.json
* ``snapshot``    capture one frame, crop, OCR once, print the lines (debug aid)
* ``run``         live loop: capture -> crop -> preprocess -> OCR -> Tracker -> log + parser + stats
* ``parse``       re-parse existing ``combat_*.log`` files offline into events and stats
* ``replay``      feed a recorded OCR fixture through Tracker + parser (development tool)

Exit codes: 0 ok, 2 game window not found (or no frame arrived), 3 OCR engine unavailable,
1 usage error (bad file path).

This is the only module in the package that prints; everything else logs. The capture, OCR
and Tk modules are imported inside the command functions so that ``--help``, ``parse`` and
``replay`` start instantly and work on machines without the Windows-only packages.

Safety posture: this module never touches the game process. It only drives the read-only
capture and OCR modules and writes files under the project folder.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:
    import numpy as np

    from mnmparse.capture import FrameSource
    from mnmparse.config import Config
    from mnmparse.ocr import OcrEngine
    from mnmparse.parser import Event
    from mnmparse.stats import Encounter, Stats
    from mnmparse.tracker import Message

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_WINDOW = 2
EXIT_NO_OCR = 3

FIRST_FRAME_TIMEOUT_S = 5.0
"""How long ``run``/``snapshot``/``calibrate`` wait for the first captured frame."""

FRAME_POLL_S = 0.05
"""Polling interval while waiting for the first frame."""

LOG_LINE_RE = re.compile(r"^\[(?P<stamp>[^\]]+)\]\s?(?P<text>.*)$")
"""EQ-style raw log line: ``[Wed Oct 01 17:11:03 2026] <text>``."""

LOG_STAMP_FMT = "%a %b %d %H:%M:%S %Y"

DEFAULT_CONFIG_NAME = "config.json"


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    """Configure stderr logging: WARNING for third-party libs, INFO (or DEBUG with -v) for us."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("mnmparse").setLevel(logging.DEBUG if verbose else logging.INFO)


def _load_cfg(path: str | None) -> Config:
    """Load the configuration (defaults + JSON overrides) and log it at DEBUG."""
    from mnmparse.config import load_config

    cfg = load_config(path)
    from mnmparse.privacy import casual_enabled, safe_config_data
    log.debug("config: %s", safe_config_data(dataclasses.asdict(cfg)) if casual_enabled(cfg) else cfg)
    return cfg


def _hms(ts: float) -> str:
    """Format a POSIX timestamp as local ``HH:MM:SS``."""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _format_event(ev: Event) -> str:
    """One compact line describing a parsed event (for ``--events``)."""
    amount = "" if ev.amount is None else f" {ev.amount}"
    skill = f" [{ev.skill}]" if ev.skill else ""
    outcome = f" ({ev.outcome})" if ev.outcome else ""
    return f"    -> {ev.kind:13} {ev.actor or '-'} -> {ev.target or '-'}{amount}{skill}{outcome}"


def _note_encounter(stats: Stats, seen: list[Encounter]) -> None:
    """Remember every distinct encounter object the Stats aggregator has opened, in order."""
    enc = stats.current()
    if enc is not None and (not seen or seen[-1] is not enc):
        seen.append(enc)


def _render_encounter(stats: Stats, enc: Encounter, cfg: Config | None = None) -> str:
    """CLI output boundary: canonical numbers are projected before rendering."""
    from mnmparse.app.models import build_snapshot
    from mnmparse.privacy import casual_enabled, metric_available, project_encounter
    if not casual_enabled(cfg):
        return stats.render(enc)
    snap = project_encounter(build_snapshot(stats, enc, getattr(cfg, "player_name", "")), cfg)
    lines = [f"{snap.label} - {snap.duration:.1f}s", "Actor                          Damage       DPS     Heals   Utility"]
    for row in snap.rows:
        damage = f"{row.damage:9g}" if metric_available(row, "damage") else f"{'—':>9}"
        dps = f"{row.dps:9.2f}" if metric_available(row, "dps") else f"{'—':>9}"
        heals = f"{row.heals:9g}" if metric_available(row, "heals") else f"{'—':>9}"
        lines.append(f"{row.name:30} {damage} {dps} {heals} {row.utility:9g}")
    if not snap.rows:
        lines.append("No own-character data available.")
    return "\n".join(lines)


def _event_output(ev: Event, cfg: Config) -> str | None:
    from mnmparse.privacy import safe_event_text
    return safe_event_text(ev, cfg)


def _event_export(ev: Event, cfg: Config) -> dict | None:
    from mnmparse.privacy import casual_enabled
    if not casual_enabled(cfg):
        return dataclasses.asdict(ev)
    text = _event_output(ev, cfg)
    if text is None:
        return None
    # No raw text, target identities, skill strings or arbitrary parser outcomes
    # cross the Carebear export boundary.
    return dict(ts=ev.ts, kind=ev.kind, text=text, actor=cfg.player_name,
                amount=ev.amount, estimated=ev.estimated, estimated_ts=ev.estimated_ts)


def _print_encounters(stats: Stats, seen: list[Encounter], cfg: Config | None = None) -> None:
    """Render every encounter in ``seen`` as a console table."""
    if not seen:
        print("No encounters found.")
        return
    for i, enc in enumerate(seen, 1):
        zone = stats.zone_at(enc.start) if hasattr(stats, "zone_at") else ""
        where = f"  [{zone}]" if zone else ""
        print(f"\n--- Encounter {i}{where} ---")
        print(_render_encounter(stats, enc, cfg))


def _split_log_line(raw: str, fallback_ts: float) -> tuple[float, str]:
    """Split an EQ-style raw log line into (timestamp, text).

    Lines without a parsable stamp get ``fallback_ts`` (the previous line's time).
    """
    m = LOG_LINE_RE.match(raw)
    if m is None:
        return fallback_ts, raw
    try:
        ts = time.mktime(time.strptime(m.group("stamp"), LOG_STAMP_FMT))
    except (ValueError, OverflowError):
        ts = fallback_ts
    return ts, m.group("text")


# --------------------------------------------------------------------------------------
# Capture / OCR plumbing (read-only; see SPEC section 1)
# --------------------------------------------------------------------------------------


def _require_window(cfg: Config) -> bool:
    """Return True if the game window exists; otherwise print the exit-2 message."""
    from mnmparse.capture import find_game_window_info

    win = find_game_window_info(cfg.window_title)
    if win is None:
        print(
            f'Game window "{cfg.window_title}" not found. Is the game running? (exit 2)',
            file=sys.stderr,
        )
        return False
    log.info("game window %s rect=%s", win.describe(), win.rect)
    return True


def _wait_first_frame(source: FrameSource, timeout_s: float) -> np.ndarray | None:
    """Poll ``source.latest()`` until a frame arrives or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        frame = source.latest()
        if frame is not None:
            return frame
        time.sleep(FRAME_POLL_S)
    return None


def _no_frame_message() -> None:
    """Print the exit-2 message for 'window found but no frame arrived'."""
    print(
        f"The game window was found but no frame arrived within {FIRST_FRAME_TIMEOUT_S:.0f} s. "
        'If the game is minimized, restore it; otherwise try capture_backend "mss". (exit 2)',
        file=sys.stderr,
    )


def _grab_one_frame(cfg: Config) -> np.ndarray | None:
    """Start a frame source, wait for the first frame, stop the source, return the frame."""
    from mnmparse.capture import make_source

    source = make_source(cfg)
    source.start()
    try:
        frame = _wait_first_frame(source, FIRST_FRAME_TIMEOUT_S)
    finally:
        source.stop()
    if frame is None:
        _no_frame_message()
    return frame


def _make_engine_or_none(cfg: Config) -> OcrEngine | None:
    """Create the configured OCR engine; print the exit-3 message and return None on failure."""
    from mnmparse.ocr import make_engine

    try:
        return make_engine(cfg)
    except Exception as exc:  # any failure here means "engine unavailable"
        log.debug("make_engine failed", exc_info=True)
        print(f"OCR engine '{cfg.ocr_engine}' unavailable: {exc} (exit 3)", file=sys.stderr)
        return None


# --------------------------------------------------------------------------------------
# Optional plain Tk status window for `run --ui`
# --------------------------------------------------------------------------------------


class _RunWindow:
    """Plain Tk status window: a scrolling message box and a label with the stats table.

    This is an ordinary application window. It never stays on top of other windows, is never
    transparent and is never positioned over the game; it is updated only from the main loop
    via ``after()``.
    """

    MAX_LINES = 2000

    def __init__(self, title: str) -> None:
        import tkinter as tk
        from tkinter import scrolledtext

        self.root = tk.Tk()
        self.root.title(f"mnmparse - {title}")
        self.closed = False
        self._destroyed = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        mono = ("Consolas", 10)
        self.text = scrolledtext.ScrolledText(
            self.root, width=110, height=24, font=mono, state="disabled", wrap="word"
        )
        self.text.pack(fill="both", expand=True, padx=6, pady=(6, 3))
        self.stats_var = tk.StringVar(master=self.root, value="(no encounter yet)")
        tk.Label(
            self.root, textvariable=self.stats_var, font=mono, justify="left", anchor="w"
        ).pack(fill="x", padx=6, pady=(3, 6))

    @classmethod
    def try_create(cls, title: str) -> _RunWindow | None:
        """Create the window, or return None (with a console note) if Tk is unavailable."""
        try:
            return cls(title)
        except Exception as exc:  # TclError, ImportError, ...
            log.warning("UI unavailable: %s", exc)
            print(f"UI unavailable ({exc}); continuing without it.", file=sys.stderr)
            return None

    def append(self, line: str) -> None:
        """Append one message line to the scrolling text box."""
        if self.closed:
            return
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        excess = int(self.text.index("end-1c").split(".")[0]) - self.MAX_LINES
        if excess > 0:
            self.text.delete("1.0", f"{excess + 1}.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def set_stats(self, table: str) -> None:
        """Replace the stats label text."""
        if not self.closed:
            self.stats_var.set(table)

    def after(self, ms: int, fn: Callable[[], None]) -> None:
        """Schedule ``fn`` on the Tk event loop after ``ms`` milliseconds."""
        self.root.after(ms, fn)

    def mainloop(self) -> None:
        """Run the Tk event loop until the window is closed or ``quit`` is called."""
        self.root.mainloop()

    def destroy(self) -> None:
        """Tear the window down (idempotent)."""
        if self._destroyed:
            return
        self._destroyed = True
        try:
            self.root.destroy()
        except Exception:  # TclError if Tk already went away
            log.debug("Tk destroy failed", exc_info=True)

    def _on_close(self) -> None:
        self.closed = True
        self.root.quit()


# --------------------------------------------------------------------------------------
# Live run session
# --------------------------------------------------------------------------------------


class _RunSession:
    """One live run: owns the pipeline objects and performs one pipeline step per call.

    ``step()`` does: ``latest()`` -> ``crop_frame`` -> ``preprocess`` -> ``engine.read`` ->
    ``tracker.update`` and, for every newly completed message, raw log + parse + event log +
    stats + console (and UI) output. The stats table is printed every ``stats_interval_s``.
    """

    def __init__(
        self,
        cfg: Config,
        engine: OcrEngine,
        source: FrameSource,
        *,
        show_stats: bool,
        ui: _RunWindow | None,
    ) -> None:
        from mnmparse.logwriter import LogWriter
        from mnmparse.stats import Stats
        from mnmparse.tracker import Tracker

        self.cfg = cfg
        self.engine = engine
        self.source = source
        self.show_stats = show_stats
        self.ui = ui
        self.crop = tuple(cfg.crop)
        self.tracker = Tracker()
        self.stats = Stats(cfg.encounter_timeout_s, player_name=cfg.player_name)
        self.writer = LogWriter(
            cfg.log_dir,
            max_bytes=int(float(getattr(cfg, "log_max_mb", 5.0)) * 1_000_000),
            break_s=float(getattr(cfg, "log_break_minutes", 60.0)) * 60.0,
        )
        self.encounters: list[Encounter] = []
        self.next_stats_at = time.monotonic() + cfg.stats_interval_s
        self.frames = 0
        self.messages = 0
        self.backlog = 0  #: lines already on screen at start (not logged)

    def step(self) -> None:
        """Process the most recent frame (if any) and print the stats table when due."""
        frame = self.source.latest()
        if frame is not None:
            self._process_frame(frame)
        if self.show_stats and time.monotonic() >= self.next_stats_at:
            self.next_stats_at = time.monotonic() + self.cfg.stats_interval_s
            self._print_stats()

    def _process_frame(self, frame: np.ndarray) -> None:
        from mnmparse.capture import crop_frame
        from mnmparse.ocr import preprocess

        t0 = time.perf_counter()
        img = preprocess(crop_frame(frame, self.crop), self.cfg.preprocess, self.cfg.ocr_scale)
        t1 = time.perf_counter()
        lines = self.engine.read(img)
        t2 = time.perf_counter()
        now = time.time()
        new_messages = self.tracker.update(lines, now)
        t3 = time.perf_counter()
        self.frames += 1
        log.debug(
            "frame %d: preprocess %.1f ms, ocr %.1f ms (%d lines), tracker %.1f ms (%d new)",
            self.frames,
            (t1 - t0) * 1000,
            (t2 - t1) * 1000,
            len(lines),
            (t3 - t2) * 1000,
            len(new_messages),
        )
        for msg in new_messages:
            self._handle_message(msg)

    def _handle_message(self, msg: Message) -> None:
        from mnmparse.parser import parse_line

        if msg.backlog:
            # On screen before the first frame: an earlier run may have logged it, and its time
            # is unknown (the app does the same).
            self.backlog += 1
            log.debug("not logged (on screen at start): %s", msg.text)
            return
        self.writer.write_raw(msg)
        ev = parse_line(msg.text, msg.first_seen, self.cfg.player_name)
        ev.estimated_ts = bool(getattr(msg, "estimated_ts", False))
        self.writer.write_event(ev)
        self.stats.add(ev)
        _note_encounter(self.stats, self.encounters)
        self.messages += 1
        text = _event_output(ev, self.cfg)
        if text is None:
            return
        line = f"[{_hms(msg.first_seen)}] {text}"
        print(line, flush=True)
        if self.ui is not None:
            self.ui.append(line)

    def _print_stats(self) -> None:
        closed = self.stats.expire(time.time())
        if closed is not None:
            # The fight simply ended (no kill line): print its final table once.
            table = "FINAL " + _render_encounter(self.stats, closed, self.cfg)
            print(table, flush=True)
            if self.ui is not None:
                self.ui.set_stats(table)
            return
        enc = self.stats.current()
        if enc is None:
            return
        table = _render_encounter(self.stats, enc, self.cfg)
        print(table, flush=True)
        if self.ui is not None:
            self.ui.set_stats(table)

    def finish(self) -> None:
        """Flush the tracker, close the writers and print the final table."""
        try:
            for msg in self.tracker.flush(time.time()):
                self._handle_message(msg)
        finally:
            self.writer.close()
        print(f"{self.frames} frames processed, {self.messages} messages logged.")
        if self.backlog:
            log.info("%d lines already on screen at start were not logged", self.backlog)
        if not self.show_stats:
            return
        enc = self.stats.current() or (self.encounters[-1] if self.encounters else None)
        if enc is None:
            print("No encounters recorded.")
        else:
            print(_render_encounter(self.stats, enc, self.cfg))


def _install_break_handler() -> None:
    """Make Ctrl-Break behave like Ctrl-C (clean flush-and-exit) on Windows consoles.

    Python's default for SIGBREAK is to terminate immediately, which would skip the tracker
    flush and leave the log files unclosed; the handler raises KeyboardInterrupt instead.
    """
    import signal

    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:
        return

    def _raise_interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(sigbreak, _raise_interrupt)
    except (ValueError, OSError):  # not on the main thread, or unsupported
        log.debug("could not install SIGBREAK handler", exc_info=True)


def _run_plain(session: _RunSession, period_s: float, seconds: float | None = None) -> None:
    """Console-only loop: one ``step()`` every ``period_s`` seconds until Ctrl-C.

    With ``seconds`` set the loop ends on its own after that long (clean flush and
    final table, exactly as Ctrl-C would).
    """
    deadline = None if seconds is None else time.monotonic() + seconds
    while deadline is None or time.monotonic() < deadline:
        t0 = time.monotonic()
        session.step()
        delay = period_s - (time.monotonic() - t0)
        if delay > 0:
            time.sleep(delay)


def _run_with_ui(session: _RunSession, ui: _RunWindow, period_s: float) -> None:
    """Tk loop: ``step()`` is scheduled via ``after()`` until the window closes or Ctrl-C.

    Tkinter swallows exceptions raised inside ``after()`` callbacks (including a Ctrl-C that
    lands during the OCR call), which would silently stop the tick chain. So the tick captures
    any exception, stops the event loop, and the exception is re-raised here, which makes UI
    mode behave exactly like the console loop.
    """
    period_ms = max(1, int(period_s * 1000))
    failure: BaseException | None = None

    def tick() -> None:
        nonlocal failure
        if ui.closed:
            return
        t0 = time.monotonic()
        try:
            session.step()
        except BaseException as exc:  # KeyboardInterrupt included, on purpose
            failure = exc
            ui.root.quit()
            return
        spent_ms = int((time.monotonic() - t0) * 1000)
        ui.after(max(1, period_ms - spent_ms), tick)

    ui.after(1, tick)
    ui.mainloop()
    if failure is not None:
        raise failure


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Capture one frame, let the user drag the crop rectangle, save it to the config file."""
    cfg = _load_cfg(args.config)
    if not _require_window(cfg):
        return EXIT_NO_WINDOW
    frame = _grab_one_frame(cfg)
    if frame is None:
        return EXIT_NO_WINDOW

    from mnmparse.calibrate import pick_crop
    from mnmparse.config import save_config

    h, w = frame.shape[:2]
    print(f"Captured a {w}x{h} frame; current crop is {tuple(cfg.crop)}.")
    print(
        "A normal window titled 'mnmparse - calibrate' opened (Alt-Tab to it if the game "
        "hides it). Drag a rectangle around the Combat window text; Enter/OK saves, Esc cancels."
    )
    crop = pick_crop(frame, tuple(cfg.crop))
    if crop is None:
        print("Calibration cancelled; config unchanged.")
        return EXIT_OK
    cfg.crop = crop
    save_config(cfg, args.config)
    print(f"Saved crop {crop} to {args.config or DEFAULT_CONFIG_NAME}.")
    return EXIT_OK


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Capture one frame, crop it, OCR it once and print the recognised lines."""
    cfg = _load_cfg(args.config)
    from mnmparse.privacy import casual_enabled
    if casual_enabled(cfg):
        print("Raw screenshots and OCR diagnostics require confirmed full mode in Morality Adjustment.", file=sys.stderr)
        return EXIT_USAGE
    if not _require_window(cfg):
        return EXIT_NO_WINDOW
    engine = _make_engine_or_none(cfg)
    if engine is None:
        return EXIT_NO_OCR
    frame = _grab_one_frame(cfg)
    if frame is None:
        return EXIT_NO_WINDOW

    from mnmparse.capture import crop_frame
    from mnmparse.ocr import preprocess

    crop = crop_frame(frame, tuple(cfg.crop))
    if args.out:
        import cv2

        cv2.imwrite(args.out, crop)
        print(f"Saved cropped frame to {args.out}")
    t0 = time.perf_counter()
    lines = engine.read(preprocess(crop, cfg.preprocess, cfg.ocr_scale))
    ms = (time.perf_counter() - t0) * 1000
    print(
        f"Frame {frame.shape[1]}x{frame.shape[0]}, crop {tuple(cfg.crop)} -> "
        f"{crop.shape[1]}x{crop.shape[0]}, {cfg.ocr_engine} OCR ({cfg.preprocess}, "
        f"x{cfg.ocr_scale:g}) {ms:.1f} ms, {len(lines)} lines"
    )
    for ln in lines:
        print(f"  y={ln.y:4d} x={ln.x:4d} h={ln.h:3d}  {ln.text}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    """Live loop: capture -> crop -> preprocess -> OCR -> Tracker -> log + parser + stats."""
    cfg = _load_cfg(args.config)
    if not _require_window(cfg):
        return EXIT_NO_WINDOW
    engine = _make_engine_or_none(cfg)
    if engine is None:
        return EXIT_NO_OCR

    from mnmparse.capture import make_source

    _install_break_handler()
    source = make_source(cfg)
    source.start()
    ui: _RunWindow | None = None
    try:
        first = _wait_first_frame(source, FIRST_FRAME_TIMEOUT_S)
        if first is None:
            _no_frame_message()
            return EXIT_NO_WINDOW
        if args.ui:
            ui = _RunWindow.try_create(cfg.window_title)
        print(
            f"Capturing {first.shape[1]}x{first.shape[0]} via {cfg.capture_backend} at "
            f"{cfg.fps:g} fps, crop {tuple(cfg.crop)}, OCR {cfg.ocr_engine}; "
            f"logs in {cfg.log_dir}/. Ctrl-C to stop."
        )
        session = _RunSession(cfg, engine, source, show_stats=not args.no_stats, ui=ui)
        period_s = 1.0 / cfg.fps if cfg.fps > 0 else 0.25
        try:
            if ui is not None:
                _run_with_ui(session, ui, period_s)
            else:
                _run_plain(session, period_s, getattr(args, "seconds", None))
        except KeyboardInterrupt:
            print("\nStopping (Ctrl-C).")
        session.finish()
    finally:
        source.stop()
        if ui is not None:
            ui.destroy()
    return EXIT_OK


def cmd_parse(args: argparse.Namespace) -> int:
    """Re-parse existing EQ-style ``combat_*.log`` files into events and encounter stats.

    Several files are imported together as one stretch of play (``importer.import_files``:
    the party goes on from file to file, and lines a restart of the app logged twice are left
    out); the results are printed per file, in time order.
    """
    cfg = _load_cfg(args.config)
    paths = [Path(name) for name in args.file]
    for path in paths:
        if not path.is_file():
            print(f"File not found: {path}", file=sys.stderr)
            return EXIT_USAGE

    from mnmparse.importer import import_file, import_files
    from mnmparse.session import format_coin
    from mnmparse.privacy import casual_enabled, project_session

    options = dict(
        player_name=cfg.player_name,
        encounter_timeout_s=cfg.encounter_timeout_s,
        include_personal=bool(getattr(cfg, "include_personal", False)),
        keep_events=bool(args.events or args.jsonl),
    )
    results = import_files(paths, **options) if len(paths) > 1 else [import_file(paths[0], **options)]
    for result in results:
        if args.jsonl:
            with open(args.jsonl, "a", encoding="utf-8") as jsonl:
                for ev in result.events:
                    exported = _event_export(ev, cfg)
                    if exported is not None:
                        jsonl.write(json.dumps(exported) + "\n")
            print(f"Events appended to {args.jsonl}")
        if args.events:
            for ev in result.events:
                text = _event_output(ev, cfg)
                if text is not None:
                    print(f"[{_hms(ev.ts)}] {text}")
                    if not casual_enabled(cfg):
                        print(_format_event(ev))
        counts = Counter(result.counts)
        summary = ("Carebear Mode: own-character output" if casual_enabled(cfg) else
                   ", ".join(f"{kind}={cnt}" for kind, cnt in counts.most_common()))
        dropped = f" ({result.backlog_dropped} repeated at a restart left out)" if result.backlog_dropped else ""
        print(f"{result.path} ({result.kind}): {result.messages} lines{dropped} -> {summary or 'no events'}")
        _print_encounters(result.stats, list(result.stats.history), cfg)
        s = project_session(result.session, cfg)
        print(
            f"Session: {s.items} items looted, {format_coin(s.coin_total)} coin, {s.crafts} crafted, "
            f"{s.kills} kills, {s.deaths} deaths, {s.cc_total} CC landed, {len(result.encounters)} encounters"
        )
        if s.group_averages:
            print("Group average: " + ", ".join(f"{key}={value:g}" for key, value in s.group_averages.items()))
    return EXIT_OK


def cmd_replay(args: argparse.Namespace) -> int:
    """Feed a recorded OCR fixture (SPEC section 5) through Tracker + parser + Stats."""
    cfg = _load_cfg(args.config)
    path = Path(args.fixture)
    if not path.is_file():
        print(f"Fixture not found: {path}", file=sys.stderr)
        return EXIT_USAGE

    from mnmparse.ocr import OcrLine
    from mnmparse.parser import parse_line
    from mnmparse.stats import Stats
    from mnmparse.tracker import Tracker
    from mnmparse.privacy import casual_enabled

    data = json.loads(path.read_text(encoding="utf-8"))
    frames = data.get("frames", [])
    print(f"Replaying {len(frames)} frames from {path} (crop {data.get('crop')})")
    tracker = Tracker()
    stats = Stats(cfg.encounter_timeout_s, player_name=cfg.player_name)
    encounters: list[Encounter] = []
    emitted = 0

    def handle(msg: Message) -> None:
        nonlocal emitted
        emitted += 1
        ev = parse_line(msg.text, msg.first_seen, cfg.player_name)
        ev.estimated_ts = bool(getattr(msg, "estimated_ts", False))
        stats.add(ev)
        _note_encounter(stats, encounters)
        text = _event_output(ev, cfg)
        if text is not None:
            print(f"[+{msg.first_seen:7.3f}s] {text}")
            if args.events and not casual_enabled(cfg):
                print(_format_event(ev))

    now = 0.0
    for fr in frames:
        now = float(fr["t_ms"]) / 1000.0
        lines = [
            OcrLine(x=int(ln["x"]), y=int(ln["y"]), h=int(ln["h"]), text=str(ln["text"]))
            for ln in fr["lines"]
        ]
        for msg in tracker.update(lines, now):
            handle(msg)
    for msg in tracker.flush(now):
        handle(msg)
    print(f"\n{emitted} messages emitted from {len(frames)} frames.")
    _print_encounters(stats, encounters, cfg)
    return EXIT_OK


# --------------------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------------------


def _version_string() -> str:
    """``mnmparse <version>`` from the package, tolerant of a missing ``__init__``."""
    try:
        from mnmparse import __version__
    except Exception:  # package metadata missing during development
        return "mnmparse (unknown version)"
    return f"mnmparse {__version__}"


CONFIG_HELP = "config JSON file (default: config.json next to the package)"
VERBOSE_HELP = "DEBUG logging (per-frame OCR timing etc.)"


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with one subcommand per SPEC section 6 command."""
    # Options accepted both before and after the subcommand. SUPPRESS keeps a subparser from
    # overwriting a value given before the subcommand with its own default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help=CONFIG_HELP)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=VERBOSE_HELP
    )

    p = argparse.ArgumentParser(
        prog="python -m mnmparse",
        description="Monsters & Memories combat parser: reads the in-game Combat chat window off "
        "the screen (read-only window capture + OCR), writes an EQ-style combat log and "
        "computes DPS / hit statistics.",
        epilog="Exit codes: 0 ok, 2 game window not found / no frame, 3 OCR engine unavailable.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", metavar="PATH", default=None, help=CONFIG_HELP)
    p.add_argument("-v", "--verbose", action="store_true", default=False, help=VERBOSE_HELP)
    p.add_argument("--version", action="version", version=_version_string())
    sub = p.add_subparsers(dest="command", metavar="<cmd>", required=True)

    sp = sub.add_parser(
        "calibrate",
        parents=[common],
        help="capture one frame, pick the Combat-window crop in a normal window, save it",
    )
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser(
        "snapshot",
        parents=[common],
        help="capture one frame, crop, OCR once and print the lines (debug)",
    )
    sp.add_argument("--out", metavar="FILE", help="also save the cropped frame as an image (PNG)")
    sp.set_defaults(func=cmd_snapshot)

    sp = sub.add_parser(
        "run",
        parents=[common],
        help="live loop: capture -> OCR -> tracker -> log + parser + stats (Ctrl-C stops)",
    )
    sp.add_argument("--no-stats", action="store_true", help="do not print the stats table")
    sp.add_argument(
        "--ui",
        action="store_true",
        help="also open a plain Tk window (normal window, no overlay) with messages and stats",
    )
    sp.add_argument(
        "--seconds",
        type=float,
        default=None,
        metavar="N",
        help="stop automatically after N seconds (console mode only; same clean exit as Ctrl-C)",
    )
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser(
        "parse",
        parents=[common],
        help="re-parse existing logs/combat_*.log files offline into events + stats",
    )
    sp.add_argument(
        "file", metavar="FILE", nargs="+",
        help="EQ-style combat log written by `run` (several: imported together as one stretch of play)",
    )
    sp.add_argument("--events", action="store_true", help="print every parsed event")
    sp.add_argument("--jsonl", metavar="PATH", help="also append the events to this JSONL file")
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser(
        "replay",
        parents=[common],
        help="feed a recorded OCR fixture JSON through Tracker + parser + stats (dev tool)",
    )
    sp.add_argument("fixture", metavar="FIXTURE", help="e.g. tests/fixtures/burst_ocr.json")
    sp.add_argument("--events", action="store_true", help="print every parsed event")
    sp.set_defaults(func=cmd_replay)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: parse arguments, configure logging, dispatch; returns the exit code."""
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    from mnmparse.privacy import casual_enabled
    cfg = _load_cfg(args.config)
    class PrivacyLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if not casual_enabled(cfg) or not record.name.startswith("mnmparse"):
                return True
            if record.levelno < logging.WARNING:
                return False  # parser/tracker diagnostic arguments may include raw chat
            record.msg = "PNUT reported a diagnostic warning; technical details are withheld in Carebear Mode."
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
            return True
    privacy_filter = PrivacyLogFilter()
    handlers = list(logging.getLogger().handlers)
    for handler in handlers:
        handler.addFilter(privacy_filter)
    func: Callable[[argparse.Namespace], int] = args.func
    try:
        return func(args)
    finally:
        for handler in handlers:
            handler.removeFilter(privacy_filter)


if __name__ == "__main__":
    sys.exit(main())

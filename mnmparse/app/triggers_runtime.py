"""Running the triggers: match every chat line, play the cue, keep the timers.

:class:`AudioOut` plays the built-in cues (small .wav files generated on first use under
``assets/sounds``), sound files (QMediaPlayer, so .mp3/.ogg work too) and speech
(QTextToSpeech: the Windows voices).  :class:`TriggerRunner` owns the :class:`TriggerStore`
and the :class:`TimerBoard`; the app feeds it every chat line through :meth:`observe`.
Everything runs in the GUI thread.
"""

from __future__ import annotations

import logging
import math
import struct
import time
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

from mnmparse.config import project_path
from mnmparse.triggers import BUILTIN_SOUNDS, ActiveTimer, Match, TimerBoard, Trigger, TriggerStore, fill_placeholders, match_trigger

log = logging.getLogger(__name__)

__all__ = ["AudioOut", "TriggerRunner", "builtin_sound_path"]

SAMPLE_RATE = 22050
TICK_MS = 100


# --------------------------------------------------------------------------- built-in cues
def _tone(freqs: list[tuple[float, float]], *, gap: float = 0.0, decay: float = 6.0, harmonics: int = 1) -> list[float]:
    """Samples for a sequence of ``(frequency, seconds)`` notes with a soft attack and decay."""
    out: list[float] = []
    for freq, dur in freqs:
        n = int(SAMPLE_RATE * dur)
        for i in range(n):
            t = i / SAMPLE_RATE
            env = min(1.0, t / 0.005) * math.exp(-decay * t)
            v = sum(math.sin(2 * math.pi * freq * k * t) / k for k in range(1, harmonics + 1))
            out.append(env * v / max(1, harmonics) * 0.9)
        out.extend([0.0] * int(SAMPLE_RATE * gap))
    return out


def _glide(f0: float, f1: float, dur: float) -> list[float]:
    n = int(SAMPLE_RATE * dur)
    out, phase = [], 0.0
    for i in range(n):
        t = i / n
        phase += 2 * math.pi * (f0 + (f1 - f0) * t) / SAMPLE_RATE
        env = min(1.0, i / (0.01 * SAMPLE_RATE)) * (1.0 - t) ** 0.5
        out.append(math.sin(phase) * env * 0.8)
    return out


def _dink() -> list[float]:
    """A soft glass "dink": a high note with two quickly fading inharmonic partials."""
    n = int(SAMPLE_RATE * 0.45)
    f = 1568.0  # G6
    out = []
    for i in range(n):
        t = i / SAMPLE_RATE
        attack = min(1.0, t / 0.004)
        v = (math.sin(2 * math.pi * f * t) * math.exp(-9.0 * t)
             + 0.28 * math.sin(2 * math.pi * f * 2.76 * t) * math.exp(-22.0 * t)
             + 0.10 * math.sin(2 * math.pi * f * 5.40 * t) * math.exp(-40.0 * t))
        out.append(attack * v * 0.45)
    return out


_CUES = {
    "Chime": lambda: _tone([(784, 0.18), (1175, 0.45)], decay=5),
    "Dink": _dink,
    "Alert": lambda: _tone([(988, 0.12)] * 3, gap=0.06, decay=2),
    "Bell": lambda: _tone([(660, 1.1)], decay=3, harmonics=4),
    "Beep": lambda: _tone([(1000, 0.18)], decay=1),
    "Low tone": lambda: _tone([(220, 0.6)], decay=3, harmonics=3),
    "Tick": lambda: _tone([(1800, 0.05)], decay=40),
    "Rising": lambda: _glide(400, 1200, 0.45),
    "Falling": lambda: _glide(1200, 400, 0.45),
}


def builtin_sound_path(name: str) -> Path:
    """The .wav of a built-in cue (written the first time it is asked for)."""
    folder = project_path("assets") / "sounds"
    path = folder / f"{name.lower().replace(' ', '_')}.wav"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        samples = _CUES.get(name, _CUES["Chime"])()
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32000)) for s in samples))
    return path


# --------------------------------------------------------------------------- audio
class AudioOut(QObject):
    """Plays cues, files and speech.  Missing Qt Multimedia / speech just logs and stays silent."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._effects: dict[str, Any] = {}
        self._players: list[Any] = []
        self._tts: Any | None = None
        self._tts_failed = False
        self._speech_queue: list[tuple[str, int, str]] = []
        self._speaking_scope: str | None = None
        self._speech_active = False
        self.master = 0.8
        self.voice = ""
        self.rate = 0.0
        self.device = ""

    # -- settings ----------------------------------------------------------------------
    def configure(self, *, volume: int, voice: str, rate: float, device: str = "") -> None:
        self.master = max(0.0, min(1.0, volume / 100.0))
        self.voice, self.rate, self.device = voice, rate, device
        if self._tts is not None:
            self._apply_voice()

    @staticmethod
    def voices() -> list[str]:
        try:
            from PySide6.QtTextToSpeech import QTextToSpeech

            return [v.name() for v in QTextToSpeech().availableVoices()]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def devices() -> list[str]:
        try:
            from PySide6.QtMultimedia import QMediaDevices

            return [d.description() for d in QMediaDevices.audioOutputs()]
        except Exception:  # noqa: BLE001
            return []

    def _device(self) -> Any | None:
        if not self.device:
            return None
        try:
            from PySide6.QtMultimedia import QMediaDevices

            return next((d for d in QMediaDevices.audioOutputs() if d.description() == self.device), None)
        except Exception:  # noqa: BLE001
            return None

    # -- playback ----------------------------------------------------------------------
    def play_builtin(self, name: str, volume: int = 100) -> None:
        try:
            from PySide6.QtMultimedia import QSoundEffect
        except Exception:  # noqa: BLE001
            log.warning("Qt Multimedia unavailable: no sound")
            return
        key = f"{name}|{self.device}"
        effect = self._effects.get(key)
        if effect is None:
            effect = QSoundEffect(self)
            device = self._device()
            if device is not None:
                effect.setAudioDevice(device)
            effect.setSource(QUrl.fromLocalFile(str(builtin_sound_path(name))))
            self._effects[key] = effect
        effect.setVolume(self.master * max(0, min(100, volume)) / 100.0)
        effect.play()

    def play_file(self, path: str, volume: int = 100) -> None:
        if not path or not Path(path).is_file():
            log.warning("sound file not found: %s", path)
            return
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except Exception:  # noqa: BLE001
            log.warning("Qt Multimedia unavailable: no sound")
            return
        player = QMediaPlayer(self)
        out = QAudioOutput(player)
        device = self._device()
        if device is not None:
            out.setDevice(device)
        out.setVolume(self.master * max(0, min(100, volume)) / 100.0)
        player.setAudioOutput(out)
        player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self._players.append(player)

        def done(status: Any, p: Any = player) -> None:
            if status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
                if p in self._players:
                    self._players.remove(p)
                p.deleteLater()

        player.mediaStatusChanged.connect(done)
        player.play()

    def speak(self, text: str, volume: int = 100, *, scope: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        tts = self._speech()
        if tts is None:
            return
        # Keep our own queue so replacement can remove only the obsolete timer's
        # speech, including alerts waiting behind an unrelated long announcement.
        self._speech_queue.append((text, volume, scope))
        self._pump_speech()

    def _pump_speech(self) -> None:
        if self._tts is None or self._speech_active or not self._speech_queue:
            return
        from PySide6.QtTextToSpeech import QTextToSpeech

        if self._tts.state() != QTextToSpeech.State.Ready:
            return
        text, volume, self._speaking_scope = self._speech_queue.pop(0)
        self._speech_active = True
        self._tts.setVolume(self.master * max(0, min(100, volume)) / 100.0)
        self._tts.say(text)

    def _speech_state_changed(self, state: Any) -> None:
        from PySide6.QtTextToSpeech import QTextToSpeech

        if state in (QTextToSpeech.State.Ready, QTextToSpeech.State.Error):
            self._speech_active = False
            self._speaking_scope = None
            if state == QTextToSpeech.State.Error:
                self._speech_queue.clear()
            else:
                QTimer.singleShot(0, self, self._pump_speech)

    def cancel_speech(self, scope: str, *, prefix: bool = False) -> None:
        """Remove queued speech for this timer, and stop it if it is already speaking."""
        matches = lambda value: value is not None and (value.startswith(scope) if prefix else value == scope)
        self._speech_queue = [entry for entry in self._speech_queue if not matches(entry[2])]
        if matches(self._speaking_scope):
            self._speech_active = False
            self._speaking_scope = None
            if self._tts is not None:
                self._tts.stop()
            QTimer.singleShot(0, self, self._pump_speech)

    def _speech(self) -> Any | None:
        if self._tts is None and not self._tts_failed:
            try:
                from PySide6.QtTextToSpeech import QTextToSpeech

                self._tts = QTextToSpeech(self)
                self._tts.stateChanged.connect(self._speech_state_changed)
                self._apply_voice()
            except Exception:  # noqa: BLE001
                log.warning("text to speech unavailable", exc_info=True)
                self._tts_failed = True
        return self._tts

    def _apply_voice(self) -> None:
        tts = self._tts
        if tts is None:
            return
        if self.voice:
            voice = next((v for v in tts.availableVoices() if v.name() == self.voice), None)
            if voice is not None:
                tts.setVoice(voice)
        tts.setRate(max(-1.0, min(1.0, self.rate)))

    def run(self, action: str, *, sound: str = "", file: str = "", speech: str = "", volume: int = 100,
            scope: str = "") -> None:
        """Perform one action (``none`` / ``sound`` / ``file`` / ``speak``)."""
        if action == "sound":
            self.play_builtin(sound or "Chime", volume)
        elif action == "file":
            self.play_file(file, volume)
        elif action == "speak":
            self.speak(speech, volume, scope=scope)

    def stop(self) -> None:
        self._speech_queue.clear()
        self._speaking_scope = None
        self._speech_active = False
        for effect in self._effects.values():
            effect.stop()
        for p in list(self._players):
            p.stop()
        if self._tts is not None:
            self._tts.stop()


# --------------------------------------------------------------------------- runner
class TriggerRunner(QObject):
    """Matches chat lines against the triggers, plays their cues and runs their timers."""

    fired = Signal(object)  #: Match
    timers_changed = Signal()
    store_changed = Signal()

    def __init__(self, store: TriggerStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.audio = AudioOut(self)
        self.board = TimerBoard()
        self._last_fired: dict[str, float] = {}
        self._apply_audio_settings()
        self._clock = QTimer(self)
        self._clock.setInterval(TICK_MS)
        self._clock.timeout.connect(self._tick)

    def _apply_audio_settings(self) -> None:
        s = self.store
        self.audio.configure(volume=s.volume, voice=s.voice, rate=s.rate, device=s.output_device)

    def save(self) -> None:
        """Persist the store (after an edit on the Triggers page) and apply its audio settings."""
        self.store.save()
        self._apply_audio_settings()
        for timer in self.board.timers:
            trigger = self.store.find(timer.trigger_id)
            if trigger is not None:
                timer.color, timer.warn_color, timer.low_color = (trigger.timer_color, trigger.timer_warn_color,
                                                                  trigger.timer_low_color)
                timer.low_s = trigger.timer_low_s
        self.timers_changed.emit()
        self.store_changed.emit()

    # -- matching ----------------------------------------------------------------------
    def observe(self, line: str, now: float | None = None) -> list[Match]:
        """Check one chat line against every trigger; act on the matches."""
        now = time.time() if now is None else now
        fired: list[Match] = []
        for m in self.store.matches(line):
            trig = m.trigger
            last = self._last_fired.get(trig.id)
            if trig.cooldown_s > 0 and last is not None and now - last < trig.cooldown_s:
                continue
            if self.fire(m, now):
                self._last_fired[trig.id] = now
                fired.append(m)
        return fired

    def fire(self, m: Match, now: float | None = None) -> bool:
        """Run ``m``'s action and start its timer (also used by the page's Test button)."""
        trig = m.trigger
        values = m.values()
        scope = ""
        if trig.timer:
            label = fill_placeholders(trig.timer_label or trig.name, values)
            previous = list(self.board.timers)
            timer = self.board.start(trig, label, now)
            if timer is None:
                return False  # Retain ignores the entire overlapping match, audio included.
            if trig.timer_mode != "stack":
                self.audio.cancel_speech(f"timer:{trig.id}:", prefix=True)
            for old in previous:
                if old not in self.board.timers:
                    self.audio.cancel_speech(self._timer_scope(old))
            scope = self._timer_scope(timer)
            self._clock.start()
            self.timers_changed.emit()
        self.audio.run(trig.action, sound=trig.sound, file=trig.file,
                       speech=fill_placeholders(trig.speech, values), volume=trig.volume, scope=scope)
        self.fired.emit(m)
        return True

    # -- timers ------------------------------------------------------------------------
    def start_one_time_timer(self, label: str, seconds: float, now: float | None = None) -> ActiveTimer:
        """Start an independent, silent countdown without adding a chat trigger."""
        if not isinstance(label, str) or not label.strip():
            raise ValueError("A one-time timer needs a label")
        try:
            duration = float(seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("A one-time timer needs a positive, finite duration") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("A one-time timer needs a positive, finite duration")
        trigger = Trigger(name=label.strip(), action="none", timer=True, timer_seconds=duration,
                          timer_mode="stack", timer_warn_action="none", timer_end_action="none")
        now = time.time() if now is None else now
        previous = list(self.board.timers)
        # A confirmed manual countdown must survive the board's longest-first cap.
        while len(self.board.timers) >= self.board.MAX_TIMERS:
            obsolete = next((timer for timer in self.board.timers if timer.ended), None)
            if obsolete is None:
                obsolete = max(self.board.timers, key=lambda timer: timer.remaining(now))
            self.board.cancel(obsolete.id)
        timer = self.board.start(trigger, trigger.name, now)
        assert timer is not None  # A fresh stack timer cannot retain an older instance.
        for old in previous:
            if old not in self.board.timers:
                self.audio.cancel_speech(self._timer_scope(old))
        self._clock.start()
        self.timers_changed.emit()
        return timer

    def cancel_timer(self, timer_id: str) -> None:
        for timer in self.board.timers:
            if timer.id == timer_id:
                self.audio.cancel_speech(self._timer_scope(timer))
        self.board.cancel(timer_id)
        self.timers_changed.emit()

    def clear_timers(self) -> None:
        self.audio.cancel_speech("timer:", prefix=True)
        self.board.clear()
        self.timers_changed.emit()

    def _tick(self) -> None:
        before = len(self.board.timers)
        warned, ended = self.board.tick()
        for t in warned:
            self._alert(t, "warn")
        for t in ended:
            self._alert(t, "end")
        if warned or ended or len(self.board.timers) != before:
            self.timers_changed.emit()
        if not self.board.timers:
            self._clock.stop()

    def _alert(self, timer: ActiveTimer, which: str) -> None:
        trig = self.store.find(timer.trigger_id)
        if trig is None or not trig.enabled or not any(t is timer for t in self.board.timers):
            return
        values = {"label": timer.label, "name": trig.name}
        if which == "warn":
            self.audio.run(trig.timer_warn_action, sound=trig.timer_warn_sound,
                           speech=fill_placeholders(trig.timer_warn_speech, values), volume=trig.volume,
                           scope=self._timer_scope(timer))
        else:
            self.audio.run(trig.timer_end_action, sound=trig.timer_end_sound,
                           speech=fill_placeholders(trig.timer_end_speech, values), volume=trig.volume,
                           scope=self._timer_scope(timer))

    @staticmethod
    def _timer_scope(timer: ActiveTimer) -> str:
        return f"timer:{timer.trigger_id}:{timer.id}"

    def test(self, trig: Trigger, line: str = "") -> None:
        """Fire ``trig`` as if ``line`` (or its pattern) had been read."""
        # Explicit previews work even for disabled triggers, but still capture
        # the sample line's regex groups exactly as live matching would.
        matched = match_trigger(replace(trig, enabled=True), line) if line else None
        if matched is not None:
            matched.trigger = trig
        self.fire(matched or Match(trig, line or trig.pattern, line or trig.pattern))


def default_store() -> TriggerStore:
    store = TriggerStore(project_path("triggers.json"))
    loaded = store.load()
    if store.install_presets() and (loaded or not store.path.is_file()):
        try:
            store.save()
        except OSError:
            log.warning("could not save bundled trigger presets", exc_info=True)
    return store


__all__ += ["default_store", "BUILTIN_SOUNDS"]

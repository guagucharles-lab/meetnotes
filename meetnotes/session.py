"""A recording session: capture -> chunked transcription -> notes.

Data flow, one thread each:

  capture thread     every second pulls new audio from the mic and the
                     system/loopback source, appends it to audio.wav and to a
                     pending buffer. When the buffer passes chunk_seconds it
                     is cut at the quietest moment and queued.
  transcribe thread  runs Whisper on each queued chunk, once per channel, so
                     your lines are "Me" and everyone else's are "Others";
                     drops mic echo; appends to transcript.md.

stop() drains both threads and asks Claude for the notes.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio as audio_mod
from .audio import TARGET_RATE, AudioSource, StereoWavWriter
from .config import Config, write_json
from .dsp import drop_echo, find_cut, gate_mic, label_by_energy, mix, pad
from .safeword import find_safe_word
from .screen import ScreenRecorder, finalize as finalize_screen
from .summarize import MeetingMeta, live_notes, render_markdown, summarize_transcript
from .transcribe import Segment, Transcriber, Transcript, fmt_ts, format_line, load_transcript_text

log = logging.getLogger("meetnotes")
EMPTY = np.zeros(0, dtype=np.int16)
GENERIC_TITLES = {"zoom meeting", "zoom webinar", "meeting", "google meet", "microsoft teams", "kokous", "manual"}

__all__ = ["MeetingSession", "mix", "slugify", "meeting_dir", "resummarize",
           "transcribe_wav", "write_notes_for", "recover_unfinished"]


def slugify(text: str, limit: int = 40) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.U).strip()
    text = re.sub(r"[\s_]+", "-", text)
    return text[:limit].strip("-") or "meeting"


def private_mkdir(path: Path) -> None:
    """Create a directory only your user account can open (macOS/Linux)."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)


def meeting_dir(root: Path, started_at: datetime, title: str) -> Path:
    private_mkdir(root)
    stamp = started_at.strftime("%Y-%m-%d_%H%M")
    path = root / f"{stamp}_{slugify(title)}"
    n = 2
    while path.exists():
        path = root / f"{stamp}_{slugify(title)}-{n}"
        n += 1
    private_mkdir(path)
    return path


_TRANSCRIBERS: dict[tuple, Transcriber] = {}
_TRANSCRIBERS_LOCK = threading.Lock()


def make_transcriber(cfg: Config) -> Transcriber:
    """One Whisper model per settings combination, shared by every session in
    this process (a desktop meeting and a phone recording at the same time
    don't load the model twice)."""
    import os
    if os.environ.get("MEETNOTES_E2E_TRANSCRIBER") and os.environ.get("MEETNOTES_INPROCESS_WHISPER"):  # tests only
        from ._testing import ScriptTranscriber
        return ScriptTranscriber(os.environ["MEETNOTES_E2E_TRANSCRIBER"])
    # The safe word in Whisper's glossary makes it write "omerta" (not
    # "Ameerta", "on merta") so the safe word is recognised reliably.
    vocab = list(cfg.vocabulary)
    if cfg.safe_word.strip() and cfg.safe_word.strip().lower() not in (v.lower() for v in vocab):
        vocab.append(cfg.safe_word.strip())
    key = (cfg.whisper_model, cfg.whisper_device, cfg.whisper_compute_type,
           cfg.language, tuple(vocab))
    with _TRANSCRIBERS_LOCK:
        if key not in _TRANSCRIBERS:
            if os.environ.get("MEETNOTES_INPROCESS_WHISPER"):
                _TRANSCRIBERS[key] = Transcriber(cfg.whisper_model, cfg.whisper_device,
                                                 cfg.whisper_compute_type, cfg.language, vocab)
            else:  # a crash in the speech engine must not take the service down
                from .whisperproc import IsolatedTranscriber
                _TRANSCRIBERS[key] = IsolatedTranscriber(cfg.whisper_model, cfg.whisper_device,
                                                         cfg.whisper_compute_type, cfg.language, vocab)
        return _TRANSCRIBERS[key]


FAST_MODELS = {"tiny", "tiny.en", "base", "base.en", "small", "small.en"}


class AdaptiveTranscriber:
    """Uses the configured Whisper model, but if this computer cannot keep up
    with it (transcribing takes longer than the audio itself, twice in a row),
    switches to the much faster 'small' model for the rest of the work, so a
    meeting never ends with hours of transcription still to go."""

    def __init__(self, cfg: Config, fallback: str = "small") -> None:
        self.cfg = cfg
        self.fallback = fallback
        self.model = cfg.whisper_model
        self.inner = make_transcriber(cfg)
        self.switched = ""
        self._slow = 0
        self.last_rtf = 0.0
        # In "separate" mode each chunk is transcribed twice (me + others).
        self._limit = 1.1 / (2 if cfg.transcribe_mode == "separate" and cfg.speaker_labels else 1)

    def load(self) -> None:
        load = getattr(self.inner, "load", None)
        if load:
            load()

    def transcribe(self, samples, offset: float, speaker: str = "") -> list[Segment]:
        self.load()  # model loading is not counted as transcription time
        t0 = time.monotonic()
        segs = self.inner.transcribe(samples, offset, speaker)
        wall = time.monotonic() - t0
        # the worker may have moved to a smaller model by itself
        self.model = getattr(self.inner, "model_size", self.model) or self.model
        audio_s = len(samples) / TARGET_RATE
        if audio_s >= 5:
            self.last_rtf = wall / audio_s
            log.info("Transcribed %.0f s of audio in %.0f s with '%s'.", audio_s, wall, self.model)
            if self.last_rtf > self._limit and self.model not in FAST_MODELS and not self.switched:
                self._slow += 1
                if self._slow >= 2:
                    from dataclasses import replace
                    log.warning("This computer is too slow for '%s' (%.1fx real time); switching to '%s'.",
                                self.model, self.last_rtf, self.fallback)
                    self.inner = make_transcriber(replace(self.cfg, whisper_model=self.fallback))
                    self.switched = self.fallback
                    self.model = self.fallback
            else:
                self._slow = 0
        return segs


@dataclass
class ChunkJob:
    offset: float
    mic: np.ndarray
    system: np.ndarray


class MeetingSession:
    """Owns one meeting folder. Call start(), then stop() when the meeting ends."""

    def __init__(self, cfg: Config, title: str, platform: str = "",
                 transcriber: Transcriber | None = None,
                 is_paused: Callable[[], bool] | None = None,
                 on_segment: Callable[[Segment], None] | None = None,
                 event=None) -> None:
        self.cfg = cfg
        # A calendar event names the meeting better than a window title does,
        # unless the window clearly shows a different meeting.
        if event is not None and event.title:
            t, e = (title or "").lower().strip(), event.title.lower().strip()
            if not t or t in GENERIC_TITLES or e in t or t in e:
                title = event.title
            else:
                event = None  # a differently named meeting: don't borrow the calendar's attendees
        self.window_title = title
        self.title = title or "Meeting"
        self.platform = platform
        self.started_at = datetime.now()
        self.dir = meeting_dir(cfg.notes_root, self.started_at, self.title)
        self.transcript = Transcript(self.dir / "transcript.md")
        self.transcriber = transcriber or AdaptiveTranscriber(cfg)
        self.is_paused = is_paused or (lambda: False)
        self.on_segment = on_segment
        self._jobs: "queue.Queue[ChunkJob | None]" = queue.Queue()
        self._stop = threading.Event()
        self._mic: AudioSource | None = None
        self._sys: AudioSource | None = None
        self._wav: StereoWavWriter | None = None
        self._screen: ScreenRecorder | None = None
        self._threads: list[threading.Thread] = []
        self._t0 = 0.0
        self._next_live = 0.0
        self._pend_mic: list[np.ndarray] = []
        self._pend_sys: list[np.ndarray] = []
        self._pend_len = 0
        self._emitted = 0          # samples already queued (or skipped while paused)
        self._was_paused = False
        self.last_speech = 0.0     # elapsed() when someone last said something
        self.safe_word_at: float | None = None  # meeting time the safe word was said
        self.phase = "recording"   # recording | transcribing | video | notes
        self._abandon = threading.Event()   # "stop waiting" pressed while finishing
        self.abandoned_backlog = 0.0
        self.event = event         # cal.Event of the calendar meeting happening now
        self.template = cfg.notes_template
        self.alerts: list[dict] = []   # watch-word hits, local, no API
        self._watch = [w.lower() for w in cfg.watch_words if w.strip()]
        self.briefing = None       # assistant.Briefing attached to this meeting
        self.assistant = None      # assistant.LiveAssistant while the meeting runs
        self._assistant_lock = threading.Lock()
        self._queued_s = 0.0       # seconds of audio waiting for Whisper
        self.warnings: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._mic, self._sys, notes = audio_mod.build_sources(self.cfg.mic_device, self.cfg.system_device)
        self.warnings.extend(notes)
        if self._mic is None and self._sys is None:
            raise RuntimeError("Both audio sources are disabled; nothing to record.")
        if self.cfg.keep_audio or self.cfg.record_screen:  # the video needs the audio track
            self._wav = StereoWavWriter(self.dir / "audio.wav")
        for src in (self._mic, self._sys):
            if src is not None:
                src.start()
        if self.cfg.record_screen:
            self._screen = ScreenRecorder(
                self.dir / "screen-video.mp4", self.cfg.screen_fps, self.cfg.screen_max_width,
                self.cfg.screen_monitor,
                # black while paused, and nothing more once the safe word is said
                is_paused=lambda: self.is_paused() or self.safe_word_at is not None)
            self._screen.start()
        self._t0 = time.monotonic()
        self._next_live = self.cfg.live_notes_interval
        if self.cfg.assistant_live:
            try:
                self.start_assistant(self.find_briefing())
            except Exception as exc:
                log.warning("assistant not started: %s", exc)
        self._write_meta(status="recording", template=self.template,
                         sources=[s.name for s in (self._mic, self._sys) if s is not None],
                         **({"event": {"title": self.event.title, "start": self.event.start, "attendees": self.event.attendees,
                                       "link": self.event.link}} if self.event is not None else {}))

        self._threads = [
            threading.Thread(target=self._capture_loop, name="capture", daemon=True),
            threading.Thread(target=self._transcribe_loop, name="transcribe", daemon=True),
        ]
        for t in self._threads:
            t.start()
        log.info("Recording to %s", self.dir)

    def elapsed(self) -> float:
        return time.monotonic() - self._t0 if self._t0 else 0.0

    def idle_for(self) -> float:
        """Seconds since anyone last said something (0 while paused)."""
        if self.is_paused():
            return 0.0
        return self.elapsed() - self.last_speech

    @property
    def paused(self) -> bool:
        return self._was_paused

    @property
    def backlog(self) -> float:
        """Seconds of meeting audio not transcribed yet."""
        return self._queued_s + self._pend_len / TARGET_RATE

    @property
    def safe_word_hit(self) -> bool:
        return self.safe_word_at is not None

    def stop(self, write_notes: bool = True) -> Path | None:
        """Stop capture, finish transcription, write notes. Returns notes path."""
        self.phase = "transcribing"
        self._stop.set()
        for t in self._threads:
            while t.is_alive() and not self._abandon.is_set():
                t.join(1.0)
        if self._abandon.is_set() and self.backlog > 1:
            self.abandoned_backlog = self.backlog
            log.warning("Stopped waiting for transcription; %.0f min of audio left untranscribed (kept in audio.wav).",
                        self.abandoned_backlog / 60)
            t = self._emitted / TARGET_RATE
            self.transcript.add([Segment(t, t, "", f"(transcription stopped early: about {round(self.abandoned_backlog / 60)} min of audio not transcribed)")])
        for src in (self._mic, self._sys):
            if src is not None:
                src.stop()
        if self._screen is not None:
            self._screen.stop()
            if self._screen.error:
                self.warnings.append(self._screen.error)
        if self._wav is not None:
            self._wav.close()
        self._finish_assistant()
        duration = self.elapsed()
        if self.safe_word_at is not None:
            # Audio between the safe word and its detection was already on
            # disk; cut the recording back to the moment it was said.
            duration = self.safe_word_at
            if self._wav is not None:
                audio_mod.truncate_wav(self._wav.path, self.safe_word_at)
            if self.cfg.safe_word_action == "discard":
                shutil.rmtree(self.dir, ignore_errors=True)
                log.info("Safe word: meeting discarded, nothing kept.")
                return None
        self.phase = "video"
        self._finish_screen()
        if self._wav is not None and not self.cfg.keep_audio:
            (self.dir / "audio.wav").unlink(missing_ok=True)  # only needed for the video
        self._write_meta(status="transcribed", duration_s=duration,
                         safe_word_at=self.safe_word_at)
        if not write_notes:
            return None
        self.phase = "notes"
        return self.write_notes(duration)

    # -- assistant -----------------------------------------------------------

    def find_briefing(self):
        from .assistant import list_briefings, match_briefing
        try:
            title = self.title if self.event is None else f"{self.title} {self.event.title}"
            return match_briefing(list_briefings(self.cfg.notes_root), title, f"{self.started_at:%Y-%m-%d}")
        except Exception as exc:
            log.warning("briefing lookup failed: %s", exc)
            return None

    def start_assistant(self, briefing=None) -> None:
        """Start (or restart with another briefing) the live assistant. The
        transcript so far is handed over, so attaching late loses nothing."""
        from .assistant import LiveAssistant, briefing_context, save_briefing
        with self._assistant_lock:
            old = self.assistant
            if old is not None:
                old.stop()
            self.briefing = briefing
            context = briefing_context(self.cfg.notes_root, briefing, title=self.title, generic_past=3)
            self.assistant = LiveAssistant(
                context, self.title, style=self.cfg.assistant_style, interval=self.cfg.assistant_interval,
                model=self.cfg.assistant_model, effort=self.cfg.assistant_effort,
                briefing_title=briefing.title if briefing else "")
            with self.transcript._lock:
                self.assistant.add_lines([format_line(s) for s in self.transcript.segments])
            self.assistant.start()
            if briefing is not None:
                briefing.used_by = self.dir.name
                save_briefing(self.cfg.notes_root, briefing)
                self._write_meta(briefing=briefing.id, briefing_title=briefing.title)
            log.info("Assistant on (%s)", briefing.title if briefing else "no briefing")

    def stop_assistant(self) -> None:
        with self._assistant_lock:
            if self.assistant is not None:
                self.assistant.stop()
                self.assistant.save(self.dir)
                self.assistant = None

    def ask(self, question: str) -> str:
        """A question during the meeting. Starts the assistant if it was off."""
        if self.assistant is None:
            self.start_assistant(self.briefing or self.find_briefing())
        return self.assistant.ask(question)

    def assistant_snapshot(self) -> dict | None:
        a = self.assistant
        return a.snapshot() if a is not None else None

    def _finish_assistant(self) -> None:
        a = self.assistant
        if a is None:
            return
        try:
            a.stop()
            a.save(self.dir)
        except Exception as exc:
            log.warning("assistant save failed: %s", exc)

    # -- your own notes during the meeting ---------------------------------

    def my_notes(self) -> str:
        f = self.dir / "my-notes.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def set_my_notes(self, text: str) -> None:
        (self.dir / "my-notes.md").write_text(text, encoding="utf-8")

    def notes_context(self) -> str:
        """Briefing, agenda status, your notes, attendees and the previous
        meeting of the same series, for the notes writer."""
        from .assistant import briefing_context
        parts = []
        if self.briefing is not None:
            parts.append(briefing_context(self.cfg.notes_root, self.briefing, budget_chars=20_000))
        if self.event is not None and self.event.attendees:
            parts.append("<attendees>\n" + "\n".join(self.event.attendees) + "\n</attendees>")
        mine = self.my_notes().strip()
        if mine:
            parts.append("<my_notes>\n" + mine + "\n</my_notes>")
        prev = previous_in_series(self.cfg.notes_root, self.title, exclude=self.dir.name)
        if prev:
            parts.append(prev)
        a = self.assistant
        summary = a.agenda_summary() if a is not None else ""
        if not summary and (self.dir / "assistant.json").exists():
            try:
                agenda = json.loads((self.dir / "assistant.json").read_text()).get("agenda") or []
                summary = "\n".join(f"- {x['item']}: {x['status']}" for x in agenda)
            except Exception:
                pass
        if summary:
            parts.append("<agenda_status>\n" + summary + "\n</agenda_status>")
        return "\n\n".join(parts)

    def _finish_screen(self) -> None:
        """screen-video.mp4 + audio -> screen.mp4 (cut at the safe word)."""
        raw = self.dir / "screen-video.mp4"
        if self._screen is None or not raw.exists():
            return
        wav = self.dir / "audio.wav"
        try:
            finalize_screen(raw, self.dir / "screen.mp4", wav if wav.exists() else None, self.safe_word_at)
            raw.unlink()
        except Exception as exc:  # keep the silent video rather than nothing
            log.warning("Could not add audio to the screen recording: %s", exc)
            raw.replace(self.dir / "screen.mp4")

    # -- capture -------------------------------------------------------------

    def _capture_loop(self) -> None:
        target = max(5, self.cfg.chunk_seconds) * TARGET_RATE
        while not self._stop.wait(1.0):
            self._tick(target)
        self._tick(target, final=True)
        self._jobs.put(None)  # sentinel for the transcriber

    def _tick(self, target: int, final: bool = False) -> None:
        mic = self._mic.take() if self._mic else EMPTY
        system = self._sys.take() if self._sys else EMPTY
        n = max(len(mic), len(system))
        mic, system = pad(mic, n), pad(system, n)
        self._collect_errors()

        if self.safe_word_at is not None:  # listening has stopped: throw audio away
            self._pend_mic, self._pend_sys, self._pend_len = [], [], 0
            return

        if self.is_paused():
            if not self._was_paused:
                self._emit_pending()  # transcribe what came before the pause
                self._mark("(recording paused)")
                self._was_paused = True
                log.info("Paused: audio is being discarded")
            if self._wav is not None and n:
                self._wav.write(np.zeros(n, np.int16), np.zeros(n, np.int16))  # keep timing, no sound
            self._emitted += n
            if final:
                self._emit_pending()
            return
        if self._was_paused:
            self._was_paused = False
            self._mark("(recording resumed)")
            log.info("Resumed")

        if self._wav is not None and n:
            self._wav.write(mic, system)
        if n:
            self._pend_mic.append(mic)
            self._pend_sys.append(system)
            self._pend_len += n
        if final:
            self._emit_pending()
        elif self._pend_len >= target:
            self._emit_pending(cut_in_pause=True)
        if self._jobs.qsize() > 3:
            log.warning("Transcription is falling behind; consider a smaller whisper_model.")

    def _emit_pending(self, cut_in_pause: bool = False) -> None:
        if not self._pend_len:
            return
        mic = np.concatenate(self._pend_mic)
        system = np.concatenate(self._pend_sys)
        cut = find_cut(mic, system, TARGET_RATE) if cut_in_pause else len(mic)
        self._queued_s += cut / TARGET_RATE
        self._jobs.put(ChunkJob(self._emitted / TARGET_RATE, mic[:cut], system[:cut]))
        self._emitted += cut
        rest_m, rest_s = mic[cut:], system[cut:]
        self._pend_mic = [rest_m] if len(rest_m) else []
        self._pend_sys = [rest_s] if len(rest_s) else []
        self._pend_len = len(rest_m)

    def _mark(self, text: str) -> None:
        t = self._emitted / TARGET_RATE
        self.transcript.add([Segment(t, t, "", text)])

    def _collect_errors(self) -> None:
        for src in (self._mic, self._sys):
            if src is not None and src.error:
                self.warnings.append(f"{src.name}: {src.error}")
                log.warning("%s: %s", src.name, src.error)
                src.error = None

    # -- transcription -------------------------------------------------------

    def abandon(self) -> None:
        """Stop waiting for the rest of the transcription and write the notes
        from what is done. The audio stays on disk for a later retry."""
        self._abandon.set()

    @property
    def notice(self) -> str:
        sw = getattr(self.transcriber, "switched", "")
        return f"Kone ei pysynyt perässä, joten puheentunnistus vaihtui nopeampaan malliin ({sw})." if sw else ""

    def _transcribe_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None or self._abandon.is_set():
                break
            try:
                self._transcribe_chunk(job)
            except Exception as exc:  # keep recording even if whisper hiccups
                log.warning("transcription failed for chunk at %.0fs: %s", job.offset, exc)
            finally:
                self._queued_s = max(0.0, self._queued_s - len(job.mic) / TARGET_RATE)
            self._maybe_live_notes()

    def _transcribe_chunk(self, job: ChunkJob) -> None:
        if self.safe_word_at is not None:
            return  # queued before detection, but it's after the safe word
        have_system = self._sys is not None
        segs = transcribe_pair(self.transcriber, self.cfg, job.mic, job.system, job.offset,
                               have_mic=self._mic is not None, have_system=have_system)
        segs, cut = apply_safe_word(segs, self.cfg, labelled=self.cfg.speaker_labels and have_system)
        if cut is not None:
            self.safe_word_at = cut
            log.info("Safe word heard at %s: listening stopped for this meeting.", fmt_ts(cut))
        self.transcript.add(segs)
        if segs:
            self.last_speech = self.elapsed()
            a = self.assistant
            if a is not None:
                a.add_lines([format_line(s) for s in segs])
            if self._watch:
                for s in segs:
                    low = s.text.lower()
                    hits = [w for w in self._watch if w in low]
                    if hits:
                        self.alerts.append({"at": datetime.now().isoformat(timespec="seconds"), "kind": "keyword",
                                            "text": s.text, "why": "Avainsana: " + ", ".join(hits), "t": fmt_ts(s.start)})
        for s in segs:
            log.debug("%s", s.text if not s.speaker else f"{s.speaker}: {s.text}")
            if self.on_segment:
                self.on_segment(s)

    def _maybe_live_notes(self) -> None:
        if not self.cfg.live_notes or self.elapsed() < self._next_live:
            return
        self._next_live = self.elapsed() + self.cfg.live_notes_interval
        if self.transcript.word_count() < 40:
            return
        try:
            md = live_notes(self.transcript.text(), self._meta(), self.cfg.claude_model)
            (self.dir / "live-notes.md").write_text(
                f"# Live notes (updated {datetime.now():%H:%M})\n\n{md}\n", encoding="utf-8")
            log.info("Live notes refreshed")
        except Exception as exc:
            log.warning("live notes failed: %s", exc)

    # -- output --------------------------------------------------------------

    def _meta(self, duration_s: float | None = None) -> MeetingMeta:
        return MeetingMeta(self.title, self.platform, self.started_at,
                           duration_s if duration_s is not None else self.elapsed())

    def _write_meta(self, **extra) -> None:
        path = self.dir / "meta.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data.update({"title": self.title, "platform": self.platform,
                     "started_at": self.started_at.isoformat(timespec="seconds"), **extra})
        write_json(path, data)

    def write_notes(self, duration_s: float) -> Path | None:
        words = self.transcript.word_count()
        if words < 20:
            if self.cfg.keep_empty or (self.dir / "screen.mp4").exists() or self.abandoned_backlog:  # keep audio to retry
                self._write_meta(status="too_short", duration_s=duration_s)
                log.info("Only %d words transcribed; no notes written.", words)
            else:
                shutil.rmtree(self.dir, ignore_errors=True)
                log.info("Only %d words transcribed; session discarded.", words)
            return None
        with self.transcript._lock:
            segs = list(self.transcript.segments)
        from .stats import talk_stats
        self._write_meta(stats=talk_stats(segs, duration_s))
        return write_notes_for(self.dir, self.transcript.text(), self._meta(duration_s), self.cfg,
                               context=self.notes_context(), template=self.template)


def transcribe_pair(transcriber: Transcriber, cfg: Config, mic: np.ndarray, system: np.ndarray,
                    offset: float, have_mic: bool = True, have_system: bool = True) -> list[Segment]:
    """Transcribe one chunk of both channels into labelled segments."""
    if cfg.speaker_labels and have_mic and have_system and cfg.transcribe_mode != "separate":
        if cfg.mic_gate:
            mic = gate_mic(mic, system, TARGET_RATE)
        segs = transcriber.transcribe(mix(mic, system), offset, "")
        return label_by_energy(segs, mic, system, offset, TARGET_RATE)
    if cfg.speaker_labels and have_mic and have_system:
        if cfg.mic_gate:
            mic = gate_mic(mic, system, TARGET_RATE)
        me = transcriber.transcribe(mic, offset, "Me")
        others = transcriber.transcribe(system, offset, "Others")
        if cfg.dedupe_echo:
            me = drop_echo(me, others)
        return me + others
    label = "Me" if not have_system else ""
    return transcriber.transcribe(mix(mic, system), offset, label)


def apply_safe_word(segs: list[Segment], cfg: Config, labelled: bool) -> tuple[list[Segment], float | None]:
    """If you said the safe word in this chunk, return only what came before
    it plus a marker, and the time it was said."""
    speakers = ("Me",) if labelled else ("Me", "")  # only your own mic can stop it
    hit = find_safe_word(segs, cfg.safe_word, speakers)
    if hit is None:
        return segs, None
    kept = [s for s in segs if s.start < hit.start]
    kept.append(Segment(hit.start, hit.start, "", "(recording stopped by safe word)"))
    return kept, hit.start


def _update_meta(folder: Path, **fields) -> None:
    meta_path = folder / "meta.json"
    data = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    data.update(fields)
    write_json(meta_path, data)


def normalized_title(title: str) -> str:
    """'Viikkopalaveri 14.11.' and 'Viikkopalaveri (viikko 46)' are one series."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", title.lower())
    t = re.sub(r"\b\d+[./-]\d+([./-]\d+)?\b|\bviikko\s*\d+\b|\bweek\s*\d+\b|\bw\d+\b|\b\d{1,2}[:.]\d{2}\b", " ", t)
    t = re.sub(r"\s*[|–-]\s*(microsoft teams|zoom|google meet|zoom meeting)\s*$", " ", t)
    return re.sub(r"[^\w]+", " ", t).strip()


def previous_in_series(root: Path, title: str, exclude: str = "", max_chars: int = 12_000) -> str:
    """<previous_meeting> block from the latest earlier meeting with the same
    normalized title (recurring meetings), or ''."""
    from .library import meetings
    key = normalized_title(title)
    if len(key) < 4:
        return ""
    for m in meetings(root):
        if m.path.name == exclude:
            continue
        if normalized_title(m.meta.get("title") or m.title) != key and normalized_title(m.title) != key:
            continue
        f = m.path / "notes.md"
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")[:max_chars]
        return f'<previous_meeting date="{(m.meta.get("started_at") or "")[:10]}" title="{m.title}">\n{text}\n</previous_meeting>'
    return ""


def post_webhook(url: str, payload: dict) -> None:
    """Best effort, in the background, never blocks the notes."""
    if not url.startswith(("https://", "http://")):
        return

    def run():
        import urllib.request
        try:
            req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "User-Agent": "meetnotes/0.1"})
            with urllib.request.urlopen(req, timeout=20) as r:
                log.info("webhook %s -> %s", url, r.status)
        except Exception as exc:
            log.warning("webhook failed: %s", exc)
    threading.Thread(target=run, daemon=True, name="webhook").start()


def write_notes_for(folder: Path, transcript_text: str, meta: MeetingMeta, cfg: Config,
                    context: str = "", template: str = "") -> Path:
    from .templates import instructions_for
    extra = cfg.extra_instructions.strip()
    tmpl = instructions_for(template or cfg.notes_template, cfg.templates)
    if tmpl:
        extra = (extra + "\n\n" if extra else "") + tmpl
    if (folder / "meta.json").exists():
        _update_meta(folder, progress={"step": "notes"})
    try:
        notes = summarize_transcript(transcript_text, meta, cfg.claude_model, cfg.effort,
                                     extra, notes_language=cfg.notes_language,
                                     context=context)
    except Exception as exc:  # shown in the app with a "try again" button
        _update_meta(folder, status="notes_failed", notes_error=str(exc)[:500], progress=None)
        raise
    md = render_markdown(notes, meta)
    out = folder / "notes.md"
    out.write_text(md + "\n", encoding="utf-8")
    (folder / "notes.json").write_text(notes.model_dump_json(indent=2), encoding="utf-8")
    meta_path = folder / "meta.json"
    data = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    data.update(status="done", duration_s=meta.duration_s, notes_title=notes.title)
    data.pop("notes_error", None)
    data.pop("progress", None)
    write_json(meta_path, data)
    if cfg.webhook_url:
        post_webhook(cfg.webhook_url, {"id": folder.name, "title": notes.title, "platform": meta.platform,
                                       "started_at": meta.started_at.isoformat(timespec="seconds"),
                                       "duration_s": meta.duration_s, "notes_markdown": md,
                                       "notes": notes.model_dump(), "transcript": transcript_text})
    return out


def resummarize(target: Path, cfg: Config, transcriber: Transcriber | None = None) -> Path:
    """`meetnotes summarize <folder|transcript.md|audio.wav>`."""
    target = target.expanduser().resolve()
    if target.is_dir():
        folder = target
        transcript_path = folder / "transcript.md"
        wav = folder / "audio.wav"
        if (not transcript_path.exists() or not load_transcript_text(transcript_path)) and wav.exists():
            transcript_path = transcribe_wav(wav, folder, cfg, transcriber)
    elif target.suffix.lower() == ".wav":
        folder = target.parent
        transcript_path = transcribe_wav(target, folder, cfg, transcriber)
    else:
        folder, transcript_path = target.parent, target

    meta_path = folder / "meta.json"
    data = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    started = datetime.fromisoformat(data["started_at"]) if "started_at" in data else \
        datetime.fromtimestamp(transcript_path.stat().st_mtime)
    text = load_transcript_text(transcript_path)
    if not text:
        raise RuntimeError(f"{transcript_path} is empty.")
    duration = float(data.get("duration_s") or 0.0)
    if not duration and meta_path.exists():  # interrupted session: estimate from the files
        duration = max(0.0, transcript_path.stat().st_mtime - started.timestamp())
    meta = MeetingMeta(data.get("title", folder.name), data.get("platform", ""), started, duration)
    parts = []
    ev = data.get("event") or {}
    if ev.get("attendees"):
        parts.append("<attendees>\n" + "\n".join(ev["attendees"]) + "\n</attendees>")
    mine = (folder / "my-notes.md")
    if mine.exists() and mine.read_text(encoding="utf-8").strip():
        parts.append("<my_notes>\n" + mine.read_text(encoding="utf-8").strip() + "\n</my_notes>")
    if (folder / "assistant.json").exists():
        try:
            agenda = json.loads((folder / "assistant.json").read_text()).get("agenda") or []
            if agenda:
                parts.append("<agenda_status>\n" + "\n".join(f"- {x['item']}: {x['status']}" for x in agenda) + "\n</agenda_status>")
        except Exception:
            pass
    prev = previous_in_series(cfg.notes_root, meta.title, exclude=folder.name)
    if prev:
        parts.append(prev)
    return write_notes_for(folder, text, meta, cfg, context="\n\n".join(parts), template=data.get("template", ""))


def transcribe_wav(wav: Path, folder: Path, cfg: Config, transcriber: Transcriber | None = None) -> Path:
    transcriber = transcriber or AdaptiveTranscriber(cfg)
    left, right, _ = audio_mod.read_wav_channels(wav)
    out = folder / "transcript.md"
    if out.exists():
        out.unlink()
    transcript = Transcript(out)
    step = TARGET_RATE * max(5, cfg.chunk_seconds)
    total = max(len(left), len(right))
    have_system = len(right) > 0 and float(np.abs(right).mean()) >= 1.0
    track = (folder / "meta.json").exists()
    for start in range(0, total, step):
        if track:  # shown in the app while it works
            _update_meta(folder, progress={"step": "transcribe", "done": int(start / TARGET_RATE),
                                           "total": int(total / TARGET_RATE)})
        offset = start / TARGET_RATE
        mic, system = left[start:start + step], right[start:start + step]
        segs = transcribe_pair(transcriber, cfg, mic, system, offset,
                               have_mic=True, have_system=have_system)
        segs, cut = apply_safe_word(segs, cfg, labelled=cfg.speaker_labels and have_system)
        transcript.add(segs)
        if cut is not None:
            log.info("Safe word at %s: rest of the recording ignored.", fmt_ts(cut))
            break
        log.info("transcribed %s / %s s", int(offset), int(total / TARGET_RATE))
    return out


def recover_unfinished(cfg: Config) -> list[Path]:
    """Write notes for sessions that were cut off (crash, sleep, shutdown)."""
    root = cfg.notes_root
    done: list[Path] = []
    if not root.exists():
        return done
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        meta_path = folder / "meta.json"
        if not meta_path.exists() or (folder / "notes.md").exists():
            continue
        try:
            status = json.loads(meta_path.read_text()).get("status")
        except Exception:
            continue
        if status not in ("recording", "transcribed"):
            continue
        transcript = folder / "transcript.md"
        if transcript.exists() and len(load_transcript_text(transcript).split()) >= 20:
            try:
                done.append(resummarize(folder, cfg))
                log.info("Recovered notes for %s", folder.name)
            except Exception as exc:
                log.warning("Could not recover %s: %s", folder.name, exc)
    return done

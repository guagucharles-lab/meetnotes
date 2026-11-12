"""Local speech-to-text with faster-whisper, plus the transcript file format."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import TARGET_RATE


@dataclass
class Segment:
    start: float   # seconds from meeting start
    end: float
    speaker: str   # "Me", "Others", or "" when unknown
    text: str
    no_speech: float = 0.0  # Whisper's probability that this was not speech


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class Transcriber:
    """Thin wrapper around faster_whisper.WhisperModel (loaded lazily)."""

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "auto", language: str = "",
                 vocabulary: list[str] | None = None) -> None:
        self.model_size = model_size
        # Whisper's initial prompt biases spelling towards these words.
        self.prompt = ("Glossary: " + ", ".join(vocabulary) + ".") if vocabulary else None
        self.device = device
        self.compute_type = compute_type
        self.language = language or None
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._model is None:
            import logging
            import time as _time
            log = logging.getLogger("meetnotes")
            log.info("Loading speech model '%s' (the first time it is downloaded, ~0.5-1.6 GB)...", self.model_size)
            t0 = _time.monotonic()
            from faster_whisper import WhisperModel  # heavy import, deferred
            compute = self.compute_type
            if compute == "auto":
                compute = "float16" if self.device == "cuda" else "int8"
            import os
            # Leave a couple of cores for audio, video and the meeting app itself.
            threads = min(8, max(2, (os.cpu_count() or 4) - 2))
            self._model = WhisperModel(self.model_size, device=self.device, compute_type=compute,
                                       cpu_threads=threads)
            log.info("Speech model '%s' ready in %.0f s (%d CPU threads).", self.model_size, _time.monotonic() - t0, threads)

    def transcribe(self, samples: np.ndarray, offset: float, speaker: str = "") -> list[Segment]:
        """Transcribe int16 mono 16 kHz samples. `offset` is where this chunk
        starts in the meeting, so returned timestamps are absolute."""
        if len(samples) < TARGET_RATE // 2:  # under half a second
            return []
        if float(np.abs(samples).mean()) < 3.0:  # digital silence
            return []
        self.load()
        audio = samples.astype(np.float32) / 32768.0
        with self._lock:
            segments, _info = self._model.transcribe(
                audio, language=self.language, vad_filter=True, beam_size=1,
                condition_on_previous_text=False, initial_prompt=self.prompt,
            )
            out = []
            for seg in segments:
                text = seg.text.strip()
                if text:
                    out.append(Segment(offset + seg.start, offset + seg.end, speaker, text,
                                       float(getattr(seg, "no_speech_prob", 0.0) or 0.0)))
        return out


class Transcript:
    """In-memory transcript that also appends to transcript.md as it grows."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.segments: list[Segment] = []
        self._lock = threading.Lock()
        if path is not None and not path.exists():
            path.write_text("# Transcript\n\n")

    def add(self, segments: list[Segment]) -> None:
        if not segments:
            return
        with self._lock:
            self.segments.extend(segments)
            self.segments.sort(key=lambda s: s.start)
            if self.path is not None:
                with open(self.path, "a", encoding="utf-8") as fh:
                    for seg in sorted(segments, key=lambda s: s.start):
                        fh.write(format_line(seg) + "\n")

    def text(self) -> str:
        with self._lock:
            return "\n".join(format_line(s) for s in self.segments)

    def duration(self) -> float:
        with self._lock:
            return max((s.end for s in self.segments), default=0.0)

    def word_count(self) -> int:
        with self._lock:
            return sum(len(s.text.split()) for s in self.segments)


def format_line(seg: Segment) -> str:
    who = f" {seg.speaker}:" if seg.speaker else ""
    return f"[{fmt_ts(seg.start)}]{who} {seg.text}"


def load_transcript_text(path: Path) -> str:
    """Read a transcript.md (or any text file) for re-summarising."""
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.startswith("# Transcript")]
    return "\n".join(lines).strip()

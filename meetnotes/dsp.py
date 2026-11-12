"""Small signal and text helpers that make the transcript cleaner.

* find_cut     - end each transcription chunk in a pause, not mid-word
* gate_mic     - mute the mic while only the other side is talking
* drop_echo    - remove "Me" lines that are really the speakers leaking into the mic
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import numpy as np

from .transcribe import Segment


def pad(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) >= n:
        return x
    out = np.zeros(n, dtype=np.int16)
    out[: len(x)] = x
    return out


def mix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if not len(a):
        return b
    if not len(b):
        return a
    n = max(len(a), len(b))
    out = np.zeros(n, dtype=np.int32)
    out[: len(a)] += a
    out[: len(b)] += b
    return np.clip(out, -32768, 32767).astype(np.int16)


def find_cut(mic: np.ndarray, system: np.ndarray, rate: int,
             search_s: float = 8.0, frame_s: float = 0.25) -> int:
    """Index of the quietest moment in the last `search_s` seconds.

    Whisper garbles words that are split across two chunks, so instead of
    cutting every N seconds exactly we cut in the nearest pause."""
    energy = np.abs(mix(mic, system).astype(np.float32))
    length = len(energy)
    frame = max(1, int(rate * frame_s))
    start = max(0, length - int(search_s * rate))
    end = length - frame
    if end <= start:
        return length
    best, best_i = float("inf"), length
    for i in range(start, end + 1, max(1, frame // 2)):
        e = float(energy[i:i + frame].mean())
        if e < best:
            best, best_i = e, i + frame // 2
    return best_i


def _frame_rms(x: np.ndarray, frame: int, n: int) -> np.ndarray:
    f = x[: n * frame].astype(np.float32).reshape(n, frame)
    return np.sqrt((f * f).mean(axis=1))


def gate_mic(mic: np.ndarray, system: np.ndarray, rate: int = 16000,
             ratio: float = 0.5, floor: float = 300.0) -> np.ndarray:
    """Zero 20 ms mic frames where the far side is talking and the mic is
    clearly quieter than it (i.e. the mic only hears the speakers)."""
    frame = rate // 50
    n = min(len(mic), len(system)) // frame
    out = mic.copy()
    if n == 0:
        return out
    m = _frame_rms(mic, frame, n)
    s = _frame_rms(system, frame, n)
    mask = (s > floor) & (m < s * ratio)
    out[: n * frame].reshape(n, frame)[mask] = 0
    return out


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def drop_echo(me: list[Segment], others: list[Segment],
              min_words: int = 3, coverage: float = 0.6) -> list[Segment]:
    """Drop a "Me" segment when most of its words were said by "Others" at the
    same time. That happens when you use speakers instead of headphones."""
    keep = []
    for seg in me:
        words = _words(seg.text)
        if len(words) < min_words:
            keep.append(seg)
            continue
        near = [o for o in others if o.start <= seg.end + 1.5 and o.end >= seg.start - 1.5]
        other_words = [w for o in near for w in _words(o.text)]
        if other_words:
            matcher = SequenceMatcher(None, words, other_words, autojunk=False)
            matched = sum(b.size for b in matcher.get_matching_blocks())
            if matched / len(words) >= coverage:
                continue
        keep.append(seg)
    return keep


def label_by_energy(segments: list[Segment], mic: np.ndarray, system: np.ndarray,
                    offset: float, rate: int = 16000, ratio: float = 1.0) -> list[Segment]:
    """Mixed-mode transcription: Whisper ran once on both channels together;
    each segment goes to "Me" if the mic was louder than the computer's audio
    over that stretch, else "Others". With headphones the mic never hears the
    others; with speakers the mic hears them more quietly than the loopback."""
    out = []
    for seg in segments:
        a = max(0, int((seg.start - offset) * rate))
        b = max(a + 1, int((seg.end - offset) * rate))
        m = mic[a:b].astype(np.float32)
        s = system[a:b].astype(np.float32)
        m_rms = float(np.sqrt((m * m).mean())) if len(m) else 0.0
        s_rms = float(np.sqrt((s * s).mean())) if len(s) else 0.0
        speaker = "Me" if m_rms > s_rms * ratio else "Others"
        out.append(Segment(seg.start, seg.end, speaker, seg.text, seg.no_speech))
    return out

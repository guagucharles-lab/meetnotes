"""Spoken safe word: say "omerta" during a meeting and listening stops.

Whisper writes the word in many ways depending on accent and the language it
guesses: "omerta", "Omertà", "omertaa", "o merta", "ommerta"... so matching is
fuzzy. It must also not fire on everyday Finnish such as "merta" (the sea),
"omena" or "omat", so a candidate has to start like the safe word and be very
close to it overall.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .transcribe import Segment


def normalize(text: str) -> str:
    """lowercase, strip accents (omertà -> omerta), letters/digits and spaces only."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", " ", text)


def _close(candidate: str, word: str, threshold: float) -> bool:
    if not candidate:
        return False
    if candidate.startswith("h") and not word.startswith("h"):
        candidate = candidate[1:]  # "homerta": a breathy start
    if candidate[:2] != word[:2] and candidate[:3] != word[0] + word[:2]:  # "om..." or "oom..."
        return False
    if abs(len(candidate) - len(word)) > 3:
        return False
    return SequenceMatcher(None, candidate, word).ratio() >= threshold


def contains_safe_word(text: str, word: str, threshold: float = 0.85) -> bool:
    word = normalize(word).replace(" ", "")
    if not word:
        return False
    tokens = normalize(text).split()
    for i, tok in enumerate(tokens):
        if _close(tok, word, threshold):
            return True
        # Split by the recogniser: "o merta", "omer ta"
        if i + 1 < len(tokens) and len(tok) <= 4 and _close(tok + tokens[i + 1], word, threshold):
            return True
    return False


def find_safe_word(segments: list[Segment], word: str, speakers: tuple[str, ...] = ("Me",),
                   max_no_speech: float = 0.6) -> Segment | None:
    """First segment (by time) spoken by one of `speakers` that contains the
    safe word. Segments Whisper thinks are probably not speech are ignored,
    so a hallucination on silence cannot stop the recording."""
    if not word.strip():
        return None
    for seg in sorted(segments, key=lambda s: s.start):
        if seg.speaker in speakers and seg.no_speech <= max_no_speech \
                and contains_safe_word(seg.text, word):
            return seg
    return None

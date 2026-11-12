"""Local meeting statistics from the transcript: no API call."""
from __future__ import annotations

from .transcribe import Segment


def talk_stats(segments: list[Segment], duration_s: float) -> dict:
    me = sum(s.end - s.start for s in segments if s.speaker == "Me")
    others = sum(s.end - s.start for s in segments if s.speaker == "Others")
    total = me + others or 1.0
    questions_me = sum(1 for s in segments if s.speaker == "Me" and "?" in s.text)
    questions_others = sum(1 for s in segments if s.speaker == "Others" and "?" in s.text)
    # longest stretch of one side talking without the other side (merged consecutive segments)
    longest = {"Me": 0.0, "Others": 0.0}
    run_speaker, run_start, run_end = None, 0.0, 0.0
    for s in sorted(segments, key=lambda x: x.start):
        if s.speaker not in ("Me", "Others"):
            continue
        if s.speaker == run_speaker and s.start - run_end < 3.0:
            run_end = s.end
        else:
            if run_speaker:
                longest[run_speaker] = max(longest[run_speaker], run_end - run_start)
            run_speaker, run_start, run_end = s.speaker, s.start, s.end
    if run_speaker:
        longest[run_speaker] = max(longest[run_speaker], run_end - run_start)
    words = sum(len(s.text.split()) for s in segments)
    return {
        "duration_s": round(duration_s),
        "talk_me_pct": round(100 * me / total) if (me or others) else None,
        "talk_others_pct": round(100 * others / total) if (me or others) else None,
        "questions_me": questions_me,
        "questions_others": questions_others,
        "longest_monologue_me_s": round(longest["Me"]),
        "longest_monologue_others_s": round(longest["Others"]),
        "words": words,
        "words_per_minute": round(words / (duration_s / 60)) if duration_s >= 60 else None,
    }

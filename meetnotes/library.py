"""Reading back past meetings: list, search, and context for `meetnotes ask`."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MeetingFolder:
    path: Path
    meta: dict

    @property
    def title(self) -> str:
        return self.meta.get("notes_title") or self.meta.get("title") or self.path.name

    @property
    def label(self) -> str:
        return f"{self.path.name[:15].replace('_', ' ')} {self.title}"


def meetings(root: Path) -> list[MeetingFolder]:
    """Newest first."""
    if not root.exists():
        return []
    out = []
    for path in sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")), reverse=True):
        meta = {}
        try:
            meta = json.loads((path / "meta.json").read_text())
        except Exception:
            pass
        out.append(MeetingFolder(path, meta))
    return out


def search(root: Path, term: str, limit: int = 50) -> list[tuple[MeetingFolder, str, str]]:
    """Case-insensitive search in notes and transcripts: (meeting, file, line)."""
    term_l = term.lower()
    hits = []
    for m in meetings(root):
        for name in ("notes.md", "transcript.md"):
            f = m.path / name
            if not f.exists():
                continue
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if term_l in line.lower():
                    hits.append((m, name, line.strip()))
                    if len(hits) >= limit:
                        return hits
    return hits


def ask_context(root: Path, last: int = 20, budget_chars: int = 200_000) -> list[tuple[str, str]]:
    """Notes of the last `last` meetings, then their transcripts (newest
    first) while the character budget lasts."""
    chosen = meetings(root)[:last]
    docs: dict[Path, list[str]] = {}
    used = 0
    for m in chosen:
        notes = m.path / "notes.md"
        if notes.exists():
            text = notes.read_text(encoding="utf-8", errors="replace")
            docs.setdefault(m.path, []).append(text)
            used += len(text)
    for m in chosen:
        tr = m.path / "transcript.md"
        if tr.exists():
            text = tr.read_text(encoding="utf-8", errors="replace")
            if used + len(text) > budget_chars:
                continue
            docs.setdefault(m.path, []).append(text)
            used += len(text)
    return [(m.label, "\n\n".join(docs[m.path])) for m in chosen if m.path in docs]


def level(samples) -> float:
    """Peak-ish level 0..1 of int16 samples (99th percentile, ignores clicks)."""
    import numpy as np
    if len(samples) == 0:
        return 0.0
    return float(np.percentile(np.abs(samples.astype(np.float32)), 99) / 32768.0)


def meter(value: float, width: int = 30) -> str:
    filled = min(width, int(round(min(1.0, value * 4) * width)))  # x4: speech rarely peaks above 0.25
    return "█" * filled + "·" * (width - filled)


def audio_verdict(mic_peak: float, sys_peak: float, have_system: bool) -> list[str]:
    msgs = []
    if mic_peak < 0.01:
        msgs.append("MIC: silent. Check mic_device and the OS microphone permission for your terminal.")
    else:
        msgs.append("MIC: OK, your voice is coming through.")
    if not have_system:
        msgs.append("OTHERS: no system-audio source. Follow the audio setup for your OS (meetnotes setup).")
    elif sys_peak < 0.005:
        msgs.append("OTHERS: silent. Was something playing in your headphones? If yes, the meeting app or "
                    "browser is sending sound to a device meetnotes is not listening to: set its speaker "
                    "to the system default (macOS: the Multi-Output Device).")
    else:
        msgs.append("OTHERS: OK, what plays in your headphones/speakers is captured.")
    return msgs


def find_related(root: Path, keywords: list[str], limit: int = 5,
                 exclude: set[str] | None = None) -> list[MeetingFolder]:
    """Past meetings that mention any keyword (title, notes or transcript),
    newest first. Short or empty keywords are ignored."""
    keys = [k.strip().lower() for k in keywords if len(k.strip()) >= 3]
    if not keys:
        return []
    out = []
    for m in meetings(root):
        if exclude and m.path.name in exclude:
            continue
        hay = m.title.lower() + " " + m.path.name.lower()
        for name in ("notes.md", "transcript.md"):
            f = m.path / name
            if f.exists():
                hay += " " + f.read_text(encoding="utf-8", errors="replace").lower()
        if any(k in hay for k in keys):
            out.append(m)
            if len(out) >= limit:
                break
    return out

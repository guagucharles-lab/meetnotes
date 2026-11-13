"""Translating a meeting: the transcript line by line (timestamps and
speakers kept), the notes as a whole, and live captions during the meeting.

Files written next to the originals: transcript.<code>.md, notes.<code>.md.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
from pathlib import Path

from .summarize import FALLBACK_BETA, _client
from .transcribe import fmt_ts

log = logging.getLogger("meetnotes")

# code -> name used in the prompt; the app shows these in Finnish.
LANGUAGES = {
    "en": "English", "fi": "Finnish", "sv": "Swedish", "de": "German", "fr": "French",
    "es": "Spanish", "et": "Estonian", "ru": "Russian", "uk": "Ukrainian", "it": "Italian",
    "nl": "Dutch", "pl": "Polish", "pt": "Portuguese", "no": "Norwegian", "da": "Danish",
    "ja": "Japanese", "zh": "Chinese", "ar": "Arabic", "vi": "Vietnamese",
}

LINES_SYSTEM = """You translate meeting transcript lines. Each input item is one
spoken line; translate its text into {lang}. Keep names, numbers, product names
and technical terms as they are. Speech recognition output can contain errors:
translate what was most likely meant, but don't add anything that wasn't said.
If a line is already in {lang}, return it unchanged. Return every item, with
the same id."""

LINES_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["id", "text"], "additionalProperties": False}}},
    "required": ["lines"], "additionalProperties": False,
}

NOTES_SYSTEM = """Translate these meeting notes into {lang}. Keep the Markdown
structure exactly (headings, lists, tables, bold), names, numbers, dates and
times. Translate headings too. Output only the translated Markdown."""

_LINE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\]\s*(?:(Me|Others):)?\s*(.*)$")
BATCH = 120  # transcript lines per request


def lang_name(code: str) -> str:
    if code not in LANGUAGES:
        raise ValueError(f"unknown language {code!r}")
    return LANGUAGES[code]


def translate_lines(texts: list[str], code: str, model: str, effort: str = "low", client=None) -> list[str]:
    """Translate a list of lines; returns the same number of lines. A line the
    model leaves out stays in the original language rather than shifting the rest."""
    if not texts:
        return []
    client = client or _client()
    out: list[str] = []
    system = LINES_SYSTEM.format(lang=lang_name(code))
    for start in range(0, len(texts), BATCH):
        part = texts[start:start + BATCH]
        items = [{"id": i, "text": t} for i, t in enumerate(part)]
        with client.beta.messages.stream(
            model=model,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": json.dumps({"lines": items}, ensure_ascii=False)}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": LINES_SCHEMA}},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined to translate this transcript.")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("The translation was cut off (max_tokens).")
        text = next(b.text for b in response.content if b.type == "text")
        got = {int(x["id"]): x["text"] for x in json.loads(text)["lines"]}
        out.extend(got.get(i, t) for i, t in enumerate(part))
    return out


def translate_markdown(md: str, code: str, model: str, effort: str = "low", client=None) -> str:
    client = client or _client()
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        system=NOTES_SYSTEM.format(lang=lang_name(code)),
        messages=[{"role": "user", "content": md}],
        output_config={"effort": effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to translate these notes.")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def translate_transcript_text(transcript: str, code: str, model: str, effort: str = "low", client=None) -> str:
    """transcript.md text -> the same file in another language. Timestamps,
    speaker labels and notes such as "(recording paused)" stay as they are."""
    rows, todo = [], []
    for line in transcript.splitlines():
        m = _LINE_RE.match(line)
        if not m or not m.group(3).strip() or re.fullmatch(r"\(.*\)", m.group(3).strip()):
            rows.append((line, None))
            continue
        rows.append((m, len(todo)))
        todo.append(m.group(3).strip())
    done = translate_lines(todo, code, model, effort, client)
    out = []
    for row, idx in rows:
        if idx is None:
            out.append(row)
        else:
            ts, who = row.group(1), row.group(2)
            out.append(f"[{ts}]" + (f" {who}:" if who else "") + f" {done[idx]}")
    return "\n".join(out) + "\n"


def translate_meeting(folder: Path, cfg, code: str, client=None) -> dict:
    """Write transcript.<code>.md and notes.<code>.md for a finished meeting."""
    from .config import write_json
    lang_name(code)
    meta_path = folder / "meta.json"
    written = {}
    tr = folder / "transcript.md"
    if tr.exists() and tr.read_text(encoding="utf-8").strip():
        out = folder / f"transcript.{code}.md"
        out.write_text(translate_transcript_text(tr.read_text(encoding="utf-8"), code, cfg.claude_model,
                                                 "low", client), encoding="utf-8")
        written["transcript"] = out.name
    notes = folder / "notes.md"
    if notes.exists() and notes.read_text(encoding="utf-8").strip():
        out = folder / f"notes.{code}.md"
        out.write_text(translate_markdown(notes.read_text(encoding="utf-8"), code, cfg.claude_model,
                                          "low", client) + "\n", encoding="utf-8")
        written["notes"] = out.name
    data = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    langs = [c for c in data.get("translations", []) if c != code] + [code]
    data["translations"] = langs
    data.pop("translating", None)
    data.pop("translate_error", None)
    if meta_path.exists():
        write_json(meta_path, data)
    log.info("Translated %s into %s: %s", folder.name, LANGUAGES[code], ", ".join(written.values()) or "nothing")
    return written


def translations(folder: Path) -> dict:
    """{code: {"name", "notes", "transcript"}} for the meeting page."""
    out = {}
    for f in sorted(folder.glob("*.*.md")):
        kind, code = f.name.split(".")[:2]
        if kind in ("notes", "transcript") and code in LANGUAGES:
            out.setdefault(code, {"name": LANGUAGES[code], "notes": "", "transcript": ""})[kind] = \
                f.read_text(encoding="utf-8", errors="replace")
    return out


class LiveTranslator:
    """Translates new transcript lines during the meeting, a few seconds after
    they are transcribed. Lines are batched, so a busy meeting is still one
    request every few seconds."""

    def __init__(self, code: str, model: str, client=None, wait: float = 2.0) -> None:
        lang_name(code)
        self.code = code
        self.model = model
        self.client = client
        self.wait = wait
        self.lines: list[dict] = []   # {"t", "who", "text", "src"}
        self.error = ""
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="live-translate")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def add(self, segments) -> None:
        for s in segments:
            if s.text.strip() and not re.fullmatch(r"\(.*\)", s.text.strip()):
                self._q.put(s)

    def snapshot(self, last: int = 60) -> list[dict]:
        with self._lock:
            return list(self.lines[-last:])

    def save(self, folder: Path) -> None:
        with self._lock:
            if not self.lines:
                return
            text = "\n".join(f"[{x['t']}]" + (f" {x['who']}:" if x["who"] else "") + f" {x['text']}" for x in self.lines)
        (folder / f"live-translation.{self.code}.md").write_text(text + "\n", encoding="utf-8")

    def _run(self) -> None:
        while not (self._stop.is_set() and self._q.empty()):
            try:
                first = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = [first]
            self._stop.wait(self.wait)  # gather what arrives in the next seconds
            while True:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            try:
                done = translate_lines([s.text for s in batch], self.code, self.model, "low", self.client)
                self.error = ""
            except Exception as exc:  # shown in the app; the meeting goes on
                log.warning("live translation failed: %s", exc)
                self.error = str(exc)[:200]
                done = [s.text for s in batch]
            with self._lock:
                for s, t in zip(batch, done):
                    self.lines.append({"t": fmt_ts(s.start), "who": s.speaker, "text": t, "src": s.text})

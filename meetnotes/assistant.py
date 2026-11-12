"""The meeting assistant ("Avustaja").

Three moments share one preparation ("briefing"):

* before:  prepare(): Claude reads the briefing (agenda, goals, materials) and
           the related past meetings, and writes a brief. chat() answers
           questions about it ("what did we agree with Kontio last time?").
* during:  LiveAssistant follows the transcript as it grows. Every
           `interval` seconds the new lines go to Claude in ONE append-only
           conversation, so earlier context is served from the prompt cache
           and suggestions are not repeated. It tracks the agenda, suggests
           what to bring up, flags decisions and tasks, and answers questions.
* after:   the agenda status goes into the notes.

Only transcript text and the briefing leave the computer, to the Anthropic API.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from .library import find_related, meetings
from .summarize import FALLBACK_BETA, _client

log = logging.getLogger("meetnotes")
BRIEFINGS_DIR = "_briefings"
STOPWORDS = {"meeting", "palaveri", "palsu", "kokous", "call", "sync", "weekly", "viikko", "with", "kanssa",
             "the", "and", "for", "about", "team", "tiimi", "review", "update", "catch", "daily"}


# --------------------------------------------------------------------------
# Briefings (stored as JSON under <notes>/_briefings/)
# --------------------------------------------------------------------------

@dataclass
class Briefing:
    id: str
    title: str
    when: str = ""                     # "2026-11-14", "2026-11-14 10:00" or free text
    agenda: list[str] = field(default_factory=list)
    goals: str = ""
    participants: list[str] = field(default_factory=list)
    materials: str = ""                # pasted text: emails, offers, numbers
    keywords: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)   # meeting folder ids chosen by hand ("" = automatic)
    prep: str = ""                     # the brief, Markdown
    prep_status: str = ""              # "" | preparing | done | failed
    prep_error: str = ""
    chat: list[dict] = field(default_factory=list)      # [{"role": "user"|"assistant", "text": ..., "at": ...}]
    created: str = ""
    used_by: str = ""                  # meeting folder this briefing was used in
    template: str = ""                 # notes template id ("" = config default)
    event_uid: str = ""                # calendar event this was made from

    def search_keywords(self) -> list[str]:
        words = [w for w in re.split(r"[^\w]+", self.title) if len(w) >= 4 and w.lower() not in STOPWORDS]
        return list(dict.fromkeys(words + self.participants + self.keywords))

    def date(self) -> str:
        m = re.search(r"\d{4}-\d{2}-\d{2}", self.when)
        if m:
            return m.group(0)
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", self.when)   # 14.11.2026
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else ""


def briefings_dir(root: Path) -> Path:
    d = root / BRIEFINGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_briefing(root: Path, **fields) -> Briefing:
    b = Briefing(id=f"{datetime.now():%Y%m%d}-{secrets.token_hex(3)}", title="", created=datetime.now().isoformat(timespec="seconds"))
    update_briefing(b, fields)
    save_briefing(root, b)
    return b


def update_briefing(b: Briefing, fields: dict) -> None:
    for key in ("title", "when", "goals", "materials", "used_by", "template", "event_uid"):
        if key in fields:
            setattr(b, key, str(fields[key] or "").strip())
    for key in ("agenda", "participants", "keywords", "related"):
        if key in fields:
            v = fields[key]
            if isinstance(v, str):
                v = [x.strip() for x in re.split(r"[\n,;]+" if key != "agenda" else r"\n+", v)]
            setattr(b, key, [re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", str(x)).strip() for x in v if str(x).strip()])
    if not b.title:
        b.title = "Kokous"


def save_briefing(root: Path, b: Briefing) -> None:
    from .config import write_json
    write_json(briefings_dir(root) / f"{b.id}.json", asdict(b))


def load_briefing(root: Path, bid: str) -> Briefing:
    if not re.fullmatch(r"[\w-]{1,40}", bid):
        raise KeyError(bid)
    path = briefings_dir(root) / f"{bid}.json"
    if not path.exists():
        raise KeyError(bid)
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f for f in Briefing.__dataclass_fields__}
    return Briefing(**{k: v for k, v in data.items() if k in known})


def delete_briefing(root: Path, bid: str) -> None:
    load_briefing(root, bid)
    (briefings_dir(root) / f"{bid}.json").unlink()


def list_briefings(root: Path) -> list[Briefing]:
    out = []
    for path in briefings_dir(root).glob("*.json"):
        try:
            out.append(load_briefing(root, path.stem))
        except Exception:
            continue
    # upcoming first (by date, then newest created)
    return sorted(out, key=lambda b: (b.date() or "9999", b.created), reverse=True)


def match_briefing(briefings: list[Briefing], title: str, today: str) -> Briefing | None:
    """Which briefing belongs to the meeting that just started? Same day and
    a keyword in the window title, else the only briefing of the day."""
    words = {w.lower() for w in re.split(r"[^\w]+", title or "") if len(w) >= 3}
    best, best_score = None, 0
    todays = []
    for b in briefings:
        same_day = b.date() == today
        if same_day and not b.used_by:
            todays.append(b)
        overlap = sum(1 for k in b.search_keywords() if k.lower() in words)
        score = overlap * 2 + (1 if same_day else 0)
        if overlap and score > best_score:
            best, best_score = b, score
    if best is not None:
        return None if best.used_by else best   # its meeting was already recorded: don't attach another
    return todays[0] if len(todays) == 1 else None


# --------------------------------------------------------------------------
# Context for Claude
# --------------------------------------------------------------------------

def related_meetings(root: Path, b: Briefing, limit: int = 5):
    if b.related:
        by_id = {m.path.name: m for m in meetings(root)}
        return [by_id[i] for i in b.related if i in by_id]
    return find_related(root, b.search_keywords(), limit=limit, exclude={b.used_by} if b.used_by else None)


def briefing_context(root: Path, b: Briefing | None, budget_chars: int = 150_000,
                     title: str = "", generic_past: int = 0) -> str:
    """<briefing> + <meeting> blocks of related past meetings."""
    parts = []
    if b is not None:
        agenda = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(b.agenda)) or "(no agenda given)"
        parts.append("<briefing>\n"
                     f"Title: {b.title}\nWhen: {b.when or 'not given'}\n"
                     f"Participants: {', '.join(b.participants) or 'not given'}\n"
                     f"Goals: {b.goals or 'not given'}\nAgenda:\n{agenda}\n"
                     + (f"Materials:\n{b.materials}\n" if b.materials else "") + "</briefing>")
        past = related_meetings(root, b)
    else:
        past = find_related(root, [w for w in re.split(r"[^\w]+", title) if len(w) >= 4], limit=generic_past) if title else []
    if b is not None:
        from .tasks import open_tasks_text
        tasks = open_tasks_text(root, b.search_keywords())
        if tasks:
            parts.append("<open_tasks>\n" + tasks + "\n</open_tasks>")
    used = sum(len(p) for p in parts)
    for m in past:
        notes = (m.path / "notes.md").read_text(encoding="utf-8", errors="replace") if (m.path / "notes.md").exists() else ""
        text = notes
        tr = m.path / "transcript.md"
        if tr.exists():
            t = tr.read_text(encoding="utf-8", errors="replace")
            if used + len(text) + len(t) <= budget_chars:
                text += "\n\n" + t
        if not text.strip():
            continue
        block = f'<meeting name="{m.title}" date="{m.meta.get("started_at", "")[:10]}">\n{text}\n</meeting>'
        if used + len(block) > budget_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Before the meeting: brief + chat
# --------------------------------------------------------------------------

PREP_SYSTEM = """You prepare the user for an upcoming meeting. You get a briefing
(agenda, goals, materials) and the user's notes and transcripts from related
past meetings. Lines labelled "Me:" are the user.

Write a brief the user can read in two minutes: what was agreed or left open
before, what to bring up, what to ask, and what could go wrong. Be concrete:
names, numbers, dates, exactly as in the sources. Never invent commitments or
facts that are not in the sources; say when something is unknown. Write in the
language of the briefing (or of the past meetings if the briefing is empty)."""

PREP_SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "description": "ISO 639-1 code of the language you wrote in"},
        "summary": {"type": "string", "description": "3-5 sentences: where things stand before this meeting"},
        "history": {"type": "array", "items": {"type": "string"}, "description": "What was agreed or said before, with the meeting and date"},
        "open_items": {"type": "array", "items": {"type": "string"}, "description": "Unresolved items and unmet commitments"},
        "talking_points": {"type": "array", "items": {"type": "string"}, "description": "What the user should bring up, in a sensible order"},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "Questions the user should ask"},
        "risks": {"type": "array", "items": {"type": "string"}, "description": "What could go wrong or needs care"},
    },
    "required": ["language", "summary", "history", "open_items", "talking_points", "questions", "risks"],
    "additionalProperties": False,
}


class PrepBrief(BaseModel):
    language: str = "en"
    summary: str
    history: list[str]
    open_items: list[str]
    talking_points: list[str]
    questions: list[str]
    risks: list[str]


PREP_LABELS = {
    "en": {"summary": "Where we stand", "history": "What was said before", "open": "Open items",
           "points": "Bring up", "questions": "Ask", "risks": "Watch out for", "none": "_Nothing._"},
    "fi": {"summary": "Tilanne ennen kokousta", "history": "Mitä aiemmin on sovittu", "open": "Avoinna",
           "points": "Ota esiin", "questions": "Kysy", "risks": "Varo", "none": "_Ei mitään._"},
    "sv": {"summary": "Läget inför mötet", "history": "Vad som sagts tidigare", "open": "Öppet",
           "points": "Ta upp", "questions": "Fråga", "risks": "Se upp för", "none": "_Inget._"},
}


def render_prep(brief: PrepBrief) -> str:
    L = PREP_LABELS.get((brief.language or "en")[:2].lower(), PREP_LABELS["en"])
    lines = [f"## {L['summary']}", "", brief.summary, ""]
    for key, items in (("history", brief.history), ("open", brief.open_items), ("points", brief.talking_points),
                       ("questions", brief.questions), ("risks", brief.risks)):
        lines += [f"## {L[key]}", ""]
        lines += [f"- {x}" for x in items] or [L["none"]]
        lines.append("")
    return "\n".join(lines)


def prepare(root: Path, b: Briefing, model: str = "claude-opus-5-5", client=None) -> str:
    """Write (or rewrite) the brief. Returns Markdown and stores it on the briefing."""
    client = client or _client()
    context = briefing_context(root, b)
    with client.beta.messages.stream(
        model=model, max_tokens=16000, system=PREP_SYSTEM,
        messages=[{"role": "user", "content": context + "\n\nWrite the brief for this meeting."}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": PREP_SCHEMA}},
        betas=[FALLBACK_BETA], fallbacks="default",
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this brief.")
    text = next(blk.text for blk in response.content if blk.type == "text")
    brief = PrepBrief.model_validate(json.loads(text))
    b.prep = render_prep(brief)
    b.prep_status, b.prep_error = "done", ""
    return b.prep


CHAT_SYSTEM = """You are the user's meeting assistant. You know the briefing of an
upcoming meeting and the user's notes and transcripts from related past
meetings ("Me:" is the user). Answer their questions from that material only,
name which meeting and date a fact comes from, and say plainly when the
material does not answer. Short, direct answers in the user's language."""


def chat(root: Path, b: Briefing, question: str, model: str = "claude-opus-5-5", client=None) -> str:
    """Ask about the meeting before it happens. History lives on the briefing."""
    client = client or _client()
    context = briefing_context(root, b)
    history = [{"role": m["role"], "content": m["text"]} for m in b.chat[-20:]]
    system = [{"type": "text", "text": CHAT_SYSTEM + "\n\n" + context, "cache_control": {"type": "ephemeral"}}]
    with client.beta.messages.stream(
        model=model, max_tokens=4000, system=system,
        messages=history + [{"role": "user", "content": question}],
        output_config={"effort": "medium"},
        betas=[FALLBACK_BETA], fallbacks="default",
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to answer.")
    answer = "".join(blk.text for blk in response.content if blk.type == "text").strip()
    now = datetime.now().isoformat(timespec="seconds")
    b.chat += [{"role": "user", "text": question, "at": now}, {"role": "assistant", "text": answer, "at": now}]
    return answer


# --------------------------------------------------------------------------
# During the meeting
# --------------------------------------------------------------------------

LIVE_SYSTEM = """You sit in on a live meeting as the user's private assistant. The
user sees your output on their own screen; nobody else does. You get the
briefing (agenda, goals, materials), related past meetings, and then the live
transcript in pieces as it is spoken. "Me:" is the user, "Others:" everyone
else. The transcript is automatic speech recognition: expect errors, use context.

Each update, return:
- agenda: the status of every agenda item (keep the item text exactly).
- nudges: things worth the user's attention RIGHT NOW. Kinds:
    bring_up  - a point from the briefing or past meetings the user should raise
                (an agenda item being skipped, a past commitment nobody mentioned,
                a number that contradicts the materials)
    ask       - a question the user should ask now
    heads_up  - something just said that conflicts with the facts you have, or a risk
    decision  - a decision was just made; state it so the user can confirm it
    action    - a task was just assigned, especially to the user
  Each nudge: one sentence the user can act on, and a short "why" naming the
  source (meeting and date, or the materials). Only nudges you have NOT given
  before in this conversation. Zero nudges is a fine answer. Never invent facts.
- answer: if the user asked a question in this update, answer it from the
  transcript so far, the briefing and the past meetings; else "".

Write in the language spoken in the meeting."""

STYLE_RULES = {
    "quiet": "Style: quiet. Only kinds decision and action, plus heads_up for clear contradictions. Answer questions fully.",
    "balanced": "Style: balanced. At most 2 nudges per update; prefer what the user would otherwise miss.",
    "active": "Style: active. Up to 4 nudges per update, including bring_up and ask suggestions whenever they help.",
}

LIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "agenda": {"type": "array", "items": {
            "type": "object",
            "properties": {"item": {"type": "string"},
                           "status": {"type": "string", "enum": ["not_started", "in_progress", "done", "skipped"]}},
            "required": ["item", "status"], "additionalProperties": False}},
        "nudges": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["bring_up", "ask", "heads_up", "decision", "action"]},
                           "text": {"type": "string"}, "why": {"type": "string"}},
            "required": ["kind", "text", "why"], "additionalProperties": False}},
        "answer": {"type": "string"},
    },
    "required": ["agenda", "nudges", "answer"],
    "additionalProperties": False,
}


class AgendaStatus(BaseModel):
    item: str
    status: str


class Nudge(BaseModel):
    kind: str
    text: str
    why: str = ""


class LiveUpdate(BaseModel):
    agenda: list[AgendaStatus]
    nudges: list[Nudge]
    answer: str = ""


class LiveAssistant:
    def __init__(self, context: str, title: str, style: str = "balanced", interval: int = 45,
                 model: str = "claude-opus-5-5", effort: str = "low", client=None,
                 clock=time.monotonic, briefing_title: str = "") -> None:
        self.context = context
        self.title = title
        self.briefing_title = briefing_title
        self.style = style if style in STYLE_RULES else "balanced"
        self.interval = max(15, int(interval))
        self.model = model
        self.effort = effort
        self._client = client
        self._clock = clock
        self.messages: list[dict] = []          # append-only conversation
        self._pending: list[str] = []
        self._pending_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_call = 0.0
        self.calls = 0
        self.feed: list[dict] = []              # newest last
        self.agenda: list[dict] = []
        self.error = ""
        self.usage = {"input": 0, "cached": 0, "output": 0}

    # -- input ---------------------------------------------------------------

    def add_lines(self, lines: list[str]) -> None:
        with self._pending_lock:
            self._pending.extend(l for l in lines if l.strip())

    def due(self) -> bool:
        with self._pending_lock:
            if not self._pending:
                return False
        return self._clock() - self.last_call >= self.interval

    # -- background loop -----------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="assistant", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            if self.due():
                try:
                    self.update()
                except Exception as exc:
                    self.error = str(exc)[:300]
                    log.warning("assistant update failed: %s", exc)
                    self.last_call = self._clock()  # don't hammer the API after an error

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=120)
            self._thread = None

    # -- the call ------------------------------------------------------------

    def ask(self, question: str) -> str:
        """Answer a question now, together with any transcript that is waiting."""
        return self.update(question=question).answer

    def update(self, question: str = "") -> LiveUpdate:
        with self._call_lock:
            with self._pending_lock:
                lines, self._pending = self._pending, []
            if not lines and not question:
                return LiveUpdate(agenda=[AgendaStatus(**a) for a in self.agenda], nudges=[], answer="")
            parts = []
            if not self.messages:
                parts.append(f"The meeting '{self.title}' has started. Watch the transcript below.")
            if lines:
                parts.append("<transcript_update>\n" + "\n".join(lines) + "\n</transcript_update>")
            else:
                parts.append("(no new transcript)")
            if question:
                parts.append(f"<question>\n{question.strip()}\n</question>")
            self.messages.append({"role": "user", "content": "\n\n".join(parts)})
            try:
                result = self._call()
            except Exception:
                self.messages.pop()  # keep the conversation valid for the next try
                with self._pending_lock:
                    self._pending[:0] = lines  # and send these lines again next time
                raise
            self.messages.append({"role": "assistant", "content": result.model_dump_json()})
            self.last_call = self._clock()
            self.calls += 1
            self.error = ""
            at = datetime.now().isoformat(timespec="seconds")
            if result.agenda:
                self.agenda = [a.model_dump() for a in result.agenda]
            for n in result.nudges:
                self.feed.append({"at": at, "kind": n.kind, "text": n.text, "why": n.why})
            if question:
                self.feed.append({"at": at, "kind": "question", "text": question.strip(), "why": ""})
                self.feed.append({"at": at, "kind": "answer", "text": result.answer, "why": ""})
            return result

    def _call(self) -> LiveUpdate:
        client = self._client or _client()
        if hasattr(client, "with_options"):
            client = client.with_options(timeout=90.0, max_retries=1)
        system = [{"type": "text", "text": LIVE_SYSTEM + "\n\n" + STYLE_RULES[self.style]
                   + ("\n\n" + self.context if self.context else ""),
                   "cache_control": {"type": "ephemeral"}}]
        with client.beta.messages.stream(
            model=self.model, max_tokens=4000, system=system, messages=list(self.messages),
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": LIVE_SCHEMA}},
            cache_control={"type": "ephemeral"},   # the growing, append-only history
            betas=[FALLBACK_BETA], fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this update.")
        u = getattr(response, "usage", None)
        if u is not None:
            self.usage["input"] += getattr(u, "input_tokens", 0) or 0
            self.usage["cached"] += getattr(u, "cache_read_input_tokens", 0) or 0
            self.usage["output"] += getattr(u, "output_tokens", 0) or 0
        text = next(blk.text for blk in response.content if blk.type == "text")
        return LiveUpdate.model_validate(json.loads(text))

    # -- output --------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._pending_lock:
            waiting = len(self._pending)
        return {"feed": list(self.feed), "agenda": list(self.agenda), "calls": self.calls,
                "error": self.error, "waiting_lines": waiting, "style": self.style,
                "briefing": self.briefing_title, "interval": self.interval,
                "next_in": max(0, int(self.interval - (self._clock() - self.last_call))) if waiting else None}

    def agenda_summary(self) -> str:
        if not self.agenda:
            return ""
        words = {"done": "covered", "in_progress": "partly covered", "not_started": "NOT covered", "skipped": "skipped"}
        return "\n".join(f"- {a['item']}: {words.get(a['status'], a['status'])}" for a in self.agenda)

    def save(self, folder: Path) -> None:
        from .config import write_json
        write_json(folder / "assistant.json", {"briefing": self.briefing_title, "agenda": self.agenda,
                                               "feed": self.feed, "calls": self.calls, "usage": self.usage})
        lines = [f"# Avustaja: {self.title}", ""]
        if self.agenda:
            lines += ["## Agenda", ""] + [f"- {a['item']} ({a['status']})" for a in self.agenda] + [""]
        lines += ["## Kokouksen aikana", ""]
        for f in self.feed:
            lines.append(f"- **{f['kind']}** {f['at'][11:16]}: {f['text']}" + (f" _({f['why']})_" if f.get("why") else ""))
        (folder / "assistant.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def briefing_from_event(root: Path, event) -> Briefing:
    """A briefing prefilled from a calendar event (title, time, attendees,
    agenda lines from the invite text). Reuses one made from the same event."""
    from .cal import agenda_from_description
    for b in list_briefings(root):
        if b.event_uid and b.event_uid == event.uid:
            return b
    when = event.start.replace("T", " ")
    b = new_briefing(root, title=event.title, when=when, participants=event.attendees,
                     agenda=agenda_from_description(event.description),
                     materials=event.description[:20_000], event_uid=event.uid)
    return b

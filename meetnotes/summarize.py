"""Turn a transcript into meeting notes with Claude."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import anthropic
from pydantic import BaseModel, Field

from .transcribe import fmt_ts

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ActionItem(BaseModel):
    task: str
    owner: str = Field(description="Person responsible, or 'unassigned'")
    due: str = Field(description="Deadline as said in the meeting, or 'not specified'")


class MeetingNotes(BaseModel):
    language: str = Field(default="en", description="ISO 639-1 code of the language the notes are written in, e.g. 'fi'")
    title: str = Field(description="Short meeting title inferred from the discussion")
    summary: str = Field(description="2-4 sentence overview of what the meeting was about and how it ended")
    key_points: list[str] = Field(description="The main things discussed, one per bullet")
    decisions: list[str] = Field(description="Decisions that were actually made (empty if none)")
    action_items: list[ActionItem]
    open_questions: list[str] = Field(description="Unresolved questions or topics parked for later")
    my_followups: list[str] = Field(description="Things the user ('Me' in the transcript) committed to or should do next")
    participants: list[str] = Field(description="Names mentioned as being present, if any")
    chapters: list["Chapter"] = Field(default_factory=list, description="The meeting's topics in order, with the transcript time each began")
    followups_from_last_time: list["Followup"] = Field(default_factory=list, description="Status of items carried over from the previous meeting")
    sections: list["Section"] = Field(default_factory=list, description="Extra sections for this kind of meeting")


class Chapter(BaseModel):
    start: str = Field(description="HH:MM:SS from the transcript where this topic began")
    title: str


class Followup(BaseModel):
    item: str
    status: str = Field(description="done | open | not_mentioned")
    note: str = ""


class Section(BaseModel):
    title: str
    items: list[str]


# Explicit JSON schema (every field required, no extra properties) as the
# structured-output format demands.
NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "description": "ISO 639-1 code of the language you wrote the notes in, e.g. fi or en"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "owner": {"type": "string"},
                    "due": {"type": "string"},
                },
                "required": ["task", "owner", "due"],
                "additionalProperties": False,
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "my_followups": {"type": "array", "items": {"type": "string"}},
        "participants": {"type": "array", "items": {"type": "string"}},
        "chapters": {"type": "array", "description": "Topics in the order they were discussed; 3-8 for a normal meeting",
                     "items": {"type": "object",
                               "properties": {"start": {"type": "string", "description": "HH:MM:SS of the transcript line where it began"},
                                              "title": {"type": "string"}},
                               "required": ["start", "title"], "additionalProperties": False}},
        "followups_from_last_time": {"type": "array", "description": "Only when a <previous_meeting> is given: each of its action items and open questions",
                                     "items": {"type": "object",
                                               "properties": {"item": {"type": "string"},
                                                              "status": {"type": "string", "enum": ["done", "open", "not_mentioned"]},
                                                              "note": {"type": "string"}},
                                               "required": ["item", "status", "note"], "additionalProperties": False}},
        "sections": {"type": "array", "description": "Extra sections asked for by the meeting type; empty otherwise",
                     "items": {"type": "object",
                               "properties": {"title": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
                               "required": ["title", "items"], "additionalProperties": False}},
    },
    "required": ["language", "title", "summary", "key_points", "decisions", "action_items",
                 "open_questions", "my_followups", "participants", "chapters", "followups_from_last_time", "sections"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You write meeting notes from an automatic transcript of a video call.

The transcript was produced by speech recognition, so expect misheard words,
missing punctuation and occasional nonsense fragments. Use context to recover
the intended meaning and silently drop fragments that carry no information.
Lines labelled "Me:" were spoken by the user who owns these notes; lines
labelled "Others:" were spoken by the other participants (several people may
share that label). Unlabelled lines could be anyone.

Write for the user, who was in the meeting and wants a record they can act on.
Be concrete: keep names, numbers, dates and product names exactly as said.
Never invent decisions or commitments that are not supported by the transcript.
If the transcript is too short or too garbled to say something, say so in the
summary and leave the lists empty rather than guessing.
Write the notes in the language mostly spoken in the meeting unless the user
asks for another language. If a <briefing> with an agenda is given, say in
key_points which agenda items were covered and which were not, and treat the
briefing's goals and materials as context (they are not part of what was said).
If <my_notes> are given, they are the user's own notes typed during the
meeting: treat them as the most reliable account of what mattered to the user,
work every point of them into the notes, and resolve anything the transcript
garbled using them. If <previous_meeting> is given, report in
followups_from_last_time what happened to each of its action items and open
questions (done / open / not mentioned). If <attendees> are given, use those
names when the transcript makes clear who is speaking, otherwise do not guess.
Lines such as "(recording paused)" mark a gap where
the user chose not to record, and "(recording stopped by safe word)" means the
user ended the recording there on purpose. Do not speculate about anything that
was said during a gap or after the recording stopped, and do not mention the
safe word itself in the notes."""

LIVE_SYSTEM_PROMPT = """You maintain running notes during a live video call from a partial,
automatic transcript. Lines labelled "Me:" are the user; "Others:" are the
other participants. Produce a concise Markdown bullet list of what has been
discussed so far, with a final "Open / to follow up" section. Keep names,
numbers and dates exact. Do not invent anything. Return only the Markdown."""


@dataclass
class MeetingMeta:
    title: str
    platform: str
    started_at: datetime
    duration_s: float

    def describe(self) -> str:
        return (f"Meeting title (from the app window): {self.title or 'unknown'}\n"
                f"Platform: {self.platform or 'unknown'}\n"
                f"Started: {self.started_at.strftime('%Y-%m-%d %H:%M')}\n"
                f"Duration: {fmt_ts(self.duration_s)}")


def _client() -> anthropic.Anthropic:
    # ANTHROPIC_API_KEY, else the key saved from the app in the OS credential
    # store, else whatever the SDK finds itself (e.g. an `ant auth login` profile).
    from .credentials import get_api_key
    key = get_api_key()
    # A stuck network shouldn't keep "writing notes" for half an hour.
    opts = {"timeout": 180.0, "max_retries": 2}
    return anthropic.Anthropic(api_key=key, **opts) if key else anthropic.Anthropic(**opts)


def summarize_transcript(transcript: str, meta: MeetingMeta, model: str = "claude-opus-5-5",
                         effort: str = "medium", extra_instructions: str = "",
                         client: anthropic.Anthropic | None = None,
                         notes_language: str = "", context: str = "") -> MeetingNotes:
    """One streamed request with a JSON-schema output format. Streaming keeps
    long transcripts from hitting HTTP timeouts."""
    client = client or _client()
    user = meta.describe()
    if notes_language.strip():
        user += f"\n\nWrite the notes in {notes_language.strip()}."
    if extra_instructions.strip():
        user += f"\n\nStanding instructions from the user:\n{extra_instructions.strip()}"
    if context.strip():
        user += f"\n\n{context.strip()}"
    user += f"\n\nTranscript:\n<transcript>\n{transcript}\n</transcript>"

    with client.beta.messages.stream(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": NOTES_SCHEMA}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        detail = getattr(getattr(response, "stop_details", None), "explanation", None) or "no detail"
        raise RuntimeError(f"Claude declined to summarise this transcript ({detail}).")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Notes were cut off (max_tokens). Try a shorter transcript.")

    text = next(b.text for b in response.content if b.type == "text")
    return MeetingNotes.model_validate(json.loads(text))


def live_notes(transcript_so_far: str, meta: MeetingMeta, model: str = "claude-opus-5-5",
               client: anthropic.Anthropic | None = None) -> str:
    client = client or _client()
    user = f"{meta.describe()}\n\nTranscript so far:\n<transcript>\n{transcript_so_far}\n</transcript>"
    with client.beta.messages.stream(
        model=model,
        max_tokens=4000,
        system=LIVE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "low"},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        return "_Live notes unavailable for this segment._"
    return "".join(b.text for b in response.content if b.type == "text").strip()


LABELS = {
    "en": {"summary": "Summary", "key_points": "Key points", "decisions": "Decisions",
           "actions": "Action items", "task": "Task", "owner": "Owner", "due": "Due",
           "followups": "My follow-ups", "open": "Open questions", "none": "_None._",
           "participants": "Participants mentioned", "untitled": "Meeting notes",
           "chapters": "Topics", "last_time": "Since last time", "status": {"done": "done", "open": "still open", "not_mentioned": "not mentioned"},
           "platforms": {"Phone (in person)": "Phone (in person)", "Manual": "Started manually"}},
    "fi": {"summary": "Yhteenveto", "key_points": "Keskeiset asiat", "decisions": "Päätökset",
           "actions": "Tehtävät", "task": "Tehtävä", "owner": "Vastuu", "due": "Määräaika",
           "followups": "Omat jatkotoimet", "open": "Avoimet kysymykset", "none": "_Ei mitään._",
           "participants": "Mainitut osallistujat", "untitled": "Muistiinpanot",
           "chapters": "Aiheet", "last_time": "Viime kerrasta", "status": {"done": "tehty", "open": "yhä auki", "not_mentioned": "ei puhuttu"},
           "platforms": {"Phone (in person)": "Puhelin (kasvokkain)", "Manual": "Käsin aloitettu"}},
    "sv": {"summary": "Sammanfattning", "key_points": "Viktiga punkter", "decisions": "Beslut",
           "actions": "Åtgärder", "task": "Uppgift", "owner": "Ansvarig", "due": "Deadline",
           "followups": "Mina uppföljningar", "open": "Öppna frågor", "none": "_Inget._",
           "participants": "Nämnda deltagare", "untitled": "Mötesanteckningar",
           "chapters": "Ämnen", "last_time": "Sedan sist", "status": {"done": "klart", "open": "fortfarande öppet", "not_mentioned": "inte nämnt"},
           "platforms": {"Phone (in person)": "Telefon (på plats)", "Manual": "Startad manuellt"}},
}
_FI_DAYS = ["maanantai", "tiistai", "keskiviikko", "torstai", "perjantai", "lauantai", "sunnuntai"]
_SV_DAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]


def _when(dt: datetime, lang: str) -> str:
    if lang == "fi":
        return f"{_FI_DAYS[dt.weekday()]} {dt.day}.{dt.month}.{dt.year} klo {dt:%H.%M}"
    if lang == "sv":
        return f"{_SV_DAYS[dt.weekday()]} {dt.day}/{dt.month} {dt.year} kl. {dt:%H.%M}"
    return dt.strftime("%A %d %B %Y, %H:%M")


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "< 1 min"
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def render_markdown(notes: MeetingNotes, meta: MeetingMeta) -> str:
    lang = (notes.language or "en").lower()[:2]
    L = LABELS.get(lang, LABELS["en"])
    platform = L["platforms"].get(meta.platform, meta.platform)
    lines = [f"# {notes.title or meta.title or L['untitled']}", ""]
    lines.append("*" + " · ".join(x for x in (_when(meta.started_at, lang), platform,
                                              _duration(meta.duration_s)) if x) + "*")
    if notes.participants:
        lines.append(f"*{L['participants']}: {', '.join(notes.participants)}*")
    lines += ["", f"## {L['summary']}", "", notes.summary, ""]
    if notes.chapters:
        lines += [f"## {L.get('chapters', 'Topics')}", ""]
        lines += [f"- {c.start} {c.title}" for c in notes.chapters] + [""]

    def section(title: str, items: list[str]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        lines.extend(f"- {item}" for item in items) if items else lines.append(L["none"])
        lines.append("")

    section(L["key_points"], notes.key_points)
    section(L["decisions"], notes.decisions)
    lines += [f"## {L['actions']}", ""]
    if notes.action_items:
        lines.append(f"| {L['task']} | {L['owner']} | {L['due']} |")
        lines.append("|------|-------|-----|")
        for item in notes.action_items:
            cells = [c.replace("|", "/") for c in (item.task, item.owner, item.due)]
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append(L["none"])
    lines.append("")
    section(L["followups"], notes.my_followups)
    section(L["open"], notes.open_questions)
    if notes.followups_from_last_time:
        lines += [f"## {L.get('last_time', 'Since last time')}", ""]
        st = L.get("status", {})
        for f in notes.followups_from_last_time:
            tail = f" – {f.note}" if f.note else ""
            lines.append(f"- {f.item}: **{st.get(f.status, f.status)}**{tail}")
        lines.append("")
    for sec in notes.sections:
        section(sec.title, sec.items)
    return "\n".join(lines)


ASK_SYSTEM_PROMPT = """You answer the user's questions about their own past meetings.
You get their meeting notes and transcripts. Use only that material. Mention
which meeting (date and title) each fact comes from. If the material does not
answer the question, say so plainly. Lines labelled "Me:" are the user.
Answer in the language of the question, briefly."""


def ask(question: str, documents: list[tuple[str, str]], model: str = "claude-opus-5-5",
        client: anthropic.Anthropic | None = None, write=None) -> str:
    """Answer a question across past meetings. `documents` is [(name, text)].
    Streams the answer through `write` (e.g. sys.stdout.write) as it arrives."""
    client = client or _client()
    corpus = "\n\n".join(f'<meeting name="{name}">\n{text}\n</meeting>' for name, text in documents)
    content = f"<meetings>\n{corpus}\n</meetings>\n\nQuestion: {question}"
    parts: list[str] = []
    with client.beta.messages.stream(
        model=model,
        max_tokens=8000,
        system=ASK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium"},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        for text in stream.text_stream:
            parts.append(text)
            if write:
                write(text)
        final = stream.get_final_message()
    if final.stop_reason == "refusal":
        raise RuntimeError("Claude declined to answer this question.")
    return "".join(parts)

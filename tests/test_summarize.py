import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from meetnotes.summarize import (MeetingMeta, MeetingNotes, NOTES_SCHEMA, render_markdown,
                                 summarize_transcript, FALLBACK_BETA)

SAMPLE = {
    "language": "en",
    "title": "Q4 planning",
    "summary": "The team agreed the Q4 roadmap.",
    "key_points": ["Budget is 120k", "Launch moves to November"],
    "decisions": ["Ship v2 on 14 Nov"],
    "action_items": [{"task": "Draft launch email", "owner": "Priya", "due": "Friday"}],
    "open_questions": ["Who owns the pricing page?"],
    "my_followups": ["Send the deck to Sam"],
    "participants": ["Priya", "Sam"],
}


def test_schema_matches_pydantic_model():
    assert set(NOTES_SCHEMA["required"]) == set(MeetingNotes.model_fields)
    notes = MeetingNotes.model_validate(SAMPLE)
    assert notes.action_items[0].owner == "Priya"


def test_render_markdown():
    meta = MeetingMeta("Q4 planning", "Zoom", datetime(2026, 10, 29, 15, 0), 1800)
    md = render_markdown(MeetingNotes.model_validate(SAMPLE), meta)
    assert md.startswith("# Q4 planning")
    assert "| Draft launch email | Priya | Friday |" in md
    assert "## Decisions" in md and "- Ship v2 on 14 Nov" in md
    assert "30 min" in md and "Thursday 29 October 2026" in md


def test_render_markdown_in_finnish():
    meta = MeetingMeta("Viikkopalaveri", "Phone (in person)", datetime(2026, 10, 30, 9, 5), 3720)
    notes = MeetingNotes.model_validate({**SAMPLE, "language": "fi", "decisions": []})
    md = render_markdown(notes, meta)
    assert "*perjantai 30.10.2026 klo 09.05 · Puhelin (kasvokkain) · 1 h 2 min*" in md
    for heading in ("## Yhteenveto", "## Keskeiset asiat", "## Päätökset", "## Tehtävät",
                    "| Tehtävä | Vastuu | Määräaika |", "## Omat jatkotoimet", "## Avoimet kysymykset"):
        assert heading in md
    assert "_Ei mitään._" in md
    old = MeetingNotes.model_validate({k: v for k, v in SAMPLE.items() if k != "language"})
    assert old.language == "en"  # notes.json written before this field still loads


class FakeStream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.response


class FakeClient:
    """Captures the request and returns a canned structured-output response."""

    def __init__(self, response):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self._response = response

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self._response)


def _response(stop_reason="end_turn", text=json.dumps(SAMPLE)):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None,
                           content=[SimpleNamespace(type="text", text=text)])


def test_summarize_transcript_request_shape():
    client = FakeClient(_response())
    meta = MeetingMeta("Q4 planning", "Zoom", datetime(2026, 10, 29, 15, 0), 1800)
    notes = summarize_transcript("[00:00:01] Me: hi", meta, model="claude-opus-5-5",
                                 effort="high", extra_instructions="Always note budgets.",
                                 client=client)
    assert notes.title == "Q4 planning"
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["schema"] is NOTES_SCHEMA
    assert call["output_config"]["effort"] == "high"
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert "Always note budgets." in call["messages"][0]["content"]
    assert "<transcript>" in call["messages"][0]["content"]


def test_summarize_transcript_refusal():
    client = FakeClient(_response(stop_reason="refusal"))
    meta = MeetingMeta("x", "", datetime.now(), 0)
    with pytest.raises(RuntimeError, match="declined"):
        summarize_transcript("hi", meta, client=client)


def test_notes_language_and_ask():
    client = FakeClient(_response())
    meta = MeetingMeta("x", "", datetime.now(), 0)
    summarize_transcript("hi", meta, client=client, notes_language="Finnish")
    assert "Write the notes in Finnish." in client.calls[0]["messages"][0]["content"]

    from meetnotes.summarize import ask

    class AskStream(FakeStream):
        text_stream = iter(["Sam ", "promised the deck."])

    class AskClient(FakeClient):
        def _stream(self, **kwargs):
            self.calls.append(kwargs)
            return AskStream(_response())

    c = AskClient(None)
    out = []
    answer = ask("What did Sam promise?", [("2026-10-29 Planning", "Sam: I'll send the deck")],
                 client=c, write=out.append)
    assert answer == "Sam promised the deck." and out == ["Sam ", "promised the deck."]
    content = c.calls[0]["messages"][0]["content"]
    assert content.index("<meetings>") < content.index("Question: What did Sam promise?")
    assert c.calls[0]["fallbacks"] == "default"

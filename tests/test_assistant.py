"""The meeting assistant: briefings, matching, context, live loop (fake Claude)."""
import json
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from meetnotes import assistant as A
from meetnotes.config import Config
from meetnotes.library import find_related, meetings


def seed(root, name, title, notes="", transcript="", started="2026-11-03T10:00:00"):
    f = root / name
    f.mkdir(parents=True)
    (f / "meta.json").write_text(json.dumps({"title": title, "status": "done", "started_at": started,
                                              "notes_title": title}))
    if notes:
        (f / "notes.md").write_text(notes)
    if transcript:
        (f / "transcript.md").write_text("# Transcript\n\n" + transcript)
    return f


# ---------------------------------------------------------------- briefings

def test_briefing_store_and_fields(tmp_path):
    b = A.new_briefing(tmp_path, title="Kontio palsu", when="14.11.2026 10:00", participants="Jari Kontio, Mira",
                       agenda="1. Hinta\n2) Toimitusaika\n- Tuki", keywords=["Kontio Oy"], goals="Sopimus kasaan")
    assert b.agenda == ["Hinta", "Toimitusaika", "Tuki"]
    assert b.participants == ["Jari Kontio", "Mira"] and b.date() == "2026-11-14"
    assert "palsu" not in [k.lower() for k in b.search_keywords()]        # stopword
    assert "Kontio" in b.search_keywords() and "Kontio Oy" in b.search_keywords()
    again = A.load_briefing(tmp_path, b.id)
    assert again == b
    assert [x.id for x in A.list_briefings(tmp_path)] == [b.id]
    assert not [m for m in meetings(tmp_path)]                             # _briefings is not a meeting
    A.delete_briefing(tmp_path, b.id)
    assert A.list_briefings(tmp_path) == []
    with pytest.raises(KeyError):
        A.load_briefing(tmp_path, "../x")


def test_match_briefing(tmp_path):
    kontio = A.new_briefing(tmp_path, title="Kontio palaveri", when="2026-11-14")
    standup = A.new_briefing(tmp_path, title="Standup", when="2026-11-14")
    other = A.new_briefing(tmp_path, title="Budjetti", when="2026-11-20")
    all_ = [kontio, standup, other]
    assert A.match_briefing(all_, "Kontio Oy | Microsoft Teams", "2026-11-14") is kontio
    assert A.match_briefing(all_, "Zoom Meeting", "2026-11-14") is None     # two today, no keyword
    assert A.match_briefing([other, kontio], "Zoom Meeting", "2026-11-20") is other  # the only one that day
    kontio.used_by = "2026-11-14_1000_x"
    assert A.match_briefing(all_, "Kontio", "2026-11-14") is None           # already used


# ---------------------------------------------------------------- context

def test_related_meetings_and_context(tmp_path):
    seed(tmp_path, "2026-10-20_1000_Kontio", "Kontio: tarjous", notes="# Kontio\nHinta 48 000 €, toimitus tammikuussa.",
         transcript="[00:00:01] Others: Toimitus pitäisi olla tammikuussa.\n", started="2026-10-20T10:00:00")
    seed(tmp_path, "2026-10-25_1000_Muu", "Viikkopalaveri", notes="# Viikko\nBudjetti ja lomat.")
    seed(tmp_path, "2026-10-26_1000_Kontio2", "Asiakas", transcript="[00:00:01] Me: Kontio soitti eilen.\n",
         started="2026-10-26T10:00:00")
    rel = find_related(tmp_path, ["Kontio"], limit=5)
    assert [m.path.name for m in rel] == ["2026-10-26_1000_Kontio2", "2026-10-20_1000_Kontio"]  # newest first
    assert find_related(tmp_path, ["ko"]) == []                             # too short

    b = A.new_briefing(tmp_path, title="Kontio palaveri", agenda=["Hinta", "Toimitus"], materials="Tarjous 48 000 €")
    ctx = A.briefing_context(tmp_path, b)
    assert ctx.startswith("<briefing>") and "1. Hinta\n2. Toimitus" in ctx and "Tarjous 48 000" in ctx
    assert ctx.count("<meeting ") == 2 and 'date="2026-10-20"' in ctx and "toimitus tammikuussa" in ctx
    small = A.briefing_context(tmp_path, b, budget_chars=300)
    assert small.count("<meeting ") <= 1                                     # budget respected
    b.related = ["2026-10-25_1000_Muu"]
    assert "Budjetti ja lomat" in A.briefing_context(tmp_path, b) and "48 000 €, toimitus" not in A.briefing_context(tmp_path, b)
    assert A.briefing_context(tmp_path, None, title="Kontio Oy", generic_past=2).count("<meeting ") == 2
    assert A.briefing_context(tmp_path, None) == ""


# ---------------------------------------------------------------- fake Claude

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
    """Returns canned JSON; records every request so tests can inspect the conversation."""

    def __init__(self, responder):
        self.calls = []
        self.responder = responder
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def with_options(self, **kw):
        self.options = kw
        return self

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        body = self.responder(kwargs)
        if isinstance(body, Exception):
            raise body
        return FakeStream(SimpleNamespace(stop_reason="end_turn", stop_details=None,
                                          content=[SimpleNamespace(type="text", text=body)],
                                          usage=SimpleNamespace(input_tokens=100, cache_read_input_tokens=80, output_tokens=20)))


def test_prepare_and_render(tmp_path):
    b = A.new_briefing(tmp_path, title="Kontio palaveri", agenda=["Hinta"])
    brief = {"language": "fi", "summary": "Tarjous on auki.", "history": ["20.10.: hinta 48 000 €"],
             "open_items": ["Toimitusaika"], "talking_points": ["Vahvista hinta"], "questions": ["Milloin päätös?"], "risks": []}
    client = FakeClient(lambda kw: json.dumps(brief))
    md = A.prepare(tmp_path, b, client=client)
    assert md.startswith("## Tilanne ennen kokousta\n\nTarjous on auki.") and "## Varo\n\n_Ei mitään._" in md
    assert "- 20.10.: hinta 48 000 €" in md and b.prep_status == "done" and b.prep == md
    call = client.calls[0]
    assert call["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": A.PREP_SCHEMA}}
    assert "<briefing>" in call["messages"][0]["content"] and call["fallbacks"] == "default"


def test_chat_keeps_history_and_caches_context(tmp_path):
    b = A.new_briefing(tmp_path, title="Kontio palaveri")
    client = FakeClient(lambda kw: "Hinnaksi sovittiin 48 000 € (Kontio: tarjous, 20.10.).")
    a1 = A.chat(tmp_path, b, "Mitä hinnasta on sovittu?", client=client)
    assert a1.startswith("Hinnaksi")
    A.chat(tmp_path, b, "Entä toimitus?", client=client)
    second = client.calls[1]
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert second["system"][0]["cache_control"] == {"type": "ephemeral"} and "<briefing>" in second["system"][0]["text"]
    assert len(b.chat) == 4 and b.chat[2]["text"] == "Entä toimitus?"


# ---------------------------------------------------------------- live assistant

def live_responder(kw):
    """Canned live updates. The answer echoes the question so tests can check it."""
    last = kw["messages"][-1]["content"]
    nudges = []
    if "tammikuussa" in last:
        nudges.append({"kind": "heads_up", "text": "Toimitusaika oli viimeksi tammikuu, nyt puhutaan maaliskuusta.",
                       "why": "Kontio: tarjous, 20.10."})
    if "päätetään" in last:
        nudges.append({"kind": "decision", "text": "Hinta 48 000 € hyväksyttiin.", "why": "juuri sanottu"})
    answer = ""
    if "<question>" in last:
        answer = "Vastaus: " + last.split("<question>")[1].split("</question>")[0].strip()
    everything = " ".join(m["content"] for m in kw["messages"] if m["role"] == "user")
    status = "done" if "päätetään" in everything else "in_progress"
    return json.dumps({"agenda": [{"item": "Hinta", "status": status}, {"item": "Toimitus", "status": "not_started"}],
                       "nudges": nudges, "answer": answer})


def test_live_conversation_is_append_only_and_cached():
    client = FakeClient(live_responder)
    t = {"now": 1000.0}
    a = A.LiveAssistant("<briefing>agenda</briefing>", "Kontio", interval=45, client=client, clock=lambda: t["now"])
    assert not a.due()
    a.add_lines(["[00:00:05] Others: Toimitus olisi maaliskuussa, ei tammikuussa.", ""])
    assert a.due()                                                 # first call right away
    u = a.update()
    assert [n.kind for n in u.nudges] == ["heads_up"] and a.agenda[0]["status"] == "in_progress"
    assert not a.due()                                             # nothing pending
    a.add_lines(["[00:00:40] Me: Sitten päätetään hinta."])
    assert not a.due()                                             # within the interval
    t["now"] += 45
    assert a.due()
    a.update()
    ans = a.ask("Mitä hinnasta sovittiin viimeksi?")
    assert ans == "Vastaus: Mitä hinnasta sovittiin viimeksi?"

    # One conversation: user/assistant alternating, every request carries the full history
    last = client.calls[-1]
    roles = [m["role"] for m in last["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert last["messages"][0]["content"].startswith("The meeting 'Kontio' has started")
    assert "<transcript_update>" in last["messages"][0]["content"]
    assert "(no new transcript)" in last["messages"][4]["content"] and "<question>" in last["messages"][4]["content"]
    assert json.loads(last["messages"][1]["content"])["agenda"][0]["item"] == "Hinta"
    assert last["cache_control"] == {"type": "ephemeral"} and last["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert last["output_config"]["format"]["schema"] is A.LIVE_SCHEMA and last["output_config"]["effort"] == "low"
    assert client.options == {"timeout": 90.0, "max_retries": 1}

    snap = a.snapshot()
    kinds = [f["kind"] for f in snap["feed"]]
    assert kinds == ["heads_up", "decision", "question", "answer"] and snap["calls"] == 3
    assert snap["agenda"][0]["status"] == "done" and a.usage == {"input": 300, "cached": 240, "output": 60}
    assert "Hinta: covered" in a.agenda_summary() and "Toimitus: NOT covered" in a.agenda_summary()


def test_live_error_keeps_conversation_valid_and_background_loop(tmp_path):
    calls = {"n": 0}

    def flaky(kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return RuntimeError("503 overloaded")
        return live_responder(kw)

    client = FakeClient(flaky)
    a = A.LiveAssistant("", "x", interval=15, client=client, clock=time.monotonic)
    a.add_lines(["[00:00:01] Me: hello"])
    with pytest.raises(RuntimeError):
        a.update()
    assert a.messages == []                                        # the failed turn was rolled back
    a.add_lines(["[00:00:02] Me: again"])
    a.update()
    assert [m["role"] for m in a.messages] == ["user", "assistant"]
    assert "hello" in a.messages[0]["content"] and "again" in a.messages[0]["content"]  # nothing lost

    # background thread: picks up pending lines on its own
    b = A.LiveAssistant("", "y", interval=15, client=FakeClient(live_responder))
    b.last_call = -100
    b.start()
    b.add_lines(["[00:00:03] Others: päätetään hinta"])
    for _ in range(50):
        if b.calls:
            break
        time.sleep(0.1)
    b.stop()
    assert b.calls == 1 and b.feed[0]["kind"] == "decision"
    b.save(tmp_path)
    saved = json.loads((tmp_path / "assistant.json").read_text())
    assert saved["feed"][0]["text"].startswith("Hinta 48 000") and "## Agenda" in (tmp_path / "assistant.md").read_text()


def test_style_rules_and_concurrent_ask():
    client = FakeClient(live_responder)
    a = A.LiveAssistant("", "x", style="active", client=client, clock=time.monotonic)
    a.add_lines(["[00:00:01] Me: hi"])
    a.update()
    assert "Style: active" in client.calls[0]["system"][0]["text"]
    assert A.LiveAssistant("", "x", style="weird").style == "balanced"

    # two threads asking at once never interleave the conversation
    results = []
    th = [threading.Thread(target=lambda q=q: results.append(a.ask(q))) for q in ("eka", "toka")]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert sorted(results) == ["Vastaus: eka", "Vastaus: toka"]
    roles = [m["role"] for m in a.messages]
    assert roles == ["user", "assistant"] * 3

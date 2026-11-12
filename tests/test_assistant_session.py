"""Assistant inside a recording session and the app server."""
import json
import time

import numpy as np

from meetnotes import audio as audio_mod
from meetnotes import assistant as A
from meetnotes import live, session as session_mod
from meetnotes.audio import AudioSource, TARGET_RATE
from meetnotes.config import Config
from meetnotes.transcribe import Segment
from tests.test_assistant import FakeClient, live_responder


class Tone(AudioSource):
    """A sound card that always has one more second of audio ready."""

    def __init__(self, label):
        super().__init__()
        self.name = label

    def start(self):
        pass

    def stop(self):
        pass

    def take(self):
        return (np.ones(TARGET_RATE) * 1000).astype(np.int16)


class Scripted:
    def transcribe(self, samples, offset, speaker=""):
        return [Segment(offset, offset + 1, speaker,
                        "Toimitus olisi maaliskuussa, ei tammikuussa, koska tehtaan linja on silloin vapaana ja kuljetus halvempi.")]


def test_session_matches_briefing_and_feeds_the_assistant(tmp_path, monkeypatch):
    root = tmp_path / "notes"
    b = A.new_briefing(root, title="Kontio palaveri", when=f"{time.strftime('%Y-%m-%d')}", agenda=["Hinta", "Toimitus"])
    client = FakeClient(live_responder)
    monkeypatch.setattr(A, "_client", lambda: client)
    mic, system = Tone("mic"), Tone("system")
    monkeypatch.setattr(audio_mod, "build_sources", lambda m, s: (mic, system, []))
    captured = {}
    monkeypatch.setattr(session_mod, "write_notes_for",
                        lambda folder, text, meta, cfg, context="", template="": captured.update(context=context) or folder / "notes.md")
    cfg = Config(notes_dir=str(root), chunk_seconds=5, assistant_live=True, assistant_interval=15, transcribe_mode="separate")
    s = session_mod.MeetingSession(cfg, "Kontio Oy | Microsoft Teams", "Microsoft Teams", transcriber=Scripted())
    s.assistant_started = None
    s.start()
    try:
        assert s.briefing is not None and s.briefing.id == b.id
        assert A.load_briefing(root, b.id).used_by == s.dir.name
        s.assistant.last_call = -1000  # due immediately
        for _ in range(60):
            if s.assistant.calls:
                break
            time.sleep(0.1)
        snap = s.assistant_snapshot()
        assert snap["calls"] >= 1 and snap["feed"][0]["kind"] == "heads_up" and snap["briefing"] == "Kontio palaveri"
        assert s.ask("Mikä hinta?") == "Vastaus: Mikä hinta?"
    finally:
        s.stop()
    assert (s.dir / "assistant.json").exists() and (s.dir / "assistant.md").exists()
    assert "<briefing>" in captured["context"] and "<agenda_status>" in captured["context"]
    assert "Hinta: covered" in captured["context"] or "Hinta: partly covered" in captured["context"]
    assert json.loads((s.dir / "meta.json").read_text())["briefing_title"] == "Kontio palaveri"


def test_assistant_off_until_asked_and_attach_later(tmp_path, monkeypatch):
    root = tmp_path / "notes"
    client = FakeClient(live_responder)
    monkeypatch.setattr(A, "_client", lambda: client)
    mic, system = Tone("mic"), Tone("system")
    monkeypatch.setattr(audio_mod, "build_sources", lambda m, s: (mic, system, []))
    monkeypatch.setattr(session_mod, "write_notes_for", lambda folder, text, meta, cfg, context="", template="": folder / "notes.md")
    cfg = Config(notes_dir=str(root), chunk_seconds=5, assistant_live=False, transcribe_mode="separate")
    s = session_mod.MeetingSession(cfg, "Zoom Meeting", "Zoom", transcriber=Scripted())
    s.start()
    try:
        for _ in range(120):  # wait for the first chunk (5 s of audio) to be transcribed
            if s.transcript.segments:
                break
            time.sleep(0.1)
        assert s.assistant is None
        assert s.ask("Missä mennään?") == "Vastaus: Missä mennään?"   # starts on demand with the transcript so far
        first = client.calls[0]["messages"][0]["content"]
        assert "<transcript_update>" in first and "maaliskuussa" in first and "<question>" in first
        b = A.new_briefing(root, title="Myöhäinen", agenda=["Yksi"])
        s.start_assistant(b)                                           # re-attach: new conversation, old lines handed over
        assert s.briefing is b and s.assistant.calls == 0
        s.assistant.update()
        assert "maaliskuussa" in client.calls[-1]["messages"][0]["content"]
        assert "<briefing>" in client.calls[-1]["system"][0]["text"]
        s.stop_assistant()
        assert s.assistant is None and (s.dir / "assistant.json").exists()
    finally:
        s.stop()


def test_server_briefing_and_live_endpoints(tmp_path, monkeypatch):
    from tests.test_server import call
    from meetnotes.server import App

    cfg = Config(notes_dir=str(tmp_path / "notes"))
    app = App(cfg, config_path=tmp_path / "c.toml")
    app.start(port=0)
    try:
        # create, list, update
        st, b = call(app, "POST", "/api/briefings", {"title": "Kontio palaveri", "when": "14.11.2026",
                                                     "agenda": "Hinta\nToimitus", "participants": "Jari, Mira"})
        assert st == 200 and b["agenda"] == ["Hinta", "Toimitus"] and b["date"] == "2026-11-14"
        bid = b["id"]
        assert call(app, "GET", "/api/briefings")[1]["briefings"][0]["id"] == bid
        call(app, "POST", "/api/briefings", {"id": bid, "goals": "Sopimus"})
        assert call(app, "GET", f"/api/briefings/{bid}")[1]["goals"] == "Sopimus"

        # prepare (background) and chat, with fakes
        def fake_prepare(root, br, model):
            br.prep, br.prep_status = "## Tilanne\n\nValmis.", "done"
        app.prepare_fn = fake_prepare
        app.chat_fn = lambda root, br, q, model: f"vastaus: {q}"
        assert call(app, "POST", f"/api/briefings/{bid}/prepare", {})[0] == 202
        for _ in range(50):
            d = call(app, "GET", f"/api/briefings/{bid}")[1]
            if d["prep_status"] == "done":
                break
            time.sleep(0.05)
        assert d["prep"].startswith("## Tilanne")
        assert call(app, "POST", f"/api/briefings/{bid}/chat", {"question": "Mitä sovittiin?"})[1]["answer"] == "vastaus: Mitä sovittiin?"
        assert call(app, "POST", f"/api/briefings/{bid}/chat", {"question": " "})[0] == 400

        # live: nothing recording
        assert call(app, "GET", "/api/live")[1] == {"active": False}
        assert call(app, "POST", "/api/live/ask", {"question": "x"})[0] == 409

        # live: a recording session (fake) is current
        class FakeSession:
            title, briefing, assistant = "Kontio Oy", None, None
            alerts, template, event = [], "auto", None
            dir = tmp_path / "notes" / "2026-11-14_1000_Kontio-Oy"

            def my_notes(self):
                return ""

            def assistant_snapshot(self):
                return None if self.assistant is None else {"feed": [{"kind": "answer", "text": "ok"}], "agenda": []}

            def find_briefing(self):
                return None

            def start_assistant(self, briefing=None):
                self.briefing, self.assistant = briefing, object()

            def stop_assistant(self):
                self.assistant = None

            def ask(self, q):
                self.assistant = object()
                return "live: " + q
        fs = FakeSession()
        live.set_current(fs)
        d = call(app, "GET", "/api/live")[1]
        assert d["active"] and d["enabled"] is False and d["briefing"] is None and d["briefings"][0]["id"] == bid
        assert call(app, "POST", "/api/live/ask", {"question": "Hinta?"})[1]["answer"] == "live: Hinta?"
        d = call(app, "POST", "/api/live/attach", {"briefing_id": bid})[1]
        assert d["briefing"]["title"] == "Kontio palaveri" and d["enabled"] is True
        assert call(app, "POST", "/api/live/enable", {"on": False})[1]["enabled"] is False
        assert call(app, "POST", "/api/live/attach", {"briefing_id": "nope"})[0] == 404
        live.clear()
        assert call(app, "POST", f"/api/briefings/{bid}/delete", {})[0] == 200
        assert call(app, "GET", f"/api/briefings/{bid}")[0] == 404

        # settings accept the assistant options
        st, res = call(app, "POST", "/api/settings", {"assistant_live": True, "assistant_style": "active",
                                                      "assistant_interval": 60, "assistant_model": "claude-sonnet-5-5"})
        assert st == 200 and app.cfg.assistant_style == "active" and app.cfg.assistant_model == "claude-sonnet-5-5"
        assert call(app, "POST", "/api/settings", {"assistant_model": "gpt-9"})[0] == 400
    finally:
        live.clear()
        app.stop()

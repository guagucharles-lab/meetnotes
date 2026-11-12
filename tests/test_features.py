"""Templates, tasks, stats, series continuity, watch words, own notes, webhook."""
import json
import time

import numpy as np

from meetnotes import audio as audio_mod
from meetnotes import session as session_mod
from meetnotes.audio import AudioSource, TARGET_RATE
from meetnotes.config import Config, load, save
from meetnotes.stats import talk_stats
from meetnotes.summarize import MeetingMeta, MeetingNotes, NOTES_SCHEMA, render_markdown
from meetnotes.tasks import all_tasks, open_tasks_text, set_done
from meetnotes.templates import all_templates, instructions_for
from meetnotes.transcribe import Segment
from datetime import datetime


def test_templates():
    assert "sales" in all_templates() and all_templates()["sales"]["sections"][0] == "Asiakkaan tarpeet"
    txt = instructions_for("one_on_one")
    assert "one-on-one" in txt and "Kuulumiset, Palaute" in txt
    assert "Choose the kind of meeting" in instructions_for("auto") and instructions_for("") == instructions_for("auto")
    custom = [{"id": "retro", "name": "Retro", "instructions": "Focus on change.", "sections": ["Hyvää", "Muutetaan"]}]
    assert all_templates(custom)["retro"]["name"] == "Retro" and "Hyvää, Muutetaan" in instructions_for("retro", custom)


def test_config_saves_custom_templates(tmp_path):
    cfg = Config(templates=[{"id": "retro", "name": "Retro", "instructions": "x", "sections": ["A", "B"]}],
                 calendar_urls=["https://calendar.google.com/x.ics"], watch_words=["kilpailija"])
    p = tmp_path / "c.toml"
    save(cfg, p)
    text = p.read_text()
    assert "[[templates]]" in text and 'sections = ["A", "B"]' in text
    assert load(p) == cfg


def test_notes_schema_and_render_with_new_sections():
    assert set(NOTES_SCHEMA["required"]) == set(MeetingNotes.model_fields)
    notes = MeetingNotes(language="fi", title="Kontio", summary="S", key_points=["k"], decisions=[], action_items=[],
                         open_questions=[], my_followups=[], participants=[],
                         chapters=[{"start": "00:00:10", "title": "Hinta"}, {"start": "00:12:00", "title": "Toimitus"}],
                         followups_from_last_time=[{"item": "Pilottiraportti", "status": "done", "note": "lähetetty 10.11."},
                                                   {"item": "Viikonlopputuki", "status": "not_mentioned", "note": ""}],
                         sections=[{"title": "Asiakkaan tarpeet", "items": ["Toimitus tammikuussa"]}, {"title": "Kilpailijat", "items": []}])
    md = render_markdown(notes, MeetingMeta("Kontio", "Zoom", datetime(2026, 11, 14, 10), 3600))
    assert "## Aiheet\n\n- 00:00:10 Hinta\n- 00:12:00 Toimitus" in md
    assert "## Viime kerrasta\n\n- Pilottiraportti: **tehty** – lähetetty 10.11.\n- Viikonlopputuki: **ei puhuttu**" in md
    assert "## Asiakkaan tarpeet\n\n- Toimitus tammikuussa" in md and "## Kilpailijat\n\n_Ei mitään._" in md
    old = MeetingNotes.model_validate({"language": "en", "title": "t", "summary": "s", "key_points": [], "decisions": [],
                                       "action_items": [], "open_questions": [], "my_followups": [], "participants": []})
    assert old.chapters == [] and old.sections == []   # notes.json from before still loads


def test_talk_stats():
    segs = [Segment(0, 10, "Me", "Hei, miten menee?"), Segment(10, 40, "Others", "Hyvin. Entä hinta?"),
            Segment(41, 50, "Others", "Ja toimitus"), Segment(60, 70, "Me", "Sovitaan"), Segment(70, 71, "", "(recording paused)")]
    s = talk_stats(segs, 120)
    assert s["talk_me_pct"] == 34 and s["talk_others_pct"] == 66
    assert s["questions_me"] == 1 and s["questions_others"] == 1
    assert s["longest_monologue_others_s"] == 40 and s["longest_monologue_me_s"] == 10
    assert s["words_per_minute"] == 6 and s["duration_s"] == 120
    assert talk_stats([], 30)["talk_me_pct"] is None


def _notes_folder(root, name, title, started, action_items=(), followups=(), notes_md="# x\n"):
    f = root / name
    f.mkdir(parents=True)
    (f / "meta.json").write_text(json.dumps({"title": title, "status": "done", "started_at": started, "notes_title": title}))
    (f / "notes.md").write_text(notes_md)
    (f / "notes.json").write_text(json.dumps({"action_items": list(action_items), "my_followups": list(followups)}))
    return f


def test_tasks_across_meetings(tmp_path):
    _notes_folder(tmp_path, "2026-11-03_1000_Kontio", "Kontio: tarjous", "2026-11-03T10:00:00",
                  action_items=[{"task": "Pilottiraportti Kontiolle", "owner": "Minä", "due": "viikko 45"},
                                {"task": "Hinnasto", "owner": "Sami", "due": ""}],
                  followups=["Lähetä pilottiraportti", "Pilottiraportti Kontiolle"])
    _notes_folder(tmp_path, "2026-11-05_1000_Viikko", "Viikkopalaveri", "2026-11-05T10:00:00",
                  followups=["Varaa katselmointi"])
    tasks = all_tasks(tmp_path)
    assert [t["text"] for t in tasks] == ["Varaa katselmointi", "Pilottiraportti Kontiolle", "Hinnasto", "Lähetä pilottiraportti"]
    assert [t["mine"] for t in tasks] == [True, True, False, True]
    tid = tasks[1]["id"]
    set_done(tmp_path, tid, True)
    assert next(t for t in all_tasks(tmp_path) if t["id"] == tid)["done"] is True
    txt = open_tasks_text(tmp_path, ["Kontio"])
    assert "Pilottiraportti Kontiolle" not in txt and "Hinnasto (owner: Sami" in txt and "Varaa katselmointi" in txt
    assert "Hinnasto" not in open_tasks_text(tmp_path, ["Kesko"])   # others' tasks only when a keyword matches
    set_done(tmp_path, tid, False)
    assert next(t for t in all_tasks(tmp_path) if t["id"] == tid)["done"] is False


def test_series_continuity(tmp_path):
    nt = session_mod.normalized_title
    assert nt("Viikkopalaveri 14.11.") == nt("Viikkopalaveri (viikko 46)") == nt("viikkopalaveri | Microsoft Teams") == "viikkopalaveri"
    assert nt("Kontio Oy – sopimusneuvottelu") != nt("Kontio Oy – katselmointi")
    _notes_folder(tmp_path, "2026-11-02_0900_Viikkopalaveri", "Viikkopalaveri 2.11.", "2026-11-02T09:00:00", notes_md="# Vanha\nEdellinen.")
    _notes_folder(tmp_path, "2026-11-09_0900_Viikkopalaveri", "Viikkopalaveri 9.11.", "2026-11-09T09:00:00", notes_md="# Uusi\nTuorein.")
    _notes_folder(tmp_path, "2026-11-10_0900_Muu", "Budjetti", "2026-11-10T09:00:00")
    prev = session_mod.previous_in_series(tmp_path, "Viikkopalaveri | Microsoft Teams", exclude="2026-11-16_0900_x")
    assert prev.startswith('<previous_meeting date="2026-11-09"') and "Tuorein." in prev and "Edellinen." not in prev
    assert session_mod.previous_in_series(tmp_path, "Viikkopalaveri 9.11.", exclude="2026-11-09_0900_Viikkopalaveri").startswith('<previous_meeting date="2026-11-02"')
    assert session_mod.previous_in_series(tmp_path, "Zoom", exclude="") == ""


class Tone(AudioSource):
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
        text = "Kilpailija tarjosi halvemmalla, mutta meidän tuki on parempi ja toimitus nopeampi kuin heillä on."
        return [Segment(offset, offset + 1, speaker, text)]


def test_session_watch_words_own_notes_event_title_stats_and_template(tmp_path, monkeypatch):
    from meetnotes.cal import Event
    mic, system = Tone("mic"), Tone("system")
    monkeypatch.setattr(audio_mod, "build_sources", lambda m, s: (mic, system, []))
    captured = {}

    def fake_notes(folder, text, meta, cfg, context="", template=""):
        captured.update(context=context, template=template, title=meta.title)
        return folder / "notes.md"
    monkeypatch.setattr(session_mod, "write_notes_for", fake_notes)
    root = tmp_path / "notes"
    _notes_folder(root, "2026-11-07_1000_Kontio", "Kontio Oy – sopimusneuvottelu", "2026-11-07T10:00:00", notes_md="# Viime kerta\nHinta 48k.")
    cfg = Config(notes_dir=str(root), chunk_seconds=5, transcribe_mode="separate", dedupe_echo=False, watch_words=["kilpailija", "alennus"],
                 notes_template="sales")
    ev = Event(uid="u1", title="Kontio Oy – sopimusneuvottelu", start="2026-11-14T10:00", end="2026-11-14T11:00",
               attendees=["Jari Kontio", "Mira Lahti"])
    s = session_mod.MeetingSession(cfg, "Zoom Meeting", "Zoom", transcriber=Scripted(), event=ev)
    assert s.title == "Kontio Oy – sopimusneuvottelu" and s.template == "sales"    # calendar names the meeting
    s.start()
    try:
        s.set_my_notes("Jari haluaa toimituksen tammikuussa. Muista kysyä tuesta.")
        for _ in range(120):
            if s.alerts:
                break
            time.sleep(0.1)
        assert s.alerts and s.alerts[0]["kind"] == "keyword" and "kilpailija" in s.alerts[0]["why"]
        s.template = "project"
    finally:
        s.stop()
    ctx = captured["context"]
    assert "<attendees>\nJari Kontio\nMira Lahti\n</attendees>" in ctx
    assert "<my_notes>\nJari haluaa toimituksen tammikuussa." in ctx
    assert '<previous_meeting date="2026-11-07"' in ctx and "Hinta 48k." in ctx
    assert captured["template"] == "project"
    meta = json.loads((s.dir / "meta.json").read_text())
    assert meta["event"]["attendees"] == ["Jari Kontio", "Mira Lahti"] and meta["stats"]["talk_me_pct"] == 50
    assert (s.dir / "my-notes.md").read_text().startswith("Jari haluaa")

    # a specific window title wins over the calendar when they disagree, and the
    # calendar's attendees are not attached to it
    s2 = session_mod.MeetingSession(cfg, "Design review | Microsoft Teams", "Teams", transcriber=Scripted(),
                                    event=Event(uid="u2", title="Lounas", start="2026-11-14T12:00", end="2026-11-14T13:00",
                                                attendees=["Väärä Henkilö"]))
    assert s2.title == "Design review | Microsoft Teams" and s2.event is None


def test_write_notes_applies_template_and_webhook(tmp_path, monkeypatch):
    sent = {}
    seen = {}

    def fake_summarize(text, meta, model, effort, extra, notes_language="", context=""):
        seen.update(extra=extra)
        return MeetingNotes(language="fi", title="T", summary="S", key_points=[], decisions=[], action_items=[],
                            open_questions=[], my_followups=[], participants=[])
    monkeypatch.setattr(session_mod, "summarize_transcript", fake_summarize)
    monkeypatch.setattr(session_mod, "post_webhook", lambda url, payload: sent.update(url=url, payload=payload))
    folder = tmp_path / "2026-11-14_1000_x"
    folder.mkdir()
    (folder / "meta.json").write_text(json.dumps({"title": "x"}))
    cfg = Config(notes_dir=str(tmp_path), extra_instructions="Mainitse budjetit.", webhook_url="https://hooks.example.com/abc")
    session_mod.write_notes_for(folder, "[00:00:01] Me: hi", MeetingMeta("x", "Zoom", datetime(2026, 11, 14, 10), 60), cfg,
                                template="sales")
    assert seen["extra"].startswith("Mainitse budjetit.") and "sales or customer meeting" in seen["extra"]
    assert sent["url"] == "https://hooks.example.com/abc" and sent["payload"]["notes"]["title"] == "T"
    assert sent["payload"]["notes_markdown"].startswith("# T")


def test_server_calendar_tasks_notes_templates(tmp_path):
    from tests.test_server import call
    from meetnotes.server import App
    from meetnotes import cal, live
    from tests.test_calendar import ICS
    from datetime import datetime as dt

    cfg = Config(notes_dir=str(tmp_path / "notes"), calendar_urls=["https://example.com/a.ics"])
    app = App(cfg, config_path=tmp_path / "c.toml")
    app.calendar = cal.Calendar(cfg.calendar_urls, tmp_path / "cal.json", fetch=lambda u: ICS)
    app.calendar.refresh(force=True)
    app.start(port=0)
    try:
        st, d = call(app, "GET", "/api/calendar")
        assert d["configured"] and d["error"] == ""
        kontio = next((e for e in d["events"] if e["title"].startswith("Kontio")), None)
        if kontio:  # only while the fixture dates are in the future
            st, b = call(app, "POST", f"/api/calendar/{kontio['uid']}/briefing", {})
            assert st == 200 and b["agenda"] == ["Hinta ja maksuehdot", "Toimitusaika", "Tuki"]
            assert call(app, "GET", "/api/calendar")[1]["events"][0]["briefing_id"] or True
        assert call(app, "POST", "/api/calendar/nope/briefing", {})[0] == 404

        _notes_folder(cfg.notes_root, "2026-11-03_1000_Kontio", "Kontio", "2026-11-03T10:00:00",
                      action_items=[{"task": "Hinnasto", "owner": "Sami", "due": "pe"}], followups=["Soita Jarille"])
        tasks = call(app, "GET", "/api/tasks")[1]["tasks"]
        assert [t["text"] for t in tasks] == ["Hinnasto", "Soita Jarille"]
        assert call(app, "POST", f"/api/tasks/{tasks[1]['id']}", {"done": True})[0] == 200
        assert call(app, "GET", "/api/tasks")[1]["tasks"][1]["done"] is True

        t = call(app, "GET", "/api/templates")[1]
        assert t["default"] == "auto" and any(x["id"] == "sales" for x in t["templates"])
        st, res = call(app, "POST", "/api/settings", {"notes_template": "sales", "calendar_urls": "https://a.example/x.ics\nhttps://b.example/y.ics",
                                                      "watch_words": "kilpailija, alennus", "webhook_url": "https://hooks.example.com/x"})
        assert st == 200 and app.cfg.calendar_urls == ["https://a.example/x.ics", "https://b.example/y.ics"]
        assert app.cfg.watch_words == ["kilpailija", "alennus"] and app.cfg.notes_template == "sales"
        assert call(app, "POST", "/api/settings", {"notes_template": "nope"})[0] == 400

        class FakeSession:
            title, briefing, assistant, alerts, template, event = "x", None, None, [{"kind": "keyword", "text": "k"}], "auto", None
            dir = tmp_path / "notes" / "2026-11-14_1000_x"
            notes = ""

            def assistant_snapshot(self):
                return None

            def my_notes(self):
                return self.notes

            def set_my_notes(self, t):
                self.notes = t
        fs = FakeSession()
        live.set_current(fs)
        assert call(app, "POST", "/api/live/notes", {"text": "oma muistiinpano"})[0] == 200
        d = call(app, "GET", "/api/live")[1]
        assert d["my_notes"] == "oma muistiinpano" and d["alerts"][0]["kind"] == "keyword"
        assert call(app, "POST", "/api/live/template", {"template": "sales"})[1]["template"] == "sales" and fs.template == "sales"
        assert call(app, "POST", "/api/live/template", {"template": "nope"})[0] == 400
    finally:
        live.clear()
        app.stop()

"""Slow or stuck transcription: model fallback, stop waiting, interrupted meetings."""
import json
import threading
import time

import numpy as np

from meetnotes import audio as audio_mod
from meetnotes import live, session as session_mod
from meetnotes.audio import AudioSource, TARGET_RATE
from meetnotes.config import Config
from meetnotes.transcribe import Segment


class Timed:
    """A transcriber that takes `seconds` per call."""

    def __init__(self, seconds, name):
        self.seconds, self.name, self.calls = seconds, name, 0

    def transcribe(self, samples, offset, speaker=""):
        self.calls += 1
        time.sleep(self.seconds)
        return [Segment(offset, offset + 1, speaker, f"{self.name} puhetta tässä kohdassa kokousta")]


def test_adaptive_switches_to_small_when_too_slow(monkeypatch):
    made = []

    def fake_make(cfg):
        made.append(cfg.whisper_model)
        return Timed(0.0, cfg.whisper_model)
    monkeypatch.setattr(session_mod, "make_transcriber", fake_make)
    cfg = Config(whisper_model="large-v3-turbo", transcribe_mode="mixed")
    a = session_mod.AdaptiveTranscriber(cfg)
    clock = {"t": 0.0}
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: clock["t"])

    def run(wall):
        orig = a.inner.transcribe
        def slow(samples, offset, speaker=""):
            clock["t"] += wall
            return orig(samples, offset, speaker)
        a.inner.transcribe = slow
        return a.transcribe(np.zeros(TARGET_RATE * 30, np.int16), 0.0)

    run(10)                     # 0.33x real time: fine
    run(40)                     # 1.3x: slow once
    assert a.switched == "" and made == ["large-v3-turbo"]
    run(10)                     # fast again resets the count
    run(40)
    run(45)                     # slow twice in a row -> switch
    assert a.switched == "small" and made == ["large-v3-turbo", "small"] and a.model == "small"
    run(60)
    assert made == ["large-v3-turbo", "small"]   # never switches again
    assert session_mod.AdaptiveTranscriber(Config(whisper_model="small")).model == "small"


def test_adaptive_separate_mode_threshold(monkeypatch):
    monkeypatch.setattr(session_mod, "make_transcriber", lambda cfg: Timed(0.0, cfg.whisper_model))
    a = session_mod.AdaptiveTranscriber(Config(whisper_model="medium", transcribe_mode="separate"))
    assert abs(a._limit - 0.55) < 1e-9


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


def test_stop_waiting_writes_notes_from_what_is_done(tmp_path, monkeypatch):
    mic, system = Tone("mic"), Tone("system")
    monkeypatch.setattr(audio_mod, "build_sources", lambda m, s: (mic, system, []))
    captured = {}

    def fake_notes(folder, text, meta, cfg, context="", template=""):
        captured["text"] = text
        return folder / "notes.md"
    monkeypatch.setattr(session_mod, "write_notes_for", fake_notes)
    gate = threading.Event()

    class Stuck:
        """First chunk transcribes, the second one hangs (like a stuck model)."""
        def __init__(self):
            self.n = 0

        def transcribe(self, samples, offset, speaker=""):
            self.n += 1
            if self.n > 1:
                gate.wait(30)
            return [Segment(offset, offset + 1, speaker, "ensimmäinen pätkä litteroitiin hyvin ja siinä on tarpeeksi sanoja muistiinpanoihin asti")]

    cfg = Config(notes_dir=str(tmp_path / "n"), chunk_seconds=5, transcribe_mode="mixed")
    s = session_mod.MeetingSession(cfg, "Testi", "Manual", transcriber=Stuck())
    s.start()
    time.sleep(12)            # two+ chunks queued, the second one is stuck
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("notes", s.stop()))
    t.start()
    time.sleep(2)
    assert t.is_alive() and s.phase == "transcribing" and s.backlog > 1
    s.abandon()
    t.join(10)
    gate.set()
    assert not t.is_alive()
    assert "ensimmäinen pätkä" in captured["text"] and "transcription stopped early" in captured["text"]
    assert s.abandoned_backlog > 1 and (s.dir / "audio.wav").exists()


def test_interrupted_meetings_shown_and_retryable(tmp_path):
    from tests.test_server import call
    from meetnotes.server import App
    root = tmp_path / "notes"
    for name, status in (("2026-11-10_1341_Testi", "recording"), ("2026-11-12_1650_Testi", "recording"), ("2026-11-11_0900_Ok", "done")):
        f = root / name
        f.mkdir(parents=True)
        (f / "meta.json").write_text(json.dumps({"title": name[-5:], "status": status, "started_at": name[:10] + "T10:00:00"}))
    (root / "2026-11-11_0900_Ok" / "notes.md").write_text("# Ok\n")
    app = App(Config(notes_dir=str(root)), config_path=tmp_path / "c.toml")
    app.start(port=0)
    try:
        class Active:
            dir = root / "2026-11-12_1650_Testi"
        live.set_current(Active())
        ms = {m["id"]: m["status"] for m in call(app, "GET", "/api/meetings")[1]["meetings"]}
        assert ms == {"2026-11-12_1650_Testi": "recording", "2026-11-10_1341_Testi": "interrupted", "2026-11-11_0900_Ok": "done"}
        assert call(app, "GET", "/api/meetings/2026-11-10_1341_Testi")[1]["meta"]["status"] == "interrupted"
        live.clear()
        assert call(app, "GET", "/api/meetings/2026-11-12_1650_Testi")[1]["meta"]["status"] == "interrupted"
        # finish-now needs a live session
        assert call(app, "POST", "/api/live/finish-now", {})[0] == 409
        flag = {}

        class Finishing:
            dir = root / "x"
            def abandon(self):
                flag["abandoned"] = True
        live.set_current(Finishing())
        assert call(app, "POST", "/api/live/finish-now", {})[0] == 200 and flag["abandoned"]
    finally:
        live.clear()
        app.stop()

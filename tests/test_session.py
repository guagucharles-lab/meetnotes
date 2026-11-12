"""End-to-end runs of a MeetingSession with fake audio and a fake transcriber."""
import json
import os
import time

import numpy as np

from meetnotes import audio as audio_mod
from meetnotes import session as session_mod
from meetnotes.audio import AudioSource, TARGET_RATE
from meetnotes.config import Config, load, save
from meetnotes.transcribe import Segment


class ToneSource(AudioSource):
    """Pretends to be a sound card: `seconds` of tone available at start."""

    def __init__(self, label, amplitude, seconds=2):
        super().__init__()
        self.name = label
        self.amplitude = amplitude
        self.seconds = seconds
        self.started = self.stopped = False

    def start(self):
        self.started = True
        self._push((np.ones(TARGET_RATE * self.seconds) * self.amplitude).astype(np.int16))

    def feed(self, seconds):
        self._push((np.ones(int(TARGET_RATE * seconds)) * self.amplitude).astype(np.int16))

    def stop(self):
        self.stopped = True


WORDS = {"Me": "I will send the budget deck tomorrow",
         "Others": "great, then we review pricing on Friday",
         "": "unlabelled speech from the mixed channel"}


class FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, samples, offset, speaker=""):
        self.calls.append((len(samples), offset, speaker))
        return [Segment(offset, offset + 1, speaker, WORDS[speaker])]


def _cfg(tmp_path, **kw):
    kw.setdefault("transcribe_mode", "separate")
    return Config(notes_dir=str(tmp_path / "notes"), chunk_seconds=5, live_notes=False, **kw)


def test_session_records_transcribes_and_writes_files(tmp_path, monkeypatch):
    mic, system = ToneSource("mic", 1000), ToneSource("system", 2000)
    monkeypatch.setattr(audio_mod, "build_sources", lambda m, s: (mic, system, ["note"]))
    fake = FakeTranscriber()

    s = session_mod.MeetingSession(_cfg(tmp_path), "Weekly sync", "Zoom", transcriber=fake)
    s.start()
    assert mic.started and system.started and s.warnings == ["note"]
    time.sleep(0.3)
    s.stop(write_notes=False)
    assert mic.stopped and system.stopped

    assert [c[2] for c in fake.calls] == ["Me", "Others"]
    lines = (s.dir / "transcript.md").read_text().splitlines()
    assert lines[0] == "# Transcript"
    assert any("Me: I will send" in ln for ln in lines) and any("Others: great" in ln for ln in lines)

    left, right, _ = audio_mod.read_wav_channels(s.dir / "audio.wav")
    assert len(left) == len(right) == TARGET_RATE * 2
    assert left[0] == 1000 and right[0] == 2000

    meta = json.loads((s.dir / "meta.json").read_text())
    assert meta["title"] == "Weekly sync" and meta["platform"] == "Zoom" and meta["status"] == "transcribed"
    if os.name == "posix":
        assert (s.dir.stat().st_mode & 0o777) == 0o700  # private folder


def test_long_meeting_is_chunked_with_correct_offsets(tmp_path, monkeypatch):
    mic, system = ToneSource("mic", 1000, seconds=0), ToneSource("system", 2000, seconds=0)
    monkeypatch.setattr(audio_mod, "build_sources", lambda m, s: (mic, system, []))
    fake = FakeTranscriber()
    s = session_mod.MeetingSession(_cfg(tmp_path), "Long", transcriber=fake)
    s._mic, s._sys = mic, system
    # Drive the capture tick by hand: 12 s of audio, chunks of >= 5 s
    for _ in range(12):
        mic.feed(1), system.feed(1)
        s._tick(5 * TARGET_RATE)
    s._tick(5 * TARGET_RATE, final=True)
    jobs = []
    while not s._jobs.empty():
        jobs.append(s._jobs.get())
    assert sum(len(j.mic) for j in jobs) == 12 * TARGET_RATE  # nothing lost or duplicated
    offsets = [j.offset for j in jobs]
    assert offsets[0] == 0 and offsets == sorted(offsets) and len(jobs) >= 2
    for a, b in zip(jobs, jobs[1:]):
        assert abs(a.offset + len(a.mic) / TARGET_RATE - b.offset) < 1e-6  # contiguous


def test_pause_discards_audio_and_marks_transcript(tmp_path, monkeypatch):
    mic, system = ToneSource("mic", 1000, seconds=0), ToneSource("system", 2000, seconds=0)
    paused = {"on": False}
    fake = FakeTranscriber()
    s = session_mod.MeetingSession(_cfg(tmp_path, keep_audio=False), "Private bit",
                                   transcriber=fake, is_paused=lambda: paused["on"])
    s._mic, s._sys = mic, system
    target = 30 * TARGET_RATE
    mic.feed(3), system.feed(3); s._tick(target)          # 3 s recorded
    paused["on"] = True
    mic.feed(4), system.feed(4); s._tick(target)          # 4 s discarded
    assert s.paused
    paused["on"] = False
    mic.feed(2), system.feed(2); s._tick(target)          # 2 s recorded
    s._tick(target, final=True)
    jobs = [s._jobs.get() for _ in range(s._jobs.qsize())]
    assert [round(j.offset) for j in jobs] == [0, 7]       # timeline keeps the gap
    assert [len(j.mic) // TARGET_RATE for j in jobs] == [3, 2]
    text = s.transcript.text()
    assert "(recording paused)" in text and "(recording resumed)" in text


def test_idle_and_empty_session_is_discarded(tmp_path, monkeypatch):
    mic, system = ToneSource("mic", 0), ToneSource("system", 0)
    monkeypatch.setattr(audio_mod, "build_sources", lambda m, s: (mic, system, []))

    class Silent(FakeTranscriber):
        def transcribe(self, samples, offset, speaker=""):
            return []

    s = session_mod.MeetingSession(_cfg(tmp_path), "Empty", transcriber=Silent())
    s.start()
    time.sleep(0.2)
    assert s.idle_for() > 0
    assert s.stop() is None
    assert not s.dir.exists()  # nothing said -> no clutter left behind


def test_echo_is_dropped_from_mic_channel(tmp_path):
    class EchoTranscriber(FakeTranscriber):
        def transcribe(self, samples, offset, speaker=""):
            text = "great then we review pricing on friday" if speaker else ""
            return [Segment(offset, offset + 2, speaker, text)]

    cfg = _cfg(tmp_path)
    segs = session_mod.transcribe_pair(EchoTranscriber(), cfg, np.ones(16000, np.int16),
                                       np.ones(16000, np.int16), 0.0)
    assert [s.speaker for s in segs] == ["Others"]
    cfg.dedupe_echo = False
    segs = session_mod.transcribe_pair(EchoTranscriber(), cfg, np.ones(16000, np.int16),
                                       np.ones(16000, np.int16), 0.0)
    assert [s.speaker for s in segs] == ["Me", "Others"]


def _fake_summarize(captured):
    def fake(text, meta, model, effort, extra, notes_language="", context=""):
        captured.update(text=text, meta=meta, model=model, notes_language=notes_language)
        from meetnotes.summarize import MeetingNotes
        return MeetingNotes(title="T", summary="S", key_points=[], decisions=[], action_items=[],
                            open_questions=[], my_followups=[], participants=[])
    return fake


def test_resummarize_from_folder_and_wav(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(session_mod, "summarize_transcript", _fake_summarize(captured))
    folder = tmp_path / "2026-10-29_1500_x"
    folder.mkdir()
    (folder / "transcript.md").write_text("# Transcript\n\n[00:00:01] Me: hello there everyone\n")
    (folder / "meta.json").write_text(json.dumps({"title": "x", "platform": "Teams",
                                                   "started_at": "2026-10-29T15:00:00",
                                                   "duration_s": 120}))
    cfg = Config(notes_dir=str(tmp_path), chunk_seconds=5, notes_language="Finnish", transcribe_mode="separate")
    out = session_mod.resummarize(folder, cfg)
    assert out == folder / "notes.md" and out.read_text().startswith("# T")
    assert captured["text"] == "[00:00:01] Me: hello there everyone"
    assert captured["meta"].platform == "Teams" and captured["meta"].duration_s == 120
    assert captured["notes_language"] == "Finnish"
    assert json.loads((folder / "meta.json").read_text())["status"] == "done"

    wav_folder = tmp_path / "wav"
    wav_folder.mkdir()
    w = audio_mod.StereoWavWriter(wav_folder / "audio.wav")
    w.write((np.ones(TARGET_RATE * 7) * 500).astype(np.int16), (np.ones(TARGET_RATE * 7) * 900).astype(np.int16))
    w.close()
    fake = FakeTranscriber()
    out = session_mod.resummarize(wav_folder / "audio.wav", cfg, transcriber=fake)
    assert out.exists()
    assert [c[2] for c in fake.calls] == ["Me", "Others", "Me", "Others"]  # 2 chunks x 2 channels
    assert fake.calls[2][1] == 5.0


def test_crashed_wav_is_still_readable(tmp_path):
    path = tmp_path / "audio.wav"
    w = audio_mod.StereoWavWriter(path)
    w.write((np.ones(TARGET_RATE) * 700).astype(np.int16), (np.ones(TARGET_RATE) * 800).astype(np.int16))
    w._wav._file.flush()  # simulate a crash: data on disk, header never finalised
    raw = path.read_bytes()
    path.write_bytes(raw)
    left, right, _ = audio_mod.read_wav_channels(path)
    assert len(left) == TARGET_RATE and left[0] == 700 and right[0] == 800


def test_recover_unfinished(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(session_mod, "summarize_transcript", _fake_summarize(captured))
    cfg = Config(notes_dir=str(tmp_path))
    crashed = tmp_path / "2026-10-29_0900_crashed"
    crashed.mkdir()
    (crashed / "meta.json").write_text(json.dumps({"title": "crashed", "status": "recording",
                                                    "started_at": "2026-10-29T09:00:00"}))
    (crashed / "transcript.md").write_text("# Transcript\n\n" + "[00:00:01] Others: word " * 30)
    done_folder = tmp_path / "2026-10-28_0900_done"
    done_folder.mkdir()
    (done_folder / "meta.json").write_text(json.dumps({"status": "done"}))
    (done_folder / "notes.md").write_text("# old")
    assert session_mod.recover_unfinished(cfg) == [crashed / "notes.md"]
    assert session_mod.recover_unfinished(cfg) == []  # idempotent


def test_config_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(whisper_model="medium", live_notes=True, watch_platforms=["zoom"],
                 extra_instructions='Note "budgets"\nalways', vocabulary=["Priya", "Kubernetes"],
                 ignore_titles=["1:1"])
    save(cfg, path)
    loaded = load(path)
    assert loaded == cfg
    assert load(tmp_path / "missing.toml") == Config()

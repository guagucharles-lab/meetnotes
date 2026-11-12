import json
import time

import numpy as np
import pytest

from meetnotes import audio as audio_mod
from meetnotes import session as session_mod
from meetnotes.audio import AudioSource, TARGET_RATE
from meetnotes.config import Config
from meetnotes.safeword import contains_safe_word, find_safe_word
from meetnotes.transcribe import Segment


@pytest.mark.parametrize("text", [
    "Omerta.", "omertà", "Omertaa nyt", "okei, o merta", "ommerta", "homerta",
    "Tämä on omertaa.", "OMERTA!", "omertan", "Omerta, joo.", "Omertá"])
def test_recognises_how_whisper_writes_it(text):
    assert contains_safe_word(text, "omerta")


@pytest.mark.parametrize("text", [
    "katsoin merta", "syön omenan", "omat asiat", "omena", "ominaisuus", "Mertanen tulee",
    "no merta", "on merta", "omistaja", "ometta", "oma etu", "omaa", "Merta oli myrskyinen",
    "kommentti", "omertilla", "Roberta tulee", "home office", ""])
def test_ignores_everyday_finnish(text):
    assert not contains_safe_word(text, "omerta")


def test_only_your_mic_and_real_speech_count():
    others = Segment(5, 6, "Others", "omerta")
    hallucination = Segment(7, 8, "Me", "Omerta.", no_speech=0.9)
    mine = Segment(9, 10, "Me", "okei, omerta")
    assert find_safe_word([others, hallucination], "omerta") is None
    assert find_safe_word([mine, others, hallucination], "omerta") is mine
    assert find_safe_word([mine], "") is None  # disabled


class Tone(AudioSource):
    def __init__(self, label, amp):
        super().__init__()
        self.name, self.amp = label, amp

    def start(self):
        pass

    def stop(self):
        pass

    def feed(self, seconds):
        self._push((np.ones(int(TARGET_RATE * seconds)) * self.amp).astype(np.int16))


class ScriptedTranscriber:
    """Says the safe word 3 s into the second chunk (meeting time 8 s)."""

    def __init__(self):
        self.calls = []

    def transcribe(self, samples, offset, speaker=""):
        self.calls.append((offset, speaker))
        if speaker == "Me" and offset >= 5:
            return [Segment(offset + 1, offset + 2, "Me", "Then let's decide the budget."),
                    Segment(offset + 3, offset + 4, "Me", "Okei, omerta."),
                    Segment(offset + 4, offset + 5, "Me", "This is the secret part.")]
        if speaker == "Others":
            segs = [Segment(offset + 2, offset + 3, "Others", f"Others talking at {offset:.0f}")]
            if offset >= 5:  # after the safe word
                segs.append(Segment(offset + 4, offset + 5, "Others", "secret reply"))
            return segs
        return [Segment(offset, offset + 1, speaker, f"Me talking at {offset:.0f}")]


def _run(tmp_path, action="stop"):
    cfg = Config(notes_dir=str(tmp_path / "notes"), chunk_seconds=5, safe_word_action=action,
                 transcribe_mode="separate")
    s = session_mod.MeetingSession(cfg, "Board meeting", transcriber=ScriptedTranscriber())
    mic, system = Tone("mic", 1000), Tone("system", 2000)
    s._mic, s._sys = mic, system
    s._wav = audio_mod.StereoWavWriter(s.dir / "audio.wav")
    target = 5 * TARGET_RATE
    for second in range(20):          # 20 s meeting, chunks every ~5 s
        mic.feed(1), system.feed(1)
        s._tick(target)
        while not s._jobs.empty():    # transcribe as the real thread would
            s._transcribe_chunk(s._jobs.get())
    s._tick(target, final=True)
    while not s._jobs.empty():
        s._transcribe_chunk(s._jobs.get())
    return s


def test_safe_word_stops_listening_and_cuts_everything_after(tmp_path, monkeypatch):
    s = _run(tmp_path)
    assert s.safe_word_hit
    cut = s.safe_word_at
    assert 7 <= cut <= 10

    text = s.transcript.text()
    assert "(recording stopped by safe word)" in text
    assert "omerta" not in text.lower()
    assert "secret" not in text                      # nothing said after the word survives
    assert "Then let's decide the budget." in text   # what came before is kept
    assert max(seg.start for seg in s.transcript.segments) == cut

    # Chunks after the word are never even transcribed
    assert all(offset < cut for offset, _ in s.transcriber.calls)

    monkeypatch.setattr(session_mod, "write_notes_for", lambda folder, text, meta, cfg, context="", template="": folder / "notes.md")
    s._threads = []
    s.stop()
    left, right, _ = audio_mod.read_wav_channels(s.dir / "audio.wav")
    assert len(left) == int(cut * TARGET_RATE)       # audio after the word deleted from disk
    meta = json.loads((s.dir / "meta.json").read_text())
    assert meta["safe_word_at"] == cut and meta["duration_s"] == cut


def test_safe_word_discard_deletes_the_meeting(tmp_path):
    s = _run(tmp_path, action="discard")
    s._threads = []
    assert s.stop() is None
    assert not s.dir.exists()


def test_others_saying_it_does_not_stop(tmp_path):
    class OthersSayIt(ScriptedTranscriber):
        def transcribe(self, samples, offset, speaker=""):
            return [Segment(offset, offset + 1, speaker, "omerta" if speaker == "Others" else "hello there")]

    cfg = Config(notes_dir=str(tmp_path / "n"), chunk_seconds=5)
    segs = session_mod.transcribe_pair(OthersSayIt(), cfg, np.ones(16000, np.int16),
                                       np.ones(16000, np.int16), 0.0)
    kept, cut = session_mod.apply_safe_word(segs, cfg, labelled=True)
    assert cut is None and kept == segs


def test_watch_loop_ends_session_on_safe_word(tmp_path, monkeypatch):
    """The CLI runner notices the flag within a second and reports why."""
    from meetnotes import cli

    class FakeSession:
        def __init__(self, *a, **k):
            self.title, self.dir, self.paused = "x", tmp_path, False
            self.started_at = __import__("datetime").datetime.now()
            self.safe_word_hit, self.warnings = False, []
            self.backlog, self.phase = 0, "recording"

        def start(self):
            self.safe_word_hit = True  # as if said right away

        def stop(self):
            return None

    monkeypatch.setattr(session_mod, "MeetingSession", FakeSession)
    t0 = time.monotonic()
    reason = cli._run_session(Config(notes_dir=str(tmp_path)), "x", "Zoom", lambda s: None)
    assert reason == "safe word" and time.monotonic() - t0 < 2

import numpy as np

from meetnotes.dsp import drop_echo, find_cut, gate_mic
from meetnotes.transcribe import Segment

RATE = 16000


def test_find_cut_lands_in_the_pause():
    loud = (np.sin(np.arange(RATE * 12) / 3) * 8000).astype(np.int16)
    loud[RATE * 9: RATE * 9 + RATE // 2] = 0  # half-second pause at 9.0-9.5 s
    cut = find_cut(loud, np.zeros(0, np.int16), RATE)
    assert RATE * 9 <= cut <= RATE * 9 + RATE // 2


def test_find_cut_short_buffer_returns_end():
    x = np.ones(1000, np.int16)
    assert find_cut(x, x, RATE) == 1000


def test_gate_mic_mutes_only_leakage():
    n = RATE  # 1 s, 50 frames
    system = np.zeros(n, np.int16)
    system[: n // 2] = 10000                     # far side talks in the first half
    mic = np.full(n, 2000, np.int16)             # quiet leakage everywhere
    mic[n * 3 // 4:] = 9000                      # you talk in the last quarter
    out = gate_mic(mic, system, RATE)
    assert not out[: n // 2].any()               # leakage removed while others talk
    assert (out[n // 2:] == mic[n // 2:]).all()  # untouched otherwise


def test_drop_echo():
    others = [Segment(10, 14, "Others", "So the launch moves to November, right?")]
    echo = Segment(10.5, 13.5, "Me", "the launch moves to November")
    mine = Segment(14, 16, "Me", "Yes, I will update the plan today.")
    short = Segment(11, 11.5, "Me", "right")
    far_away = Segment(60, 62, "Me", "the launch moves to November")
    kept = drop_echo([echo, mine, short, far_away], others)
    assert kept == [mine, short, far_away]


def test_label_by_energy_mixed_mode():
    from meetnotes.dsp import label_by_energy
    mic = np.zeros(RATE * 6, np.int16)
    system = np.zeros(RATE * 6, np.int16)
    mic[RATE * 0: RATE * 2] = 6000          # you talk 0-2 s
    system[RATE * 2: RATE * 4] = 9000       # they talk 2-4 s
    mic[RATE * 2: RATE * 4] = 1500          # speakers leaking into your mic, quieter
    mic[RATE * 4: RATE * 6] = 7000          # you again, over a quiet system channel
    system[RATE * 4: RATE * 6] = 500
    segs = [Segment(10.0, 12.0, "", "a"), Segment(12.0, 14.0, "", "b"), Segment(14.0, 16.0, "", "c")]
    out = label_by_energy(segs, mic, system, offset=10.0, rate=RATE)
    assert [s.speaker for s in out] == ["Me", "Others", "Me"]
    assert [s.text for s in out] == ["a", "b", "c"]


def test_mixed_mode_transcribes_once():
    from meetnotes.config import Config
    from meetnotes.session import transcribe_pair

    class Counting:
        calls = []

        def transcribe(self, samples, offset, speaker=""):
            self.calls.append(speaker)
            return [Segment(offset, offset + 1, speaker, "hello there everyone")]

    t = Counting()
    mic = np.full(RATE * 2, 8000, np.int16)
    system = np.zeros(RATE * 2, np.int16)
    segs = transcribe_pair(t, Config(), mic, system, 0.0)
    assert t.calls == [""] and [s.speaker for s in segs] == ["Me"]   # one Whisper pass
    t.calls.clear()
    transcribe_pair(t, Config(transcribe_mode="separate"), mic, system, 0.0)
    assert t.calls == ["Me", "Others"]

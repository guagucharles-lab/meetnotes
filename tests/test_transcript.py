import numpy as np

from meetnotes.audio import StereoWavWriter, read_wav_channels, _resample, resolve_device, DeviceInfo
from meetnotes.session import mix, slugify, meeting_dir
from meetnotes.transcribe import Segment, Transcript, fmt_ts, format_line, load_transcript_text
from datetime import datetime


def test_fmt_ts():
    assert fmt_ts(0) == "00:00:00"
    assert fmt_ts(65.7) == "00:01:05"
    assert fmt_ts(3725) == "01:02:05"


def test_transcript_file_and_text(tmp_path):
    path = tmp_path / "transcript.md"
    t = Transcript(path)
    t.add([Segment(30.0, 32.0, "Others", "Let's start."), Segment(5.0, 7.0, "Me", "Hello everyone.")])
    assert t.text().splitlines() == ["[00:00:05] Me: Hello everyone.", "[00:00:30] Others: Let's start."]
    assert t.duration() == 32.0
    assert t.word_count() == 4
    assert load_transcript_text(path).splitlines()[0] == "[00:00:05] Me: Hello everyone."
    assert format_line(Segment(1, 2, "", "unlabelled")) == "[00:00:01] unlabelled"


def test_wav_round_trip(tmp_path):
    path = tmp_path / "a.wav"
    w = StereoWavWriter(path)
    mic = np.arange(100, dtype=np.int16)
    system = np.arange(50, dtype=np.int16) * -1
    w.write(mic, system)
    w.close()
    left, right, rate = read_wav_channels(path)
    assert rate == 16000
    assert len(left) == len(right) == 100
    assert np.array_equal(left, mic)
    assert np.array_equal(right[:50], system) and not right[50:].any()


def test_resample_and_mix():
    x = np.ones(48000, dtype=np.int16) * 1000
    assert len(_resample(x, 48000, 16000)) == 16000
    a = np.array([30000, 30000, 0], dtype=np.int16)
    b = np.array([30000], dtype=np.int16)
    assert mix(a, b).tolist() == [32767, 30000, 0]  # clipped, padded
    assert mix(np.zeros(0, dtype=np.int16), b).tolist() == [30000]


def test_resolve_device():
    devs = [DeviceInfo(0, "MacBook Pro Microphone", 1, 48000, "Core Audio"),
            DeviceInfo(3, "BlackHole 2ch", 2, 48000, "Core Audio")]
    assert resolve_device("blackhole", devs) == 3
    assert resolve_device("3", devs) == 3
    try:
        resolve_device("nope", devs)
    except ValueError as exc:
        assert "meetnotes devices" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_slugify_and_meeting_dir(tmp_path):
    assert slugify("Weekly sync: Q4 / planning!") == "Weekly-sync-Q4-planning"
    assert slugify("") == "meeting"
    when = datetime(2026, 10, 29, 15, 0)
    a = meeting_dir(tmp_path, when, "Weekly sync")
    b = meeting_dir(tmp_path, when, "Weekly sync")
    assert a.name == "2026-10-29_1500_Weekly-sync" and b.name.endswith("-2")

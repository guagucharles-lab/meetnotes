import http.client
import json
import time

import av
import numpy as np
import pytest

from meetnotes import audio as audio_mod
from meetnotes import screen, session as session_mod
from meetnotes.audio import AudioSource, StereoWavWriter, TARGET_RATE
from meetnotes.config import Config
from meetnotes.transcribe import Segment


def fake_grab(color=(0, 200, 0)):
    def grab():
        img = np.zeros((1080, 1920, 4), np.uint8)
        img[..., 0], img[..., 1], img[..., 2], img[..., 3] = color[2], color[1], color[0], 255
        return img, 1920, 1080
    return grab


def durations(path):
    with av.open(str(path)) as c:
        return {s.type: float(s.duration * s.time_base) if s.duration else None for s in c.streams}, \
            {s.type: s.codec_context.name for s in c.streams}


def frame_means(path):
    with av.open(str(path)) as c:
        return [(float(f.time), f.to_ndarray(format="rgb24").mean()) for f in c.decode(video=0)]


def test_output_size():
    assert screen.output_size(1920, 1080, 1280) == (1280, 720)
    assert screen.output_size(2560, 1440, 1280) == (1280, 720)
    assert screen.output_size(1366, 768, 1280) == (1280, 720)
    assert screen.output_size(800, 601, 1280) == (800, 600)  # even sizes for yuv420p


def test_recording_follows_wall_clock(tmp_path):
    r = screen.ScreenRecorder(tmp_path / "v.mp4", fps=5, grab=fake_grab())
    r.start()
    time.sleep(2.0)
    r.stop()
    assert r.error is None
    (dur, codecs) = durations(tmp_path / "v.mp4")
    assert codecs["video"] in ("h264", "mpeg4")
    assert 1.6 <= dur["video"] <= 2.6
    with av.open(str(tmp_path / "v.mp4")) as c:
        assert (c.streams.video[0].width, c.streams.video[0].height) == (1280, 720)


def test_pause_shows_black(tmp_path):
    paused = {"on": False}
    r = screen.ScreenRecorder(tmp_path / "v.mp4", fps=5, grab=fake_grab(), is_paused=lambda: paused["on"])
    r.start()
    time.sleep(1.0)
    paused["on"] = True
    time.sleep(1.0)
    paused["on"] = False
    time.sleep(1.0)
    r.stop()
    means = frame_means(tmp_path / "v.mp4")
    assert means[0][1] > 30                                        # green screen
    assert any(t > 0.9 and m < 5 for t, m in means)                # black while paused
    assert means[-1][1] > 30                                       # back after resume


def test_finalize_adds_audio_and_cuts(tmp_path):
    r = screen.ScreenRecorder(tmp_path / "v.mp4", fps=5, grab=fake_grab())
    r.start()
    time.sleep(3.0)
    r.stop()
    w = StereoWavWriter(tmp_path / "audio.wav")
    w.write((np.sin(np.arange(TARGET_RATE * 3) / 5) * 8000).astype(np.int16),
            (np.sin(np.arange(TARGET_RATE * 3) / 9) * 8000).astype(np.int16))
    w.close()
    screen.finalize(tmp_path / "v.mp4", tmp_path / "full.mp4", tmp_path / "audio.wav")
    dur, codecs = durations(tmp_path / "full.mp4")
    assert codecs["audio"] == "aac" and 2.5 <= dur["audio"] <= 3.2 and 2.5 <= dur["video"] <= 3.4
    screen.finalize(tmp_path / "v.mp4", tmp_path / "cut.mp4", tmp_path / "audio.wav", cut_at=1.5)
    dur, _ = durations(tmp_path / "cut.mp4")
    assert dur["video"] <= 1.6 and 1.3 <= dur["audio"] <= 1.6
    screen.finalize(tmp_path / "v.mp4", tmp_path / "silent.mp4")
    assert set(durations(tmp_path / "silent.mp4")[0]) == {"video"}


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


class SafeWordAt4:
    def transcribe(self, samples, offset, speaker=""):
        if speaker == "Me":
            return [Segment(offset + 1, offset + 2, "Me", "ok let's talk about the plan for next week"),
                    Segment(offset + 4, offset + 4.5, "Me", "omerta")]
        return [Segment(offset + 2, offset + 3, "Others", "sure the plan is ready and the budget is approved now")]


@pytest.mark.parametrize("keep_audio", [True, False])
def test_session_screen_recording_cut_at_safe_word(tmp_path, monkeypatch, keep_audio):
    mic, system = Tone("mic", 1000), Tone("system", 2000)
    monkeypatch.setattr(audio_mod, "build_sources", lambda m, s: (mic, system, []))
    real = screen.ScreenRecorder
    monkeypatch.setattr(session_mod, "ScreenRecorder",
                        lambda *a, **k: real(*a, grab=fake_grab(), **k))
    monkeypatch.setattr(session_mod, "write_notes_for", lambda folder, text, meta, cfg, context="", template="": folder / "notes.md")
    cfg = Config(notes_dir=str(tmp_path / "n"), chunk_seconds=5, record_screen=True, keep_audio=keep_audio,
                 transcribe_mode="separate")
    s = session_mod.MeetingSession(cfg, "Screen test", transcriber=SafeWordAt4())
    s.start()
    for _ in range(7):  # 7 s of meeting; the capture thread ticks every second
        mic.feed(1), system.feed(1)
        time.sleep(1)
    s.stop()
    assert s.safe_word_at == pytest.approx(4.0, abs=1.5)
    dur, codecs = durations(s.dir / "screen.mp4")
    assert dur["video"] <= s.safe_word_at + 0.3 and dur["audio"] <= s.safe_word_at + 0.3
    assert "audio" in codecs  # the video carries the meeting audio either way
    assert not (s.dir / "screen-video.mp4").exists()
    assert (s.dir / "audio.wav").exists() is keep_audio


def test_server_streams_video_with_ranges(tmp_path):
    from meetnotes.server import App
    cfg = Config(notes_dir=str(tmp_path / "n"))
    folder = cfg.notes_root / "2026-10-30_1000_x"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"title": "x"}))
    data = bytes(range(256)) * 400
    (folder / "screen.mp4").write_bytes(data)
    app = App(cfg, config_path=tmp_path / "c.toml")
    app.start(port=0)
    port = app.local.server_address[1]

    def get(path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path, headers={"Host": "127.0.0.1", **(headers or {})})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    try:
        base = "/api/meetings/2026-10-30_1000_x/screen.mp4"
        assert get(base)[0] == 401
        assert get(base + "?t=wrong")[0] == 401
        status, h, body = get(f"{base}?t={app.token}")
        assert status == 200 and body == data and h["Content-Type"] == "video/mp4" and h["Accept-Ranges"] == "bytes"
        status, h, body = get(f"{base}?t={app.token}", {"Range": "bytes=1000-1999"})
        assert status == 206 and body == data[1000:2000] and h["Content-Range"] == f"bytes 1000-1999/{len(data)}"
        status, _, body = get(f"{base}?t={app.token}", {"Range": "bytes=-100"})
        assert status == 206 and body == data[-100:]
        assert get(f"{base}?t={app.token}", {"Range": f"bytes={len(data)}-"})[0] == 416
        assert get(f"/api/meetings/nope/screen.mp4?t={app.token}")[0] == 404
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/meetings/2026-10-30_1000_x",
                  headers={"Host": "127.0.0.1", "X-Meetnotes-Token": app.token})
        assert json.loads(c.getresponse().read())["has_screen"] is True
    finally:
        app.stop()

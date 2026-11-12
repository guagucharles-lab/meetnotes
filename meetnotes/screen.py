"""Screen recording during a meeting (optional, `record_screen = true`).

* Grabs the screen with mss (plain Python over the OS screenshot API: no
  extra program for Smart App Control to block, no yellow capture border,
  nothing the meeting apps or other participants can see).
* Encodes H.264 (or MPEG-4 if H.264 is missing) with PyAV, a few frames per
  second, scaled to at most `max_width` pixels. Meetings are mostly still
  images, so 5 fps keeps files small (roughly 0.2-0.5 GB per hour).
* Frame timestamps follow the wall clock, so the video stays in sync with the
  meeting audio even if the computer is busy and frames are skipped.
* While paused, a black frame is shown instead of the screen.
* At the end, finalize() adds the meeting audio and cuts everything after the
  safe word, producing screen.mp4.
"""
from __future__ import annotations

import logging
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger("meetnotes")

VIDEO_CODECS = (  # first one this PyAV build has wins
    # No B-frames: frames are reordered with them, and with skipped wall-clock
    # slots that made the muxer reject duplicate timestamps at the end.
    ("libx264", {"preset": "veryfast", "crf": "30", "tune": "stillimage", "bf": "0"}),
    ("mpeg4", {"qscale:v": "6"}),
)


def _even(n: int) -> int:
    return max(2, n - n % 2)


def output_size(width: int, height: int, max_width: int) -> tuple[int, int]:
    if width > max_width:
        height = round(height * max_width / width)
        width = max_width
    return _even(width), _even(height)


def open_video(path: Path, fps: int, width: int, height: int):
    import av
    container = av.open(str(path), "w")
    for name, options in VIDEO_CODECS:
        if name not in av.codecs_available:
            continue
        stream = container.add_stream(name, rate=fps)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        stream.time_base = Fraction(1, fps)
        stream.options = dict(options)
        return container, stream
    container.close()
    raise RuntimeError("no video encoder available")


class ScreenRecorder:
    def __init__(self, path: Path, fps: int = 5, max_width: int = 1280, monitor: int = 1,
                 is_paused: Callable[[], bool] = lambda: False,
                 grab: Callable[[], tuple[np.ndarray, int, int]] | None = None) -> None:
        self.path = path
        self.fps = max(1, min(30, int(fps)))
        self.max_width = max_width
        self.monitor = monitor
        self.is_paused = is_paused
        self._grab = grab               # tests inject frames; None = real screen
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.frames = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="screen", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None

    # -- capture loop --------------------------------------------------------

    def _run(self) -> None:
        try:
            if self._grab is not None:
                self._loop(self._grab)
            else:
                import mss
                with mss.mss() as sct:
                    monitors = sct.monitors  # [0] = all screens together, [1] = primary
                    mon = monitors[self.monitor] if 0 <= self.monitor < len(monitors) else monitors[1]

                    def grab():
                        shot = sct.grab(mon)
                        return np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4), \
                            shot.width, shot.height
                    self._loop(grab)
        except Exception as exc:
            self.error = f"screen recording stopped: {exc}"
            log.warning(self.error)

    def _loop(self, grab) -> None:
        import av
        first, w, h = grab()
        out_w, out_h = output_size(w, h, self.max_width)
        container, stream = open_video(self.path, self.fps, out_w, out_h)
        black = av.VideoFrame.from_ndarray(np.zeros((out_h, out_w, 3), np.uint8), format="rgb24")
        t0 = time.monotonic()
        index = 0
        was_paused = False
        image = first
        try:
            while True:
                if self.is_paused():
                    if not was_paused:  # one black frame; it stays on screen until we resume
                        self._encode(container, stream, black, index, out_w, out_h)
                    was_paused = True
                else:
                    if image is None:
                        image, _, _ = grab()
                    frame = av.VideoFrame.from_ndarray(image, format="bgra")
                    self._encode(container, stream, frame, index, out_w, out_h)
                    was_paused = False
                image = None
                if self._stop.is_set():
                    break
                # Next frame at its wall-clock slot; skip slots if we fell behind.
                index = max(index + 1, int((time.monotonic() - t0) * self.fps))
                delay = t0 + index / self.fps - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
            for packet in stream.encode(None):
                container.mux(packet)
        finally:
            container.close()

    def _encode(self, container, stream, frame, index: int, w: int, h: int) -> None:
        frame = frame.reformat(width=w, height=h, format="yuv420p")
        frame.pts = index
        # A fixed 1/fps: the MP4 muxer changes stream.time_base once the header
        # is written, and frames stamped with that would all land at pts 0.
        frame.time_base = Fraction(1, self.fps)
        for packet in stream.encode(frame):
            container.mux(packet)
        self.frames += 1


def finalize(video: Path, out: Path, wav: Path | None = None, cut_at: float | None = None) -> Path:
    """screen-video.mp4 (+ audio.wav) -> screen.mp4, cut at `cut_at` seconds.
    The video is copied as is; the two audio channels (you, the others) are
    mixed to one AAC track."""
    import av
    vin = av.open(str(video))
    out_c = av.open(str(out), "w")
    try:
        v_in = vin.streams.video[0]
        v_out = out_c.add_stream_from_template(v_in)
        a_out = None
        ain = None
        if wav is not None and wav.exists() and wav.stat().st_size > 44:
            ain = av.open(str(wav))
            a_out = out_c.add_stream("aac", rate=44100)
            a_out.layout = "mono"

        for packet in vin.demux(v_in):
            if packet.dts is None:
                continue
            if cut_at is not None and packet.pts is not None and float(packet.pts * v_in.time_base) >= cut_at:
                break
            packet.stream = v_out
            out_c.mux(packet)

        if ain is not None:
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=44100)
            fifo = av.AudioFifo()
            limit = None if cut_at is None else int(cut_at * 44100)
            written = 0

            def drain(final=False):
                nonlocal written
                size = a_out.codec_context.frame_size or 1024
                while fifo.samples >= size or (final and fifo.samples):
                    chunk = fifo.read(min(size, fifo.samples))
                    chunk.pts = written
                    chunk.time_base = Fraction(1, 44100)
                    written += chunk.samples
                    for p in a_out.encode(chunk):
                        out_c.mux(p)

            done = False
            for frame in ain.decode(audio=0):
                frame.pts = None
                for rf in resampler.resample(frame):
                    if limit is not None and written + fifo.samples + rf.samples > limit:
                        keep = limit - written - fifo.samples
                        if keep > 0:
                            arr = rf.to_ndarray()[:, :keep]
                            part = av.AudioFrame.from_ndarray(np.ascontiguousarray(arr), format="fltp", layout="mono")
                            part.sample_rate = 44100
                            fifo.write(part)
                        done = True
                        break
                    fifo.write(rf)
                drain()
                if done:
                    break
            drain(final=True)
            for p in a_out.encode(None):
                out_c.mux(p)
            ain.close()
    finally:
        out_c.close()
        vin.close()
    return out


def wav_to_m4a(wav: Path, out: Path, bitrate: int = 64000) -> Path:
    """The meeting audio as one small file to listen to or share: both
    channels (you and the others) mixed to mono AAC. About 30 MB per hour,
    instead of ~230 MB for the WAV."""
    import av
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    ain = av.open(str(wav))
    out_c = av.open(str(tmp), "w", format="mp4")
    try:
        a_out = out_c.add_stream("aac", rate=44100)
        a_out.layout = "mono"
        a_out.bit_rate = bitrate
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=44100)
        fifo = av.AudioFifo()
        written = 0

        def drain(final=False):
            nonlocal written
            size = a_out.codec_context.frame_size or 1024
            while fifo.samples >= size or (final and fifo.samples):
                chunk = fifo.read(min(size, fifo.samples))
                chunk.pts = written
                chunk.time_base = Fraction(1, 44100)
                written += chunk.samples
                for p in a_out.encode(chunk):
                    out_c.mux(p)

        for frame in ain.decode(audio=0):
            frame.pts = None
            for rf in resampler.resample(frame):
                fifo.write(rf)
            drain()
        for rf in resampler.resample(None):
            fifo.write(rf)
        drain(final=True)
        for p in a_out.encode(None):
            out_c.mux(p)
    finally:
        out_c.close()
        ain.close()
    tmp.replace(out)
    return out

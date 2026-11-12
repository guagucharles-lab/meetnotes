"""Recordings made with the phone's own microphone (in-person meetings).

The phone streams small pieces of a MediaRecorder recording (WebM/Opus on
Android, MP4/AAC on iPhone) to the computer while it records, so nothing is
lost if the phone locks or the page closes. When the phone says it is done,
the computer decodes the file, transcribes it and writes the notes exactly
like a desktop meeting. Everyone in the room shares one mic, so lines are not
labelled Me/Others, and anyone in the room saying the safe word stops it.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

from .audio import TARGET_RATE, StereoWavWriter
from .config import Config, write_json
from .transcribe import Transcript, fmt_ts

log = logging.getLogger("meetnotes")

MAX_CHUNK = 25 * 1024 * 1024          # one upload
MAX_TOTAL = 1024 * 1024 * 1024        # one recording (~ many hours of Opus)
EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4",
              "audio/aac": "aac", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-m4a": "m4a"}


def _ext(mime: str) -> str:
    return EXTENSIONS.get((mime or "").split(";")[0].strip().lower(), "webm")


def decode_audio(path: Path) -> np.ndarray:
    """Any container/codec the phone produced -> 16 kHz mono int16, via PyAV
    (FFmpeg), which faster-whisper already installs. A recording whose end was
    cut off (phone lost Wi-Fi mid-upload) is decoded up to the damage."""
    import av

    out: list[np.ndarray] = []
    resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)

    def take(frames):
        for f in frames or []:
            out.append(f.to_ndarray().reshape(-1).astype(np.int16))

    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("no audio in the recording")
        try:
            for frame in container.decode(stream):
                frame.pts = None
                take(resampler.resample(frame))
        except av.error.FFmpegError as exc:  # truncated tail
            if not out:
                raise
            log.warning("recording ends early (%s); keeping what decoded", exc)
    take(resampler.resample(None))  # flush
    return np.concatenate(out) if out else np.zeros(0, np.int16)


class PhoneRecorder:
    def __init__(self, cfg: Config, transcriber=None, notes_fn=None) -> None:
        self.cfg = cfg
        self._transcriber = transcriber
        self._notes_fn = notes_fn
        self._lock = threading.Lock()
        self.sessions: dict[str, dict] = {}

    # -- recording -----------------------------------------------------------

    def start(self, title: str, mime: str) -> str:
        from .session import meeting_dir
        started = datetime.now()
        folder = meeting_dir(self.cfg.notes_root, started, title or "Phone meeting")
        path = folder / f"phone.{_ext(mime)}"
        path.write_bytes(b"")
        sid = folder.name
        meta = {"title": title or "Phone meeting", "platform": "Phone",
                "started_at": started.isoformat(timespec="seconds"), "status": "recording",
                "mime": mime}
        write_json(folder / "meta.json", meta)
        with self._lock:
            self.sessions[sid] = {"dir": folder, "file": path, "status": "recording",
                                  "bytes": 0, "started": started, "error": ""}
        log.info("Phone recording started: %s", sid)
        return sid

    def chunk(self, sid: str, data: bytes) -> int:
        with self._lock:
            s = self.sessions.get(sid)
            if s is None or s["status"] != "recording":
                raise KeyError(sid)
            if len(data) > MAX_CHUNK or s["bytes"] + len(data) > MAX_TOTAL:
                raise ValueError("recording too large")
            with open(s["file"], "ab") as fh:
                fh.write(data)
            s["bytes"] += len(data)
            return s["bytes"]

    def finish(self, sid: str, background: bool = True) -> None:
        with self._lock:
            s = self.sessions.get(sid)
            if s is None or s["status"] != "recording":
                raise KeyError(sid)
            s["status"] = "processing"
        if background:
            threading.Thread(target=self._process, args=(sid,), daemon=True,
                             name=f"phone-{sid}").start()
        else:
            self._process(sid)

    def status(self, sid: str) -> dict:
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                raise KeyError(sid)
            return {"id": sid, "status": s["status"], "bytes": s["bytes"], "error": s["error"]}

    # -- processing ----------------------------------------------------------

    def _set(self, sid: str, **kw) -> None:
        with self._lock:
            self.sessions[sid].update(kw)

    def _process(self, sid: str) -> None:
        s = self.sessions[sid]
        folder: Path = s["dir"]
        try:
            status = process_phone_file(folder, s["file"], self.cfg, self._transcriber, self._notes_fn)
            self._set(sid, status=status)
            log.info("Phone recording %s: %s", sid, "notes written" if status == "done" else "too little speech, discarded")
        except Exception as exc:
            log.warning("Phone recording %s failed: %s", sid, exc)
            self._set(sid, status="error", error=str(exc))

    def recover(self) -> None:
        """Process phone uploads left behind by a restart (no 'finish' came)."""
        root = self.cfg.notes_root
        if not root.exists():
            return
        for folder in root.iterdir():
            files = list(folder.glob("phone.*")) if folder.is_dir() else []
            if not files or not files[0].stat().st_size:
                continue
            try:
                meta = json.loads((folder / "meta.json").read_text())
            except Exception:
                continue
            if meta.get("status") in ("recording", "processing"):
                log.info("Finishing interrupted phone recording %s", folder.name)
                try:
                    process_phone_file(folder, files[0], self.cfg, self._transcriber, self._notes_fn)
                except Exception as exc:
                    log.warning("could not finish %s: %s", folder.name, exc)


def process_phone_file(folder: Path, path: Path, cfg: Config, transcriber=None, notes_fn=None) -> str:
    """Decode -> (wav) -> transcript -> notes. Returns done | empty."""
    from .session import apply_safe_word, make_transcriber, write_notes_for
    from .summarize import MeetingMeta

    meta_path = folder / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["status"] = "processing"
    write_json(meta_path, meta)

    audio = decode_audio(path)
    duration = len(audio) / TARGET_RATE
    from .session import AdaptiveTranscriber
    transcriber = transcriber or AdaptiveTranscriber(cfg)
    transcript_path = folder / "transcript.md"
    transcript_path.unlink(missing_ok=True)
    transcript = Transcript(transcript_path)

    step = TARGET_RATE * max(5, cfg.chunk_seconds)
    cut_at = None
    for start in range(0, len(audio), step):
        segs = transcriber.transcribe(audio[start:start + step], start / TARGET_RATE, "")
        segs, cut = apply_safe_word(segs, cfg, labelled=False)
        transcript.add(segs)
        if cut is not None:
            cut_at = cut
            log.info("Safe word at %s in phone recording", fmt_ts(cut))
            break

    if cut_at is not None:
        if cfg.safe_word_action == "discard":
            shutil.rmtree(folder, ignore_errors=True)
            return "empty"
        audio = audio[: int(cut_at * TARGET_RATE)]
        duration = cut_at
    if cfg.keep_audio:
        wav = StereoWavWriter(folder / "audio.wav")
        wav.write(audio, np.zeros(0, np.int16))
        wav.close()
    path.unlink(missing_ok=True)

    meta.update(status="transcribed", duration_s=duration)
    write_json(meta_path, meta)
    if transcript.word_count() < 20:
        if cfg.keep_empty:
            meta["status"] = "too_short"
            write_json(meta_path, meta)
        else:
            shutil.rmtree(folder, ignore_errors=True)
        return "empty"

    started = datetime.fromisoformat(meta["started_at"])
    m = MeetingMeta(meta["title"], "Phone (in person)", started, duration)
    (notes_fn or write_notes_for)(folder, transcript.text(), m, cfg)
    return "done"

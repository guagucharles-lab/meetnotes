"""End-to-end test support. Active only when MEETNOTES_E2E_TRANSCRIBER points
to a script file (one line of text per spoken stretch); never in normal use.

The script stands in for Whisper, which needs a model download. Everything
around it is real: audio capture, chunking, speaker labels from channel
loudness, safe word, live assistant, notes.
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from .transcribe import Segment

RATE = 16000


class ScriptTranscriber:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.used = 0
        self._seen = None
        self._lock = threading.Lock()

    def load(self) -> None:
        pass

    def transcribe(self, samples, offset: float, speaker: str = "") -> list[Segment]:
        a = np.abs(samples.astype(np.float32))
        if len(a) < RATE // 2:
            return []
        frame = RATE // 10
        n = len(a) // frame
        if n == 0:
            return []
        loud = a[: n * frame].reshape(n, frame).mean(axis=1) > 300
        idx = np.flatnonzero(loud)
        if len(idx) == 0:
            return []
        with self._lock:
            raw = self.path.read_text(encoding="utf-8")
            if raw != self._seen:  # the test rewrote the script: start from its first line
                self._seen, self.used = raw, 0
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            if self.used >= len(lines):
                return []
            text = lines[self.used]
            self.used += 1
        start = offset + idx[0] * frame / RATE
        end = offset + (idx[-1] + 1) * frame / RATE
        return [Segment(start, end, speaker, text)]


class FakeWhisper:
    """Stands in for Whisper inside the speech worker (MEETNOTES_FAKE_WHISPER,
    tests only) and misbehaves on purpose, the way a broken setup does:

        {"crash_on_device": "cuda"}   native crash when run on the GPU
        {"crash_load_model": "x"}     crash while loading model x
        {"hang_model": "x"}           never answers with model x
        {"error_model": "x"}          Python error with model x
    """

    def __init__(self, spec: str, model_size: str, device: str, compute_type: str,
                 language: str = "", vocabulary=None) -> None:
        import json
        import os
        self.spec = json.loads(spec)
        self.model_size = model_size
        self.device = "cuda" if device == "auto" else device
        self.language = language
        self._os = os
        log = self.spec.get("log")
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{model_size}|{self.device}\n")

    def _crash(self) -> None:
        import sys
        print("Could not load library cudnn_ops64_9.dll. Error code 126", file=sys.stderr, flush=True)
        self._os.abort()  # like a native library failing: no Python exception

    def load(self) -> None:
        import time
        if self.spec.get("crash_load_model") == self.model_size:
            self._crash()
        if self.spec.get("hang_model") == self.model_size:
            time.sleep(3600)
        if self.spec.get("error_model") == self.model_size:
            raise RuntimeError(f"cannot load {self.model_size}")
        self._model = type("M", (), {"model": type("C", (), {"device": self.device})()})()

    def selftest(self) -> None:
        if self.spec.get("crash_on_device") == self.device:
            self._crash()

    def transcribe(self, samples, offset: float, speaker: str = "") -> list[Segment]:
        self.selftest()
        if self.spec.get("hang_transcribe_model") == self.model_size:
            import time
            time.sleep(3600)
        secs = len(samples) / RATE
        return [Segment(offset, offset + secs, speaker, f"[{self.model_size}/{self.device}] puhetta", 0.1)]

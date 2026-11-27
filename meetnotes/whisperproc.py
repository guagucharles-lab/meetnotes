"""Runs Whisper in a separate worker process.

Speech recognition is native code (CTranslate2, ONNX Runtime). When it fails
it can take the whole process down without a Python error. The classic
Windows case is a laptop with an NVIDIA GPU: "auto" picks CUDA, the CUDA
libraries aren't installed, and the process just exits. Before this module
that killed the background service mid-meeting, so meetings ended up as
"interrupted" or stuck on "writing notes".

The service now talks to a worker process over a local socket. If the
worker crashes, hangs or errors, the service records why, falls back
(GPU -> CPU, then a smaller model), retries the same audio, and remembers
the setup that works so the next meeting starts with it.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import pickle
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .audio import TARGET_RATE
from .transcribe import Segment

log = logging.getLogger("meetnotes")

IDLE_EXIT_S = 15 * 60       # the worker frees its memory after this long unused
START_TIMEOUT_S = 120       # Python start + import of the worker
TRANSCRIBE_TIMEOUT_MIN_S = 180  # one chunk must finish within max(this, 12x its length)
LOAD_STALL_S = 240          # loading/downloading with no download progress for this long = stuck
SMALL_MODELS = {"tiny", "tiny.en", "base", "base.en", "small", "small.en"}
MODEL_FALLBACK_TTL_S = 3 * 24 * 3600  # a smaller model is retried after this; a GPU->CPU switch is kept

# What the app shows about speech recognition (read by the server).
STATE: dict = {"phase": "idle"}
_STATE_LOCK = threading.Lock()


def set_state(**kw) -> None:
    with _STATE_LOCK:
        STATE.update(kw)
        STATE["updated"] = time.time()


def state() -> dict:
    with _STATE_LOCK:
        return dict(STATE)


HELLO = b"meetnotes-worker:"


class WorkerFailed(RuntimeError):
    pass


# -- framing ------------------------------------------------------------------

def _send(sock: socket.socket, obj) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    sock.sendall(struct.pack(">Q", len(data)) + data)


def _recv_exact(sock: socket.socket, n: int, alive=None) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(min(n - len(buf), 1 << 20))
        except socket.timeout:
            if alive is None:
                raise
            alive()  # raises when the worker died, hung or stalled
            continue
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return bytes(buf)


def _recv(sock: socket.socket, alive=None):
    (n,) = struct.unpack(">Q", _recv_exact(sock, 8, alive))
    return pickle.loads(_recv_exact(sock, n, alive))


# -- remembered setup ---------------------------------------------------------

def _memory_path() -> Path:
    from .config import state_dir
    return state_dir() / "speech.json"


def _memory() -> dict:
    try:
        return json.loads(_memory_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember(key: str, value: dict | None) -> None:
    from .config import write_json
    mem = _memory()
    if value is None:
        mem.pop(key, None)
    else:
        mem[key] = value
    try:
        write_json(_memory_path(), mem)
    except OSError:
        pass


def forget_fallbacks() -> None:
    try:
        _memory_path().unlink()
    except OSError:
        pass


def _key(model: str, device: str, compute: str) -> str:
    return f"{model}|{device}|{compute}"


def next_fallback(setup: tuple[str, str, str]) -> tuple[str, str, str] | None:
    model, device, compute = setup
    if device != "cpu" or compute not in ("auto", "int8"):
        return (model, "cpu", "int8")
    if model not in SMALL_MODELS:
        return ("small", "cpu", "int8")
    if model.startswith("small"):
        return ("base", "cpu", "int8")
    return None


def _cache_bytes() -> int:
    """Size of the downloaded Whisper models, to show download progress."""
    roots = []
    if os.environ.get("HF_HUB_CACHE"):
        roots.append(Path(os.environ["HF_HUB_CACHE"]))
    home = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    roots.append(home / "hub")
    total = 0
    for root in roots:
        try:
            for d in root.iterdir():
                if "whisper" in d.name.lower():
                    for f in d.rglob("*"):
                        try:
                            if f.is_file() and not f.is_symlink():
                                total += f.stat().st_size
                        except OSError:
                            pass
        except OSError:
            pass
    return total


def worker_log_path() -> Path:
    from .config import state_dir
    return state_dir() / "speech-worker.log"


def _log_tail(n: int = 15) -> str:
    try:
        lines = worker_log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(line for line in lines[-n:] if line.strip())


def _exit_reason(code: int | None) -> str:
    if code is None:
        return "no response"
    if code < 0 or code > 255:  # Windows reports crashes as large NTSTATUS codes
        return f"crashed (exit code {code if code < 0 else hex(code & 0xFFFFFFFF)})"
    return f"exited (code {code})"


# -- the service side -----------------------------------------------------------

class IsolatedTranscriber:
    """Same interface as transcribe.Transcriber, but Whisper runs in a worker
    process that may crash without taking the service with it."""

    def __init__(self, model_size: str = "small", device: str = "auto", compute_type: str = "auto",
                 language: str = "", vocabulary: list[str] | None = None) -> None:
        self.requested = (model_size, device or "auto", compute_type or "auto")
        self.language = language
        self.vocabulary = list(vocabulary or [])
        self.setup = self._remembered_setup()
        self.model_size = self.setup[0]
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self.device_used = ""
        self._fallback_reason = ""

    # setup memory
    def _remembered_setup(self) -> tuple[str, str, str]:
        m = _memory().get(_key(*self.requested))
        if m:
            fresh = time.time() - float(m.get("at", 0)) < MODEL_FALLBACK_TTL_S
            if m.get("model") == self.requested[0] or fresh:
                log.info("Speech recognition: using %s on %s (remembered: %s).", m["model"], m["device"], m.get("reason", ""))
                return (m["model"], m["device"], m["compute"])
        return self.requested

    def _fall_back(self, reason: str) -> None:
        nxt = next_fallback(self.setup)
        if nxt is None:
            set_state(phase="failed", error=reason)
            raise WorkerFailed(f"Speech recognition does not work on this computer: {reason}")
        log.warning("Speech recognition with %s on %s failed (%s); trying %s on %s.",
                    self.setup[0], self.setup[1], reason, nxt[0], nxt[1])
        self.setup = nxt
        self.model_size = nxt[0]
        self._fallback_reason = reason

    # worker lifecycle
    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._sock is not None

    def close(self) -> None:
        with self._lock:
            self._kill()

    def _kill(self) -> None:
        if self._sock is not None:
            try:
                _send(self._sock, ("quit",))
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self._proc = None

    def _died(self) -> WorkerFailed:
        code = self._proc.poll() if self._proc is not None else None
        tail = _log_tail()
        reason = _exit_reason(code)
        if tail:
            log.warning("Speech worker %s. Its last output:\n%s", reason, tail)
        self._kill()
        last = tail.splitlines()[-1] if tail else ""
        return WorkerFailed(reason + (f": {last[:300]}" if last else ""))

    def _spawn(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        token = secrets.token_hex(16)
        env = dict(os.environ)
        # The plain HTTPS download resumes reliably; the newer Xet transfer
        # has been seen to hang on some Windows machines.
        env.setdefault("HF_HUB_DISABLE_XET", "1")
        env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        env["MEETNOTES_WORKER_LOG"] = str(worker_log_path())
        try:  # keep the log small: start over when it passes 2 MB
            if worker_log_path().stat().st_size > 2_000_000:
                worker_log_path().unlink()
        except OSError:
            pass
        logf = open(worker_log_path(), "ab")
        logf.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start {self.setup} ---\n".encode())
        logf.flush()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        cmd = [sys.executable, "-m", "meetnotes.whisperproc", str(srv.getsockname()[1]), token]
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                                          env=env, creationflags=flags)
        finally:
            logf.close()
        srv.settimeout(1.0)
        deadline = time.monotonic() + START_TIMEOUT_S
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    if self._proc.poll() is not None:
                        raise self._died() from None
                    if time.monotonic() > deadline:
                        self._kill()
                        raise WorkerFailed("worker did not start") from None
                    continue
                conn.settimeout(10)
                # Authenticate with raw bytes before anything is unpickled:
                # another program on this computer could connect to the port
                # too, and unpickling its data would run its code.
                try:
                    hello = _recv_exact(conn, len(HELLO) + len(token))
                except (OSError, EOFError):
                    conn.close()
                    continue
                if not hmac.compare_digest(hello, HELLO + token.encode()):
                    conn.close()
                    continue
                conn.settimeout(1.0)
                self._sock = conn
                return
        finally:
            srv.close()

    def _start_and_load(self) -> None:
        model, device, compute = self.setup
        set_state(phase="starting", model=model, device=device, error="")
        self._spawn()
        _send(self._sock, ("load", {"model_size": model, "device": device, "compute_type": compute,
                                    "language": self.language, "vocabulary": self.vocabulary}))
        t0 = time.monotonic()
        start_bytes = last_bytes = _cache_bytes()
        last_growth = t0
        set_state(phase="loading", downloaded_mb=0)

        def alive():
            nonlocal last_bytes, last_growth
            if self._proc.poll() is not None:
                raise self._died()
            now = time.monotonic()
            b = _cache_bytes()
            if b > last_bytes:
                last_bytes, last_growth = b, now
                set_state(phase="downloading", downloaded_mb=round((b - start_bytes) / 1e6))
            if now - t0 > LOAD_STALL_S and now - last_growth > LOAD_STALL_S * 0.75:
                self._kill()
                raise WorkerFailed(f"loading the speech model got stuck ({int(now - t0)} s)")

        try:
            reply = _recv(self._sock, alive)
        except (OSError, EOFError):
            raise self._died() from None
        if reply[0] != "ok":
            self._kill()
            raise WorkerFailed(str(reply[1]).strip().splitlines()[-1][:300] if reply[1] else "load failed")
        self.device_used = reply[1].get("device", device)
        log.info("Speech model '%s' ready on %s in %.0f s (worker process %d).",
                 model, self.device_used, time.monotonic() - t0, self._proc.pid)
        set_state(phase="ready", model=model, device=self.device_used, downloaded_mb=0)

    def _request(self, msg, timeout: float):
        """Send one request to a loaded worker, starting it (and falling
        back to a safer setup) as needed. Retries until something works."""
        tries = 0
        while True:
            tries += 1
            try:
                if not self._alive():
                    if self._proc is not None:
                        # exited on its own: idle timeout (code 0) or a crash
                        code = self._proc.poll()
                        if code not in (0, None):
                            raise self._died()
                        self._kill()
                    self._start_and_load()
                deadline = time.monotonic() + timeout

                def alive():
                    if self._proc.poll() is not None:
                        raise self._died()
                    if time.monotonic() > deadline:
                        self._kill()
                        raise WorkerFailed(f"no answer in {int(timeout)} s")
                try:
                    _send(self._sock, msg)
                    reply = _recv(self._sock, alive)
                except (OSError, EOFError):
                    if self._proc is not None and self._proc.poll() == 0 and tries < 3:
                        self._kill()  # it was just exiting because idle; start again
                        continue
                    raise self._died() from None
                if reply[0] == "error":
                    raise WorkerFailed(str(reply[1]).strip().splitlines()[-1][:300])
                self._settled()
                return reply[1]
            except WorkerFailed as exc:
                self._kill()
                self._fall_back(str(exc))  # raises when nothing is left to try

    def _settled(self) -> None:
        reason = self._fallback_reason
        if reason and self.setup != self.requested:
            _remember(_key(*self.requested), {"model": self.setup[0], "device": self.setup[1],
                                              "compute": self.setup[2], "reason": reason, "at": time.time()})
            set_state(fallback=f"{self.setup[0]} / {self.setup[1]}", fallback_reason=reason)
            self._fallback_reason = ""

    # Transcriber interface
    def load(self) -> None:
        with self._lock:
            if not self._alive():
                self._request(("ping",), 30)

    def self_test(self) -> dict:
        """Load the model and run it once on a second of noise, without the
        voice filter, so problems (e.g. a broken GPU setup) show up now and
        not in the middle of a meeting."""
        with self._lock:
            t0 = time.monotonic()
            self._request(("selftest",), 300)
            return {"model": self.setup[0], "device": self.device_used or self.setup[1],
                    "seconds": round(time.monotonic() - t0, 1)}

    def transcribe(self, samples: np.ndarray, offset: float, speaker: str = "") -> list[Segment]:
        if len(samples) < TARGET_RATE // 2:
            return []
        if float(np.abs(samples).mean()) < 3.0:
            return []
        audio_s = len(samples) / TARGET_RATE
        pcm = np.asarray(samples, dtype=np.int16).tobytes()
        with self._lock:
            set_state(phase="transcribing")
            try:
                rows = self._request(("transcribe", pcm, float(offset), speaker), max(TRANSCRIBE_TIMEOUT_MIN_S, 12 * audio_s))
            finally:
                if state().get("phase") == "transcribing":
                    set_state(phase="ready")
        return [Segment(*r) for r in rows]


# -- the worker side ------------------------------------------------------------

def _worker(port: int, token: str) -> int:
    import faulthandler
    # Under pythonw sys.stderr may be missing, so write to the log by name.
    # A native crash then still leaves a Python stack trace behind.
    out = open(os.environ.get("MEETNOTES_WORKER_LOG") or os.devnull, "a", encoding="utf-8", buffering=1)
    sys.stderr = out
    faulthandler.enable(file=out)
    logging.basicConfig(level=logging.INFO, stream=out, format="%(asctime)s %(message)s")
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(HELLO + token.encode())
    model = None
    fake = os.environ.get("MEETNOTES_FAKE_WHISPER")  # tests only
    idle = float(os.environ.get("MEETNOTES_WORKER_IDLE") or IDLE_EXIT_S)
    while True:
        sock.settimeout(idle)
        try:
            msg = _recv(sock)
        except (socket.timeout, EOFError, OSError):
            return 0
        op = msg[0]
        if op == "quit":
            return 0
        try:
            if op == "load":
                args = msg[1]
                if fake:
                    from ._testing import FakeWhisper
                    model = FakeWhisper(fake, **args)
                elif os.environ.get("MEETNOTES_E2E_TRANSCRIBER"):  # end-to-end tests only
                    from ._testing import ScriptTranscriber
                    model = ScriptTranscriber(os.environ["MEETNOTES_E2E_TRANSCRIBER"])
                else:
                    from .transcribe import Transcriber
                    model = Transcriber(args["model_size"], args["device"], args["compute_type"],
                                        args["language"], args["vocabulary"])
                model.load()
                dev = getattr(getattr(model, "_model", None), "model", None)
                device = getattr(dev, "device", None) or args["device"]
                print(f"loaded {args['model_size']} on {device}", file=sys.stderr, flush=True)
                _send(sock, ("ok", {"device": str(device)}))
            elif op == "ping":
                _send(sock, ("ok", None))
            elif op == "selftest":
                noise = (np.random.default_rng(0).standard_normal(TARGET_RATE) * 0.05).astype(np.float32)
                if hasattr(model, "selftest"):
                    model.selftest()
                elif hasattr(model, "_model"):
                    segs, _ = model._model.transcribe(noise, language=model.language, beam_size=1,
                                                      vad_filter=False, condition_on_previous_text=False)
                    list(segs)
                _send(sock, ("ok", None))
            elif op == "transcribe":
                _, pcm, offset, speaker = msg
                samples = np.frombuffer(pcm, dtype=np.int16)
                segs = model.transcribe(samples, offset, speaker)
                _send(sock, ("ok", [(s.start, s.end, s.speaker, s.text, s.no_speech) for s in segs]))
            else:
                _send(sock, ("error", f"unknown request {op!r}"))
        except Exception:
            import traceback
            tb = traceback.format_exc()
            print(tb, file=sys.stderr, flush=True)
            try:
                _send(sock, ("error", tb[-2000:]))
            except OSError:
                return 1


if __name__ == "__main__":
    sys.exit(_worker(int(sys.argv[1]), sys.argv[2]))


# -- self-test and diagnostics (used by the app) ----------------------------------

_TEST_LOCK = threading.Lock()
_testing = False


def _requested(cfg) -> tuple[str, str, str]:
    return (cfg.whisper_model, cfg.whisper_device or "auto", cfg.whisper_compute_type or "auto")


def last_test(cfg) -> dict:
    return (_memory().get("_tests") or {}).get(_key(*_requested(cfg))) or {}


def _save_test(cfg, result: dict) -> None:
    from .config import write_json
    mem = _memory()
    mem.setdefault("_tests", {})[_key(*_requested(cfg))] = result
    try:
        write_json(_memory_path(), mem)
    except OSError:
        pass


def run_self_test(cfg) -> dict:
    """Start the speech engine the way a meeting would and run it once.
    Falls back and remembers a working setup if the configured one fails."""
    global _testing
    from . import __version__
    from .session import make_transcriber
    if not _TEST_LOCK.acquire(blocking=False):
        return {"busy": True}
    _testing = True
    prev = state().get("phase")
    try:
        set_state(phase="testing")
        t = make_transcriber(cfg)
        if not hasattr(t, "self_test"):
            res = {"ok": True, "model": cfg.whisper_model, "device": "-", "seconds": 0}
        else:
            res = {"ok": True, **t.self_test()}
        set_state(phase="ready")
    except Exception as exc:
        res = {"ok": False, "error": str(exc)[:400]}
        log.warning("Speech self-test failed: %s", exc)
    finally:
        _testing = False
        _TEST_LOCK.release()
        if state().get("phase") == "testing":
            set_state(phase=prev if prev not in (None, "testing") else "idle")
    res.update(at=time.strftime("%Y-%m-%dT%H:%M:%S"), version=__version__)
    _save_test(cfg, res)
    log.info("Speech self-test: %s", res)
    return res


def test_in_background(cfg, close_after: bool = False) -> bool:
    if _testing:
        return False

    def run():
        run_self_test(cfg)
        if close_after:
            from . import live
            from .session import make_transcriber
            if live.current() is None:  # free the memory until a meeting needs it
                t = make_transcriber(cfg)
                if hasattr(t, "close"):
                    t.close()
    threading.Thread(target=run, daemon=True, name="speech-test").start()
    return True


def needs_test(cfg) -> bool:
    from . import __version__
    t = last_test(cfg)
    return not t or not t.get("ok") or t.get("version") != __version__


def reset(cfg) -> None:
    """Forget the safer setup chosen earlier and try the configured one again."""
    from .session import _TRANSCRIBERS, _TRANSCRIBERS_LOCK
    forget_fallbacks()
    with _TRANSCRIBERS_LOCK:
        for t in list(_TRANSCRIBERS.values()):
            if hasattr(t, "close"):
                t.close()
        _TRANSCRIBERS.clear()
    set_state(phase="idle", error="", fallback="", fallback_reason="")


def info(cfg) -> dict:
    mem = _memory().get(_key(*_requested(cfg))) or {}
    setup = {"model": cfg.whisper_model, "device": cfg.whisper_device, "fallback": False}
    if mem:
        setup = {"model": mem.get("model"), "device": mem.get("device"), "fallback": True, "reason": mem.get("reason", "")}
    return {"speech": state(), "setup": setup, "test": last_test(cfg), "testing": _testing}


def diagnostics_text(cfg, lines: int = 120) -> str:
    import platform
    from . import __version__
    from .service import log_path
    out = [f"Meetnotes {__version__} | {platform.platform()} | Python {platform.python_version()} | "
           f"{os.cpu_count()} CPU | model {cfg.whisper_model} / {cfg.whisper_device} / {cfg.whisper_compute_type} | "
           f"mode {cfg.transcribe_mode}",
           f"speech: {json.dumps(info(cfg), ensure_ascii=False)}", ""]
    for title, path in (("meetnotes.log", log_path()), ("speech-worker.log", worker_log_path())):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            text = ["(ei vielä)"]
        out += [f"===== {title} =====", *text, ""]
    return "\n".join(out)

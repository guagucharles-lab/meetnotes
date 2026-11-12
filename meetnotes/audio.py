"""Audio capture.

Two sources are captured side by side:

* ``mic``    - your microphone (what you say)
* ``system`` - what you hear: the other participants, via a loopback /
               monitor device (PulseAudio/PipeWire monitor on Linux, BlackHole
               on macOS, Stereo Mix / VB-Cable on Windows)

Each source produces 16 kHz mono int16 PCM. Nothing here talks to the meeting
app; it only records what your sound card is already playing and capturing.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

TARGET_RATE = 16000


def _resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resample. Good enough for speech transcription."""
    if src_rate == dst_rate or len(samples) == 0:
        return samples
    n_out = int(round(len(samples) * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples.astype(np.float32)).astype(np.int16)


class AudioSource:
    """Base class: accumulates int16 mono samples at TARGET_RATE."""

    name = "source"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self.error: str | None = None

    def _push(self, samples: np.ndarray) -> None:
        with self._lock:
            self._chunks.append(samples)

    def take(self) -> np.ndarray:
        """Return everything captured since the last call."""
        with self._lock:
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(chunks)

    def start(self) -> None:  # pragma: no cover - hardware
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - hardware
        raise NotImplementedError


class SoundDeviceSource(AudioSource):
    """Captures from a PortAudio device via the sounddevice package."""

    def __init__(self, device: int | str | None, label: str) -> None:
        super().__init__()
        self.device = device
        self.name = label
        self._stream = None
        self._rate = TARGET_RATE

    def start(self) -> None:  # pragma: no cover - hardware
        import sounddevice as sd

        def callback(indata, frames, time_info, status):
            if status and "overflow" not in str(status).lower():
                self.error = str(status)
            mono = indata[:, 0] if indata.ndim > 1 else indata
            self._push(_resample(mono.copy(), self._rate, TARGET_RATE))

        try:
            self._stream = sd.InputStream(
                device=self.device, channels=1, samplerate=TARGET_RATE,
                dtype="int16", callback=callback,
            )
            self._stream.start()
        except Exception:
            # Device refuses 16 kHz: open at its native rate and resample.
            info = sd.query_devices(self.device, "input")
            self._rate = int(info["default_samplerate"])
            self._stream = sd.InputStream(
                device=self.device, channels=1, samplerate=self._rate,
                dtype="int16", callback=callback,
            )
            self._stream.start()

    def stop(self) -> None:  # pragma: no cover - hardware
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class PulseMonitorSource(AudioSource):
    """Linux: records the default sink's monitor with parec (PulseAudio or
    PipeWire's pulse shim). This captures exactly what the speakers play."""

    name = "system (pulse monitor)"

    def __init__(self, monitor: str) -> None:
        super().__init__()
        self.monitor = monitor
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:  # pragma: no cover - hardware
        self._proc = subprocess.Popen(
            ["parec", "-d", self.monitor, "--format=s16le",
             f"--rate={TARGET_RATE}", "--channels=1", "--latency-msec=100"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:  # pragma: no cover - hardware
        assert self._proc and self._proc.stdout
        block = TARGET_RATE * 2 // 10  # 100 ms of int16
        while True:
            data = self._proc.stdout.read(block)
            if not data:
                break
            self._push(np.frombuffer(data, dtype=np.int16).copy())

    def stop(self) -> None:  # pragma: no cover - hardware
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


class WasapiLoopbackSource(AudioSource):
    """Windows: records whatever the *default output device* plays, using
    WASAPI loopback (soundcard package). Works with USB and Bluetooth
    headphones, where 'Stereo Mix' usually does not, and needs no virtual
    cable. The default output is looked up when each meeting starts."""

    name = "system (WASAPI loopback)"

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.speaker_name = ""

    def start(self) -> None:  # pragma: no cover - Windows only
        import soundcard as sc
        self.speaker_name = sc.default_speaker().name
        self.name = f"system (loopback of {self.speaker_name})"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:  # pragma: no cover - Windows only
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)  # COM per thread
        except Exception:
            pass
        try:
            import soundcard as sc
            speaker = sc.default_speaker()
            loop = sc.get_microphone(id=str(speaker.id), include_loopback=True)
            with loop.recorder(samplerate=TARGET_RATE, channels=1, blocksize=TARGET_RATE // 10) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=TARGET_RATE // 10)  # float32, 100 ms
                    mono = data[:, 0] if data.ndim > 1 else data
                    self._push((np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16))
        except Exception as exc:
            self.error = f"loopback stopped: {exc}"

    def stop(self) -> None:  # pragma: no cover - Windows only
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


def wasapi_loopback_available() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        import soundcard  # noqa: F401
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Device discovery
# --------------------------------------------------------------------------

@dataclass
class DeviceInfo:
    index: int
    name: str
    inputs: int
    rate: float
    hostapi: str


def list_input_devices() -> list[DeviceInfo]:
    try:
        import sounddevice as sd
    except Exception:
        return []
    out = []
    apis = sd.query_hostapis()
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            out.append(DeviceInfo(idx, dev["name"], dev["max_input_channels"],
                                  dev["default_samplerate"], apis[dev["hostapi"]]["name"]))
    return out


def resolve_device(spec: str, devices: list[DeviceInfo]) -> int | None:
    """Turn a config spec (index or name substring) into a device index."""
    spec = spec.strip()
    if spec.isdigit():
        return int(spec)
    low = spec.lower()
    for d in devices:
        if low in d.name.lower():
            return d.index
    raise ValueError(f"No input device matching {spec!r}. Run `meetnotes devices`.")


SYSTEM_DEVICE_HINTS = {
    "Darwin": ["blackhole", "loopback", "soundflower", "multi-output"],
    "Windows": ["stereo mix", "cable output", "loopback", "what u hear", "wave out mix"],
    "Linux": ["monitor", "pulse", "pipewire"],
}


def pick_sink(default: str, short_sinks: str) -> str:
    """Choose which output to record. `short_sinks` is `pactl list short sinks`
    output (idx, name, module, format, state). If the default output is idle
    but another one is playing (e.g. the meeting app was pointed straight at
    a headset), record the one that is playing."""
    running = []
    for line in short_sinks.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 5 and parts[-1].upper() == "RUNNING":
            running.append(parts[1])
    if default in running or not running:
        return default
    return running[0]


def default_pulse_monitor() -> str | None:
    """Linux: '<sink>.monitor' for the output in use, if pactl is available."""
    if not shutil.which("pactl") or not shutil.which("parec"):
        return None
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                              text=True, timeout=3, check=True).stdout.strip()
        sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True,
                               text=True, timeout=3).stdout
    except Exception:
        return None
    sink = pick_sink(sink, sinks)
    return f"{sink}.monitor" if sink else None


def auto_system_device(devices: list[DeviceInfo]) -> int | None:
    hints = SYSTEM_DEVICE_HINTS.get(platform.system(), [])
    for hint in hints:
        for d in devices:
            if hint in d.name.lower():
                return d.index
    return None


def build_sources(mic_spec: str, system_spec: str) -> tuple[AudioSource | None, AudioSource | None, list[str]]:
    """Create (mic, system) sources from config specs. Returns notes for the user."""
    notes: list[str] = []
    devices = list_input_devices()

    mic: AudioSource | None = None
    if mic_spec.lower() != "none":
        idx = resolve_device(mic_spec, devices) if mic_spec else None
        mic = SoundDeviceSource(idx, "mic")

    system: AudioSource | None = None
    if system_spec.lower() != "none":
        if system_spec.lower() == "loopback":
            if not wasapi_loopback_available():
                raise ValueError('system_device = "loopback" needs Windows and the soundcard package')
            system = WasapiLoopbackSource()
        elif system_spec:
            if platform.system() == "Linux" and system_spec.endswith(".monitor"):
                system = PulseMonitorSource(system_spec)
            else:
                system = SoundDeviceSource(resolve_device(system_spec, devices), "system")
        elif wasapi_loopback_available():
            system = WasapiLoopbackSource()
        else:
            monitor = default_pulse_monitor() if platform.system() == "Linux" else None
            if monitor:
                system = PulseMonitorSource(monitor)
            else:
                idx = auto_system_device(devices)
                if idx is not None:
                    system = SoundDeviceSource(idx, "system")
                else:
                    notes.append(
                        "No system-audio (loopback) device found, so only your microphone is "
                        "recorded. The other participants will be missing from the transcript. "
                        "See README 'Audio setup' for your OS."
                    )
    return mic, system, notes


# --------------------------------------------------------------------------
# WAV writing
# --------------------------------------------------------------------------

class StereoWavWriter:
    """Left = mic, right = system. Keeps the two sides separable for labelling."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(2)
        self._wav.setsampwidth(2)
        self._wav.setframerate(TARGET_RATE)

    def write(self, mic: np.ndarray, system: np.ndarray) -> None:
        n = max(len(mic), len(system))
        if n == 0:
            return
        left = np.zeros(n, dtype=np.int16)
        right = np.zeros(n, dtype=np.int16)
        left[: len(mic)] = mic
        right[: len(system)] = system
        self._wav.writeframes(np.column_stack([left, right]).tobytes())

    def close(self) -> None:
        self._wav.close()


def truncate_wav(path: Path, seconds: float) -> None:
    """Keep only the first `seconds` of a WAV file (rewrites it in place)."""
    with wave.open(str(path), "rb") as src:
        params = src.getparams()
        keep = min(src.getnframes(), max(0, int(seconds * src.getframerate())))
        data = src.readframes(keep)
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(data)
    tmp.replace(path)


def read_wav_channels(path: Path) -> tuple[np.ndarray, np.ndarray, int]:
    """Return (left, right, rate) as int16 arrays. Mono files return (mono, empty)."""
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if not raw and path.stat().st_size > 44:
        # The recorder was killed before it could finalise the header (crash,
        # shutdown). The samples are still there right after the 44-byte header.
        raw = path.read_bytes()[44:]
        raw = raw[: len(raw) - len(raw) % (channels * width)]
    if width != 2:
        raise ValueError("Only 16-bit PCM WAV files are supported")
    data = np.frombuffer(raw, dtype=np.int16)
    if channels == 1:
        left, right = data, np.zeros(0, dtype=np.int16)
    else:
        data = data.reshape(-1, channels)
        left, right = data[:, 0].copy(), data[:, 1].copy()
    if rate != TARGET_RATE:
        left = _resample(left, rate, TARGET_RATE)
        right = _resample(right, rate, TARGET_RATE)
    return left, right, TARGET_RATE


def platform_audio_help() -> str:
    system = platform.system()
    if system == "Darwin":
        return (
            "macOS: install BlackHole 2ch (brew install blackhole-2ch), then in\n"
            "Audio MIDI Setup create a Multi-Output Device containing your headphones\n"
            "(first, as clock source) and BlackHole, tick Drift Correction for\n"
            "BlackHole, and select it as the system output. Set\n"
            "system_device = \"BlackHole\" in the config. In Zoom/Teams/Meet keep the\n"
            "speaker on 'Same as system'. Grant Terminal microphone and (for `watch`)\n"
            "Accessibility access in System Settings > Privacy."
        )
    if system == "Windows":
        return (
            "Windows: nothing to set up. meetnotes records your default output device\n"
            "(speakers or headphones, USB and Bluetooth included) with WASAPI loopback.\n"
            "In Zoom/Teams/Meet keep the speaker on the system default, or make the\n"
            "headset the Windows default output."
        )
    return (
        "Linux: PulseAudio/PipeWire is detected automatically via the default\n"
        "sink's monitor (needs `pactl` and `parec`). Override with\n"
        "system_device = \"alsa_output.xxx.monitor\" if needed."
    )

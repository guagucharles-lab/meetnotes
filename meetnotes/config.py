"""User configuration: ~/.config/meetnotes/config.toml."""
from __future__ import annotations

import os
import platform
import tomllib
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "meetnotes" / "config.toml"


def state_dir() -> Path:
    """Where the background service keeps its pid, status, control flags and log."""
    env = os.environ.get("MEETNOTES_STATE_DIR")
    if env:
        path = Path(env)
    elif platform.system() == "Darwin":
        path = Path.home() / "Library" / "Application Support" / "meetnotes"
    elif platform.system() == "Windows":
        path = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "meetnotes"
    else:
        path = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "meetnotes"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Config:
    # Where meeting folders are written.
    notes_dir: str = "~/MeetingNotes"

    # Audio. Device specs are a substring of the device name, an index, or
    # "none" to disable that source. "" means auto-detect.
    mic_device: str = ""
    system_device: str = ""
    sample_rate: int = 16000
    chunk_seconds: int = 30
    keep_audio: bool = True

    # Transcription (faster-whisper, runs locally).
    whisper_model: str = "small"       # tiny | base | small | medium | large-v3
    whisper_device: str = "auto"       # auto | cpu | cuda
    whisper_compute_type: str = "auto" # auto | int8 | float16 | ...
    language: str = ""                 # "" = auto-detect, or e.g. "en"
    vocabulary: list[str] = field(default_factory=list)  # names/jargon Whisper should spell right
    speaker_labels: bool = True        # label "Me" vs "Others" using the two channels
    transcribe_mode: str = "mixed"     # mixed = one Whisper pass, speaker from channel loudness (half the CPU)
                                       # separate = one pass per channel (twice the CPU)
    safe_word: str = "omerta"          # say this and listening stops for the rest of the meeting ("" = off)
    safe_word_action: str = "stop"     # stop = keep notes of what came before | discard = delete the whole meeting
    dedupe_echo: bool = True           # drop "Me" lines that are just the speakers leaking into the mic
    mic_gate: bool = False             # also mute the mic while only others talk (for laptop speakers)

    # Notes (Claude).
    claude_model: str = "claude-opus-5-5"
    effort: str = "medium"             # low | medium | high | xhigh | max
    live_notes: bool = False           # write running notes during the meeting
    live_notes_interval: int = 300     # seconds between live-note refreshes
    extra_instructions: str = ""       # anything Claude should always do in notes
    notes_language: str = ""           # "" = same language as the meeting, or e.g. "Finnish"

    # Auto-detect meetings (meetnotes watch).
    watch_platforms: list[str] = field(default_factory=lambda: ["zoom", "teams", "meet"])
    poll_seconds: int = 5
    end_grace_seconds: int = 45        # meeting window gone for this long => stop
    idle_stop_minutes: int = 15        # nobody spoke for this long => stop (0 = never)
    ignore_titles: list[str] = field(default_factory=list)  # never record meetings whose title contains these
    notify: bool = False               # desktop notification when notes are ready (off: screen sharing)
    keep_empty: bool = False           # keep folders of sessions where nobody spoke
    auto_recover: bool = True          # on start, finish notes for sessions cut off by a crash

    # Calendar: iCal/ICS feed addresses (Google "secret address in iCal format",
    # Outlook "publish calendar" ICS, iCloud public calendar). Read-only.
    calendar_urls: list[str] = field(default_factory=list)
    notes_template: str = "auto"       # auto | general | sales | one_on_one | standup | interview | project | board | <custom id>
    templates: list[dict] = field(default_factory=list)  # custom templates, see templates.py
    watch_words: list[str] = field(default_factory=list) # words to flag instantly in the live feed (no API call)
    webhook_url: str = ""              # POST notes JSON here after every meeting (Zapier, n8n, your own script)
    update_url: str = ""               # update.json address; empty = the one baked in at release
    update_check: bool = True          # look for a new version once a day

    # Assistant ("Avustaja"): prepares before, advises during the meeting.
    assistant_live: bool = False       # follow the meeting live and suggest what to bring up
    assistant_interval: int = 45       # seconds between live updates (each is one Claude call)
    assistant_style: str = "balanced"  # quiet = answer questions, flag decisions | balanced | active
    translate_to: str = ""             # e.g. "en": translate every meeting (transcript + notes) afterwards
    translate_live: bool = False       # also show live translated captions during the meeting
    assistant_model: str = "claude-opus-5-5"  # e.g. claude-sonnet-5-5 for cheaper live updates
    assistant_effort: str = "low"      # effort for live updates; preparation always uses "high"

    # Screen recording (video of your screen with the meeting audio).
    record_screen: bool = False
    screen_monitor: int = 1            # 1 = main screen, 2 = second screen, 0 = all screens together
    screen_fps: int = 5                # frames per second; meetings are mostly still images
    screen_max_width: int = 1280       # scaled down to this width to keep files small

    # App (desktop window + phone remote).
    app_server: bool = True            # the background service serves the app on this computer
    app_port: int = 47800              # http://127.0.0.1:47800, this computer only
    phone_access: bool = False         # also serve it to phones on the same Wi-Fi (HTTPS + pairing code)
    phone_port: int = 47801
    tray_icon: bool = True             # Windows: keep an icon in the taskbar notification area

    @property
    def notes_root(self) -> Path:
        return Path(os.path.expanduser(self.notes_dir))


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    cfg = Config()
    if not path.exists():
        return cfg
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    known = {f.name for f in fields(Config)}
    for key, value in data.items():
        if key in known:
            setattr(cfg, key, value)
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{text}"'


COMMENTS = {
    "notes_dir": "Where meeting folders are written.",
    "mic_device": "Microphone: name substring, index, \"none\" to disable, \"\" for the default input.",
    "system_device": "System audio (what you hear): name substring, index, \"none\", or \"\" for auto.",
    "whisper_model": "Whisper size: tiny | base | small | medium | large-v3. Bigger = slower, better.",
    "language": "Spoken language code, e.g. \"en\". Empty = auto-detect.",
    "claude_model": "Claude model used to write the notes.",
    "live_notes": "Refresh a running notes file during the meeting (costs extra API calls).",
    "extra_instructions": "Standing instructions for the notes, e.g. \"Always list budget figures.\"",
    "watch_platforms": "Which meeting apps `meetnotes watch` should look for.",
    "vocabulary": "Names and jargon to help Whisper spell them, e.g. [\"Priya\", \"Kubernetes\"].",
    "safe_word": "Say this word during a meeting and listening stops for the rest of it. Empty = off.",
    "safe_word_action": "stop = keep notes for the part before the word | discard = delete the whole meeting.",
    "transcribe_mode": "mixed = transcribe once and tell Me/Others apart by loudness (half the CPU); separate = each side on its own.",
    "dedupe_echo": "Remove your mic picking up the other participants from the speakers.",
    "mic_gate": "Stronger echo removal for laptop speakers. May clip you when you talk over others.",
    "notes_language": "Language of the notes. Empty = same as the meeting.",
    "idle_stop_minutes": "Auto mode stops a session after this many silent minutes (0 = never).",
    "ignore_titles": "Never record meetings whose window title contains any of these.",
    "notify": "Desktop notification when notes are ready. Off by default so nothing pops up while you share your screen.",
    "keep_empty": "Keep the folder of a session where nobody spoke.",
    "calendar_urls": "ICS feed addresses: upcoming meetings, automatic titles and briefings. Read-only.",
    "notes_template": "Meeting type for the notes: auto picks one; or sales, one_on_one, standup, interview, project, board.",
    "watch_words": "Words flagged in the live feed the moment they are said, e.g. [\"kilpailija\", \"alennus\"].",
    "webhook_url": "After each meeting the notes are POSTed as JSON to this address (empty = off).",
    "assistant_live": "Follow the meeting live with Claude and suggest what to bring up (one call every assistant_interval seconds).",
    "assistant_style": "quiet = only answers and decisions | balanced | active = more suggestions.",
    "assistant_model": "Model for live updates; preparation and notes use claude_model.",
    "translate_to": "Language code (en, fi, sv, ...) to translate each meeting into afterwards. Empty = off.",
    "translate_live": "Show live translated captions during meetings (needs translate_to).",
    "record_screen": "Also record a video of the screen during meetings (screen.mp4, with the meeting audio).",
    "screen_monitor": "1 = main screen, 2 = second screen, 0 = all screens together.",
    "app_port": "Port of the app on this computer (127.0.0.1 only).",
    "phone_access": "Let your phone on the same Wi-Fi control meetnotes (HTTPS, needs the pairing QR code).",
    "tray_icon": "Windows: show an icon in the notification area while the app window is open.",
    "auto_recover": "When the service starts, write notes for sessions interrupted by a crash or shutdown.",
}


def save(cfg: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# meetnotes configuration", ""]
    tables = []
    for key, value in asdict(cfg).items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            tables.append((key, value))
            continue
        if key in COMMENTS:
            lines.append(f"# {COMMENTS[key]}")
        lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    for key, rows in tables:
        for row in rows:
            lines.append(f"[[{key}]]")
            for k, v in row.items():
                lines.append(f"{k} = {_toml_value(v)}")
            lines.append("")
    path.write_text("\n".join(lines))
    return path


def write_json(path: Path, data) -> None:
    """Write JSON so a reader never sees a half-written file: write a temp file,
    then swap it in. Windows refuses the swap while another thread has the file
    open for a moment, so retry briefly."""
    import json
    import time
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.02)

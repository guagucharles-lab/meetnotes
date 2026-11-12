# meetnotes

Private meeting notes for **Zoom, Microsoft Teams and Google Meet**, written by Claude.

meetnotes runs on your own computer. It listens to the meeting audio your
sound card is already playing (the other participants) and capturing (your
microphone), transcribes it locally with Whisper, and when the meeting ends
asks Claude for a summary, decisions, action items and your follow-ups.

**Nothing joins the call.** There is no bot participant, no "Recording" badge
and no extra tile in the gallery. The meeting apps are not touched at all;
meetnotes only reads window titles to know when a meeting starts and ends.

> **Why not an invisible bot?** Every meeting platform shows all participants to
> everyone, and bots that join a call are listed like anyone else. A truly
> hidden participant is not possible, so this tool works from your side of the
> call instead. Please check your local law and company policy on recording
> conversations. In many places you must tell the other people you are
> recording or transcribing.

## What you get

For every meeting a folder like `~/MeetingNotes/2026-10-29_1500_Weekly-sync/`:

| File | Contents |
|------|----------|
| `notes.md` | Summary, key points, decisions, action-item table, your follow-ups, open questions |
| `transcript.md` | Timestamped transcript, labelled `Me:` and `Others:` |
| `live-notes.md` | Running notes refreshed during the meeting (optional) |
| `audio.wav` | The recording, left channel = you, right channel = everyone else (optional) |
| `notes.json`, `meta.json` | Machine-readable notes and metadata |

## Desktop and phone app

* **Windows:** open the `windows` folder and double-click `Asenna Meetnotes.cmd`.
  It installs Python if needed (winget, per-user), the app into
  `%LOCALAPPDATA%\meetnotes`, Edge WebView2 if missing, a **Meetnotes icon on
  the desktop** and in the Start menu, and start-at-login. `Poista Meetnotes.cmd`
  removes it and keeps your notes.
* **Any OS:** `pip install ".[app]"`, then `meetnotes app` (or `meetnotes-app`).

The background service serves the app on `http://127.0.0.1:47800`; the desktop
window (pywebview) shows it. Pages: home (status, record now, pause, stop,
automatic recording on/off), meetings (notes, transcript, delete), ask, settings
(API key saved in the OS credential store, language, model, safe word, privacy).

**Phone:** turn on *Settings → Puhelin* and scan the QR code. The phone gets
the same app over HTTPS on your Wi-Fi (`phone_port`, self-signed certificate,
pairing token in the QR code) and can add it to the home screen. It can act as
a remote for the computer, or record **in-person meetings with the phone's own
mic**: audio is uploaded in 5-second pieces while recording, and the computer
transcribes it and writes the notes. Phones cannot capture a Teams/Zoom/Meet
call running on the phone itself (iOS and Android block other apps' call audio).

## Releases and in-app updates

`python release.py --owner <github-user>` builds `dist/`: the wheel,
`Meetnotes-Windows.zip`, `Meetnotes-macOS.zip`, `Meetnotes.apk` (when the
Debian Android tools are installed) and `update.json` (version, wheel URL +
SHA-256, APK version code). `.github/workflows/release.yml` does the same on
a `v*` tag and attaches everything to a GitHub release. The update address
`https://github.com/<owner>/<repo>/releases/latest/download/update.json` is
baked into the package (`meetnotes/_release.json`; `update_url` in config
overrides it).

Installed copies check it daily (`meetnotes/update.py`). `Asetukset →
Päivitykset → Päivitä nyt` (or `meetnotes update`) downloads the wheel,
verifies the checksum, `pip install --upgrade`s it into the existing venv and
sets a restart flag; the watcher restarts itself after the current meeting
(`service.restart_background`). Config, state, notes and the Whisper model are
outside the package and untouched. The Android app is a shell for the
computer's web app, so it updates with the computer; a newer APK (same signing
key, higher `versionCode`) is offered from the settings page.

`mac/` holds the macOS installer: venv under `~/Library/Application Support/meetnotes`,
BlackHole via Homebrew when available, a `Meetnotes.app` bundle in `~/Applications`.

## Android app

`android/` is a small native app (one Java activity, no Gradle) that shows the
same app in a WebView. It pairs with a 6-digit, single-use code from
*Asetukset → Puhelin → Android-sovellus* (`POST /api/pair/claim` on the Wi-Fi
listener; 10-minute lifetime, locked for a minute after five wrong codes) and
pins the computer's certificate SHA-256, so the self-signed certificate is
accepted without a warning and nothing else is. It grants the page the
microphone and keeps the screen on while the phone records. Build with
`android/build.sh` (Debian/Ubuntu packages listed in the script); keep
`android/meetnotes.keystore`, since Android only installs updates signed with the
same key.

## Install

Requires Python 3.11+ and an Anthropic API key. Finnish guide: [OHJE.md](OHJE.md).

```bash
# macOS
brew install portaudio blackhole-2ch
# Debian / Ubuntu
sudo apt install libportaudio2 pulseaudio-utils wmctrl
# Windows: nothing extra

pip install .
export ANTHROPIC_API_KEY=sk-ant-...   # or run `ant auth login`
meetnotes setup
meetnotes check-audio   # play a video in your headphones and talk
```

## Audio setup (capturing the other participants)

Your microphone is captured directly. The other side is captured from your
output device. Headphones are the best case: no echo, clean Me/Others labels.

* **Windows**: nothing to install. The default output device (speakers or USB,
  Bluetooth or wired headphones) is recorded with WASAPI loopback.
* **macOS**: `brew install blackhole-2ch`. In *Audio MIDI Setup* create a
  Multi-Output Device with your headphones first (clock source) and BlackHole
  with Drift Correction, and make it the system output. Set
  `system_device = "BlackHole"`.
* **Linux**: the PulseAudio/PipeWire monitor of the output that is actually
  playing (Bluetooth headsets included) is used automatically.

In every case keep the meeting app's speaker on "Same as system".
`meetnotes check-audio` shows live meters for both sides and says what is wrong.

## Use

```bash
meetnotes start            # background recorder: no window, records meetings by itself
meetnotes autostart on     # start it at every login
meetnotes status           # running? recording? paused?
meetnotes pause            # off the record until resume or the meeting ends
meetnotes resume
meetnotes stop             # finishes the current meeting's notes, then exits

meetnotes record --title "Design review" [--minutes 45] [--show]   # manual, foreground

meetnotes list
meetnotes open
meetnotes search budget
meetnotes ask "What did Priya commit to this week?"
meetnotes summarize ~/MeetingNotes/2026-10-29_1500_Weekly-sync
meetnotes recover
```

### Auto-detection

The recorder polls window titles every few seconds and starts a session after
seeing a meeting window twice in a row:

* Google Meet: a tab titled `Meet – <code or name>` or a `meet.google.com/xxx-xxxx-xxx` URL
* Zoom: the `Zoom Meeting` / `Zoom Webinar` window (or `CptHost.exe` on Windows)
* Teams: a meeting window `<Meeting name> | Microsoft Teams` (not Chat/Calendar)

A session ends when the window has been gone for `end_grace_seconds` (45 s),
or after `idle_stop_minutes` (15) without speech. Titles matching
`ignore_titles` are never recorded. On macOS grant Accessibility permission;
on Linux install `wmctrl` or `xdotool`.

### Calendar, templates, tasks, continuity

* **Calendar** (`meetnotes/cal.py`): read-only ICS feeds (`calendar_urls`),
  recurrences expanded with `recurring-ical-events`, cached in the state dir
  and refreshed every 15 min by the watcher. The event happening now names
  the recording and supplies attendees; `POST /api/calendar/<uid>/briefing`
  makes a briefing from an invite (agenda from numbered/bulleted lines).
* **Templates** (`meetnotes/templates.py`): meeting types add sections to the
  notes (`notes_template`, custom `[[templates]]` in config, switchable during
  a meeting via `/api/live/template`). Notes JSON now also has `chapters`
  (timestamped topics), `sections` and `followups_from_last_time`.
* **Own notes**: `/api/live/notes` stores what you type during the meeting
  (`my-notes.md`); the notes writer treats them as the most reliable account.
* **Series continuity**: `previous_in_series()` finds the latest earlier
  meeting with the same normalized title and the writer reports each of its
  action items as done / open / not mentioned.
* **Tasks** (`meetnotes/tasks.py`): action items and follow-ups across all
  meetings, done-state in `_tasks.json`; open ones feed the briefing context.
* **Stats** (`meetnotes/stats.py`): talk share, questions, longest monologue,
  words per minute, computed locally into `meta.json`.
* **Watch words**: `watch_words` are flagged in the live feed as they are said,
  with no API call. **Webhook**: `webhook_url` gets the notes as JSON.

### Assistant (briefings + live suggestions)

`meetnotes/assistant.py`. A *briefing* (title, date, agenda, goals,
participants, materials) is stored under `~/MeetingNotes/_briefings/`. Related
past meetings are found by keyword over titles, notes and transcripts.

* **Before:** `prepare()` asks Claude (structured output) for a brief: where
  things stand, what was said before, open items, talking points, questions,
  risks. `chat()` answers questions over the same context (system prompt
  cached with `cache_control`).
* **During:** `LiveAssistant` runs in the recording session. New transcript
  lines are sent every `assistant_interval` seconds as the next turn of ONE
  append-only conversation (top-level `cache_control`, so the growing history
  is served from the prompt cache). Each turn returns agenda status, nudges
  (`bring_up`, `ask`, `heads_up`, `decision`, `action`) and an answer to the
  user's question, as JSON schema output. A failed turn is rolled back and its
  lines resent. Briefings are matched to a starting meeting by date and
  window-title keywords; the app can attach one later. `POST /api/live/ask`
  answers a question immediately; `/api/live` serves the feed to the app.
* **After:** the agenda status and briefing go to the notes writer, and
  `assistant.md` / `assistant.json` are saved in the meeting folder.

Defaults: `claude-opus-5-5` at effort `low` for live turns (a few seconds
per update), `high` for preparation; `assistant_model` can be
`claude-sonnet-5-5`. Live assistance is off by default (`assistant_live`).

### Screen recording

`record_screen = true` (or *Asetukset → Näytön tallennus* in the app, or
`meetnotes record --screen`) also saves `screen.mp4`: the chosen monitor at
`screen_fps` (5) scaled to `screen_max_width` (1280), with the meeting audio
mixed in. Capture uses mss (the OS screenshot API: no capture border, no extra
executable) and H.264 via PyAV; timestamps follow the wall clock so audio stays
in sync. Paused time is black, and the video is cut at the safe word like the
audio. The app's meeting page has a Video tab (served with HTTP Range support).
Windows the OS marks as capture-protected (e.g. Teams "Prevent screen capture")
come out black.

### Safe word

Say **"omerta"** during a meeting and listening stops for the rest of it.
Only your own mic can trigger it. Everything from the sentence containing the
word onwards is removed from the transcript, the WAV and the notes, and the
earlier part is summarised as usual (`safe_word_action = "discard"` deletes the
whole meeting instead). Matching is fuzzy so accents and spellings like
"omertà", "omertaa" or "o merta" work, while everyday words such as "merta" or
"omena" do not trigger it. Segments Whisper rates as probably not speech are
ignored. Detection happens when the chunk is transcribed (up to about
`chunk_seconds`). Tip: mute yourself in the meeting app first. meetnotes still
hears your mic, but the other participants do not hear the word.

### Transcript quality

* Chunks are cut in the nearest pause, so words are not split between chunks.
* Mic and output are transcribed separately (`Me:` / `Others:`).
* `dedupe_echo` drops mic lines that are just the speakers leaking in;
  `mic_gate` additionally mutes the mic while only the far side talks.
* `vocabulary` biases Whisper towards names and jargon; set `language` to stop
  per-chunk language guessing; `large-v3-turbo` is much better for non-English.
* Sessions with fewer than 20 words are discarded; sessions cut off by a crash
  are finished on the next start (`auto_recover`).

## Configuration

`~/.config/meetnotes/config.toml`, created by `meetnotes setup`:

```toml
notes_dir = "~/MeetingNotes"
mic_device = ""            # "" default mic, name substring, index, or "none"
system_device = ""         # "" auto, e.g. "BlackHole", "Stereo Mix", "none"
whisper_model = "small"    # tiny | base | small | medium | large-v3 | large-v3-turbo
language = ""              # "" auto-detect, or "en", "fi", ...
vocabulary = []            # e.g. ["Priya", "Kubernetes"]
speaker_labels = true      # label Me / Others using the two channels
safe_word = "omerta"       # "" disables
safe_word_action = "stop"  # stop | discard
dedupe_echo = true
mic_gate = false
claude_model = "claude-opus-5-5"
effort = "medium"          # low | medium | high | xhigh | max
live_notes = false         # refresh live-notes.md every live_notes_interval s
live_notes_interval = 300
extra_instructions = ""    # e.g. "Always list the budget numbers mentioned."
notes_language = ""        # "" = meeting language, or e.g. "Finnish"
keep_audio = true
watch_platforms = ["zoom", "teams", "meet"]
end_grace_seconds = 45
idle_stop_minutes = 15
ignore_titles = []         # e.g. ["1:1", "HR"]
notify = false             # off so nothing pops up while you share your screen
keep_empty = false
auto_recover = true
```

Whisper runs on the CPU by default. On a machine with an NVIDIA GPU set
`whisper_device = "cuda"` and use `medium` or `large-v3` for better accuracy.
The `small` model keeps up with a meeting in real time on a recent laptop.

## How it works

```
mic ──────┐                       ┌─ transcript.md (live)
          ├─ 30 s chunks ─ Whisper ┤
speakers ─┘   (local)              └─ Claude ─ notes.md
(loopback)
```

* `meetnotes/audio.py` captures both sources at 16 kHz and writes the stereo WAV.
* `meetnotes/transcribe.py` runs faster-whisper on each chunk, per channel, so
  your lines are labelled `Me:` and everyone else `Others:`.
* `meetnotes/detect.py` classifies window titles to find live meetings.
* `meetnotes/summarize.py` sends the transcript to Claude with a JSON schema
  for the notes, streaming the response so long meetings do not time out, and
  server-side fallbacks enabled so a safety refusal is retried on another model.
* `meetnotes/dsp.py` cuts chunks in pauses and removes mic echo.
* `meetnotes/safeword.py` recognises the spoken safe word.
* `meetnotes/screen.py` records the screen and muxes it with the meeting audio.
* `meetnotes/session.py` ties it together and writes the meeting folder.
* `meetnotes/service.py` runs the background recorder, pause/stop flags,
  start-at-login items and notifications.
* `meetnotes/library.py` lists, searches and gathers past meetings for `ask`.
* `meetnotes/server.py` is the app's HTTP API (local) and HTTPS listener (phone);
  `meetnotes/web/` the single-page app; `meetnotes/desktop.py` the window and
  tray icon; `meetnotes/phone.py` phone-mic recordings; `meetnotes/tls.py` the
  self-signed certificate; `meetnotes/credentials.py` the API key store.
* `windows/` holds the installer and uninstaller.

Only the transcript text leaves your machine, and only to the Anthropic API.
Audio never does. Meeting folders are created readable by your account only.

## Who can tell it is running

* **Other participants: no one.** Nothing joins the call and the meeting apps
  are never contacted. Share a single window rather than your whole screen
  so a terminal or notes folder does not appear in the share.
* **Your own machine's administrators can.** On a managed work computer, IT
  can see installed software, processes and traffic to `api.anthropic.com`.
  This tool does not try to hide from that; check your workplace policy.
* **The law varies.** Finland and many places allow recording a conversation
  you take part in; some jurisdictions require everyone's consent.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests use fake audio sources and a fake Claude client; no hardware, network
or API key is needed.

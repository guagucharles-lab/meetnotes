"""Command line entry point: `meetnotes <command>`."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import platform
import shutil
import sys
import threading
import time
from pathlib import Path

from . import __version__, config as config_mod, live, service
from .config import Config

log = logging.getLogger("meetnotes")


def _setup_logging(verbose: bool, to_file: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    if to_file:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            service.log_path(), maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("faster_whisper", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# setup / diagnostics
# --------------------------------------------------------------------------

def cmd_setup(args, cfg: Config) -> int:
    from .audio import platform_audio_help
    path = args.config or config_mod.config_path()
    if path.exists() and not args.force:
        print(f"Config already exists at {path} (use --force to rewrite).")
    else:
        config_mod.save(cfg, path)
        print(f"Wrote default config to {path}")
    from .session import private_mkdir
    private_mkdir(cfg.notes_root)
    print(f"Notes will be saved under {cfg.notes_root} (readable only by your account)\n")
    print("Audio setup for this machine:\n" + platform_audio_help() + "\n")
    print("Next: `meetnotes check-audio` with a video playing in your headphones,")
    print("then `meetnotes start` to run it in the background.")
    return 0


def cmd_devices(args, cfg: Config) -> int:
    from .audio import (list_input_devices, auto_system_device, default_pulse_monitor,
                        wasapi_loopback_available)
    devices = list_input_devices()
    if not devices:
        print("No input devices found (is PortAudio installed? see README).")
        return 1
    auto_sys = auto_system_device(devices)
    print(f"{'idx':>3}  {'ch':>2}  {'rate':>6}  name  [host api]")
    for d in devices:
        tag = "  <- auto system audio" if d.index == auto_sys else ""
        print(f"{d.index:>3}  {d.inputs:>2}  {int(d.rate):>6}  {d.name}  [{d.hostapi}]{tag}")
    if platform.system() == "Linux":
        mon = default_pulse_monitor()
        print(f"\nPulse/PipeWire monitor: {mon or 'not available (install pulseaudio-utils)'}")
    if wasapi_loopback_available():
        import soundcard as sc
        print(f"\nWASAPI loopback (used automatically): {sc.default_speaker().name}")
    print("\nSet mic_device / system_device in the config to an index or a name substring.")
    return 0


def cmd_check_audio(args, cfg: Config) -> int:
    """Live level meters for both sides, then a verdict."""
    from .audio import build_sources
    from .library import audio_verdict, level, meter
    mic, system, notes = build_sources(cfg.mic_device, cfg.system_device)
    for n in notes:
        print(n)
    print(f"mic:    {mic.name if mic else 'disabled'}")
    print(f"others: {system.name if system else 'none'}")
    print(f"\nFor {args.seconds} s: play a video in your headphones and say something.\n")
    for src in (mic, system):
        if src:
            src.start()
    mic_peak = sys_peak = 0.0
    try:
        for _ in range(args.seconds):
            time.sleep(1)
            lm = level(mic.take()) if mic else 0.0
            ls = level(system.take()) if system else 0.0
            mic_peak, sys_peak = max(mic_peak, lm), max(sys_peak, ls)
            print(f"  me {meter(lm)}   others {meter(ls)}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        for src in (mic, system):
            if src:
                src.stop()
    print()
    for line in audio_verdict(mic_peak, sys_peak, system is not None):
        print(line)
    return 0 if mic_peak >= 0.01 and (system is None or sys_peak >= 0.005) else 1


def cmd_doctor(args, cfg: Config) -> int:
    ok = True
    print(f"meetnotes {__version__} on {platform.system()} {platform.release()}, Python {platform.python_version()}")
    print(f"config: {config_mod.config_path()} ({'found' if config_mod.config_path().exists() else 'defaults'})")

    audio_ok = True
    try:
        import sounddevice  # noqa: F401
        print("audio:  PortAudio OK")
    except Exception as exc:
        ok = audio_ok = False
        print(f"audio:  FAIL - {exc} (install PortAudio, see README)")

    if audio_ok:
        try:
            from .audio import build_sources
            mic, system, notes = build_sources(cfg.mic_device, cfg.system_device)
            print(f"mic:    {'OK' if mic else 'disabled'}")
            print(f"system: {'OK (' + system.name + ')' if system else 'MISSING - other participants will not be transcribed'}")
            for n in notes:
                print("        " + n)
        except Exception as exc:
            ok = False
            print(f"devices: FAIL - {exc}")

    try:
        import faster_whisper  # noqa: F401
        print(f"whisper: faster-whisper OK (model '{cfg.whisper_model}' downloads on first run)")
    except Exception as exc:
        ok = False
        print(f"whisper: FAIL - {exc}")

    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print("claude: credentials found in environment")
    elif shutil.which("ant"):
        print("claude: no env key; will use the `ant auth login` profile if one exists")
    else:
        ok = False
        print("claude: FAIL - set ANTHROPIC_API_KEY (or run `ant auth login`)")

    if platform.system() == "Darwin":
        print("watch:  needs Accessibility permission for Terminal (System Settings > Privacy)")
    elif platform.system() == "Linux":
        tool = "wmctrl" if shutil.which("wmctrl") else ("xdotool" if shutil.which("xdotool") else None)
        print(f"watch:  window titles via {tool}" if tool else "watch:  install wmctrl or xdotool for auto-detect")
    print(f"service: {'running' if service.running_pid() else 'not running'}, "
          f"start at login {'on' if service.autostart_enabled() else 'off'}")
    print("\nAll good." if ok else "\nFix the FAIL lines above.")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def _run_session(cfg: Config, title: str, platform_name: str, stop_when, show: bool = False,
                 calendar=None) -> str:
    """Record until stop_when(session) returns a reason. Returns that reason."""
    from .session import MeetingSession
    on_segment = None
    if show:
        from .transcribe import format_line
        on_segment = lambda seg: print(format_line(seg), flush=True)
    event = None
    if calendar is not None:
        try:
            event = calendar.current()
        except Exception as exc:
            log.warning("calendar lookup failed: %s", exc)
    session = MeetingSession(cfg, title, platform_name, is_paused=service.is_paused,
                             on_segment=on_segment, event=event)
    try:
        session.start()
    except Exception as exc:  # e.g. no microphone: report it, keep the service alive
        log.error("Could not start recording: %s", exc)
        import shutil as _shutil
        _shutil.rmtree(session.dir, ignore_errors=True)
        _run_session.last_error = (time.monotonic(), str(exc))
        return "error"
    for w in session.warnings:
        log.warning(w)
    log.info("● Recording '%s' (%s)", session.title, platform_name or "manual")
    live.set_current(session)
    reason = "interrupted"
    try:
        while True:
            service.write_status(state="paused" if session.paused else "recording",
                                 meeting=session.title, platform=platform_name,
                                 folder=str(session.dir), since=session.started_at.isoformat(timespec="seconds"),
                                 backlog=round(session.backlog), notice=getattr(session, "notice", ""))
            r = "safe word" if session.safe_word_hit else stop_when(session)
            if r:
                reason = r
                break
            time.sleep(1)
    except KeyboardInterrupt:
        reason = "interrupted"
    log.info("Meeting over (%s). Finishing transcription and writing notes...", reason)
    result: dict = {}

    def finish():
        try:
            result["notes"] = session.stop()
        except Exception as exc:
            result["error"] = exc

    import threading as _threading
    worker = _threading.Thread(target=finish, name="finish", daemon=True)
    worker.start()
    while worker.is_alive():  # tell the app what is happening and how much is left
        service.write_status(state="finishing", phase=session.phase, meeting=session.title,
                             folder=str(session.dir), backlog=round(session.backlog),
                             notice=getattr(session, "notice", ""))
        worker.join(1.0)
    live.clear(session)
    service.resume()  # a pause lasts until the end of the meeting
    if "error" in result:
        log.error("Notes failed: %s. Transcript kept; retry from the app or with: meetnotes summarize \"%s\"",
                  result["error"], session.dir)
        return reason
    notes = result.get("notes")
    if notes:
        log.info("✔ Notes: %s", notes)
        if cfg.notify:
            service.notify("Meeting notes ready", session.title)
    return reason


_run_session.last_error = (0.0, "")


def recent_error(max_age: float = 120.0) -> str:
    when, text = _run_session.last_error
    return text if text and time.monotonic() - when < max_age else ""


def cmd_record(args, cfg: Config) -> int:
    if args.screen:
        cfg.record_screen = True
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    print("Recording. Ctrl-C (or `meetnotes stop` from another terminal) ends it and writes the notes.")

    def stop_when(_session):
        if service.stop_requested():
            service.clear_stop()
            return "stop"
        if deadline is not None and time.monotonic() >= deadline:
            return "time limit"
        return None

    _run_session(cfg, args.title or "Meeting", args.platform or "", stop_when, show=args.show)
    return 0


def is_ignored(title: str, patterns: list[str]) -> bool:
    low = title.lower()
    return any(p.strip() and p.strip().lower() in low for p in patterns)


def _default_mic():
    from .detect import browser_mic_in_use
    return browser_mic_in_use()


def pick_meeting(allowed, detect_fn, mic_fn=_default_mic):
    """What should be recorded right now: a manual recording asked for from
    the app/phone, else a meeting window (if automatic recording is on)."""
    from .detect import Meeting
    forced = service.force_title()
    if forced is not None:
        return Meeting("manual", forced)
    if not service.auto_enabled():
        return None
    meeting = detect_fn(allowed)
    # A Meet tab left open after the call ("You left the meeting") is not a meeting.
    if meeting is not None and meeting.platform == "meet" and mic_fn() is False:
        return None
    return meeting


MEET_MIC_GRACE_S = 90  # browser off the microphone this long during a Meet call = you left


def make_stop_when(cfg: Config, meeting, allowed, detect_fn, clock=time.monotonic, mic_fn=_default_mic):
    """Decides when the session for `meeting` is over. Returns a reason or None."""
    state = {"last_check": clock(), "missing_since": None, "mic_off_since": None}

    def stop_when(session):
        if service.stop_requested():
            return "stop"
        if service.take_skip():
            return "stopped by user"
        if meeting.platform == "manual":
            return None if service.force_title() is not None else "stopped by user"
        if cfg.idle_stop_minutes and session.idle_for() >= cfg.idle_stop_minutes * 60:
            return "nobody spoke"
        now = clock()
        if now - state["last_check"] < cfg.poll_seconds:
            return None
        state["last_check"] = now
        if detect_fn(allowed) is not None:
            state["missing_since"] = None
            if meeting.platform == "meet":
                if mic_fn() is False:
                    state["mic_off_since"] = state["mic_off_since"] or now
                    if now - state["mic_off_since"] >= max(cfg.end_grace_seconds, MEET_MIC_GRACE_S):
                        return "left the meeting"
                else:
                    state["mic_off_since"] = None
            return None
        if state["missing_since"] is None:
            state["missing_since"] = now
        if now - state["missing_since"] >= cfg.end_grace_seconds:
            return "window closed"
        return None

    return stop_when


def _start_calendar(cfg: Config):
    from .cal import Calendar
    cal = Calendar(list(cfg.calendar_urls), service.state_dir() / "calendar.json")
    cal.start_background()
    return cal


def _start_app(cfg: Config, config_path, calendar=None):
    if not cfg.app_server:
        return None
    try:
        from .server import App
        app = App(cfg, config_path)
        app.calendar = calendar
        app.start()
        return app
    except OSError as exc:
        log.warning("App not started (port %s busy?): %s", cfg.app_port, exc)
    except Exception as exc:
        log.warning("App not started: %s", exc)
    return None


def cmd_watch(args, cfg: Config) -> int:
    from .detect import detect
    if args.background:
        _setup_logging(args.verbose, to_file=True)
    if service.running_pid() and service.running_pid() != os.getpid():
        log.error("Another meetnotes watcher is already running (pid %s).", service.running_pid())
        return 1
    service.write_pid()
    service.clear_stop()
    service.take_skip()
    allowed = set(args.platforms or cfg.watch_platforms)
    log.info("Watching for meetings (%s).", ", ".join(sorted(allowed)))
    calendar = _start_calendar(cfg)
    app = _start_app(cfg, args.config, calendar)

    if cfg.auto_recover:
        def recover():
            try:
                from .session import recover_unfinished
                recover_unfinished(cfg)
            except Exception as exc:
                log.warning("recovery skipped: %s", exc)
        threading.Thread(target=recover, daemon=True, name="recover").start()

    # Check once per version that speech recognition works on this computer
    # (downloads the model on first run), so a problem shows up in the app
    # before a meeting and not in the middle of one.
    def speech_check():
        time.sleep(15)
        from . import live
        from .whisperproc import needs_test, test_in_background
        if needs_test(cfg) and live.current() is None:
            test_in_background(cfg, close_after=True)
    threading.Thread(target=speech_check, daemon=True, name="speech-check").start()

    service.clear_restart()
    if cfg.update_check:
        def update_check():
            from .update import check
            while True:
                try:
                    check(cfg.update_url)
                except Exception as exc:
                    log.debug("update check: %s", exc)
                time.sleep(6 * 3600)
        threading.Thread(target=update_check, daemon=True, name="update-check").start()

    seen_key, seen_count, suppressed = None, 0, None
    restart = False
    try:
        while not service.stop_requested():
            if service.restart_requested():
                restart = True
                break
            if suppressed is None or recent_error():
                service.write_status(state="watching" if service.auto_enabled() else "auto-off",
                                     error=recent_error())
            meeting = pick_meeting(allowed, detect)
            if meeting is None:
                seen_count, suppressed = 0, None
                service.take_skip()  # nothing to skip
                _sleep_or_wake(cfg.poll_seconds)
                continue
            key = (meeting.platform, meeting.title)
            if key == suppressed and meeting.platform != "manual":
                _sleep_or_wake(cfg.poll_seconds)
                continue
            if meeting.platform != "manual":
                if is_ignored(meeting.title, cfg.ignore_titles):
                    log.info("Ignoring '%s' (matches ignore_titles).", meeting.title)
                    suppressed = key
                    continue
                seen_count = seen_count + 1 if key == seen_key else 1
                seen_key = key
                if seen_count < 2:  # two sightings in a row, so a flicker doesn't start a session
                    _sleep_or_wake(cfg.poll_seconds)
                    continue

            log.info("Detected %s: %s", meeting.pretty_platform, meeting.title)
            stop_when = make_stop_when(cfg, meeting, allowed, detect)
            reason = _run_session(cfg, meeting.title, meeting.pretty_platform, stop_when, calendar=calendar)
            service.clear_force()  # a manual recording ends with its session
            if service.restart_requested():
                restart = True
                break
            seen_count = 0
            if reason in ("nobody spoke", "safe word", "stopped by user", "error", "left the meeting") and meeting.platform != "manual":
                suppressed = key  # don't restart for the same meeting until its window goes away
            if reason == "safe word":
                service.write_status(state="stopped-safe-word", meeting=meeting.title)
            log.info("Back to watching.")
    except KeyboardInterrupt:
        pass
    finally:
        if app is not None:
            app.stop()
        calendar.stop()
        service.clear_stop()
        service.clear_status()
        if restart:
            log.info("Restarting with the new version.")
            service.restart_background(args.config)
        else:
            service.clear_pid()
        log.info("Watcher stopped.")
    return 0


def _sleep_or_wake(seconds: float) -> None:
    """Sleep, but react within half a second to the app pressing 'Record now'."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if service.force_title() is not None or service.stop_requested():
            return
        time.sleep(0.5)


def cmd_update(args, cfg: Config) -> int:
    from .update import apply, check
    st = check(cfg.update_url, force=True)
    if not st["configured"]:
        print("No update address configured (update_url).")
        return 1
    if st["error"]:
        print(f"Could not check: {st['error']}")
        return 1
    print(f"Installed {st['current']}, latest {st['latest']}" + (f": {st['notes']}" if st["notes"] else ""))
    if not st["available"]:
        print("Up to date.")
        return 0
    if args.check:
        return 0
    print("Downloading and installing...")
    st = apply(cfg.update_url)
    if st["progress"] == "failed":
        print(f"Update failed: {st['progress_error']}")
        return 1
    print(f"Installed {st['installed']}. The background service restarts after the current meeting.")
    return 0


def cmd_app(args, cfg: Config) -> int:
    from .desktop import main as desktop_main
    return desktop_main(args.config)


# --------------------------------------------------------------------------
# background service
# --------------------------------------------------------------------------

def cmd_start(args, cfg: Config) -> int:
    pid, started = service.start_background(args.config)
    if not started:
        print(f"Already running in the background (pid {pid}).")
        return 0
    time.sleep(2)
    if service.running_pid():
        print("Running in the background. It records Zoom, Teams and Meet meetings on its own.")
        print("No window, no icon, no pop-ups. `meetnotes status` shows what it is doing.")
    else:
        print(f"It did not stay up. See the log: {service.log_path()}")
        return 1
    return 0


def cmd_stop(args, cfg: Config) -> int:
    if not service.running_pid():
        print("Not running.")
        return 0
    st = service.read_status()
    if st.get("state") in ("recording", "paused"):
        print(f"Finishing the meeting in progress ('{st.get('meeting')}') and writing its notes...")
    result = service.stop_background()
    print({"stopped": "Stopped.", "killed": "Stopped (forced; check the log).",
           "not-running": "Not running."}[result])
    return 0


def cmd_status(args, cfg: Config) -> int:
    pid = service.running_pid()
    st = service.read_status() if pid else {}
    if not pid:
        print("Background service: not running   (start it with `meetnotes start`)")
    else:
        state = st.get("state", "starting")
        line = {"watching": "watching, no meeting right now",
                "recording": f"RECORDING '{st.get('meeting')}' since {st.get('since', '?')[11:16]}",
                "paused": f"PAUSED in '{st.get('meeting')}' (resume with `meetnotes resume`)",
                "writing-notes": f"writing notes for '{st.get('meeting')}'",
                "finishing": f"finishing '{st.get('meeting')}' ({st.get('phase')}, "
                             f"{round((st.get('backlog') or 0) / 60)} min of audio left)",
                "stopped-safe-word": f"stopped by safe word in '{st.get('meeting')}'; "
                                     "starts again with the next meeting",
                "auto-off": "running, automatic recording switched off in the app"}.get(state, state)
        print(f"Background service: {line}  (pid {pid})")
    if service.is_paused():
        print("Pause flag is set: nothing is being recorded until `meetnotes resume` or the meeting ends.")
    print(f"Start at login: {'on' if service.autostart_enabled() else 'off'}")
    print(f"Notes folder:   {cfg.notes_root}")
    print(f"Log:            {service.log_path()}")
    return 0


def cmd_pause(args, cfg: Config) -> int:
    service.pause()
    print("Paused. Audio is discarded (not recorded, not transcribed) until `meetnotes resume`")
    print("or until this meeting ends.")
    return 0


def cmd_resume(args, cfg: Config) -> int:
    service.resume()
    print("Resumed.")
    return 0


def cmd_autostart(args, cfg: Config) -> int:
    if args.state == "on":
        path = service.enable_autostart()
        print(f"meetnotes will start in the background when you log in ({path}).")
    elif args.state == "off":
        path = service.disable_autostart()
        print(f"Start at login removed ({path})." if path else "Start at login was not on.")
    else:
        print(f"Start at login: {'on' if service.autostart_enabled() else 'off'}")
    return 0


# --------------------------------------------------------------------------
# reading notes
# --------------------------------------------------------------------------

def cmd_summarize(args, cfg: Config) -> int:
    from .session import resummarize
    out = resummarize(Path(args.target), cfg)
    print(f"✔ Notes: {out}")
    return 0


def cmd_recover(args, cfg: Config) -> int:
    from .session import recover_unfinished
    done = recover_unfinished(cfg)
    print("\n".join(f"✔ {p}" for p in done) if done else "Nothing to recover.")
    return 0


def cmd_list(args, cfg: Config) -> int:
    from .library import meetings
    items = meetings(cfg.notes_root)[: args.limit]
    if not items:
        print(f"No meetings yet ({cfg.notes_root}).")
        return 0
    for m in items:
        dur = m.meta.get("duration_s")
        dur_s = f"{int(dur // 60)} min" if dur else ""
        print(f"{m.path.name:<48} {m.meta.get('status', '?'):<12} {dur_s:<8} {m.title}")
    return 0


def cmd_search(args, cfg: Config) -> int:
    from .library import search
    hits = search(cfg.notes_root, " ".join(args.term))
    if not hits:
        print("No matches.")
        return 1
    for m, name, line in hits:
        print(f"{m.path.name} [{name}]  {line}")
    return 0


def cmd_ask(args, cfg: Config) -> int:
    from .library import ask_context
    from .summarize import ask
    docs = ask_context(cfg.notes_root, last=args.last)
    if not docs:
        print("No meetings with notes yet.")
        return 1
    ask(" ".join(args.question), docs, cfg.claude_model, write=sys.stdout.write)
    print()
    return 0


def cmd_open(args, cfg: Config) -> int:
    from .library import meetings
    items = meetings(cfg.notes_root)
    if not items:
        print("No meetings yet.")
        return 1
    target = items[0].path / "notes.md"
    if not target.exists():
        target = items[0].path
    if platform.system() == "Windows":
        os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        import subprocess
        subprocess.run(["open" if platform.system() == "Darwin" else "xdg-open", str(target)])
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meetnotes",
                                description="Private meeting notes for Zoom, Teams and Google Meet. "
                                            "Records on your machine; nothing joins the call.")
    p.add_argument("--version", action="version", version=f"meetnotes {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", type=Path, help="alternate config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        return s

    s = add("setup", cmd_setup, "write the default config and show audio setup steps")
    s.add_argument("--force", action="store_true")
    add("devices", cmd_devices, "list audio input devices")
    s = add("check-audio", cmd_check_audio, "live level meters: is your voice and the other side captured?")
    s.add_argument("--seconds", type=int, default=10)
    add("doctor", cmd_doctor, "check audio, whisper, Claude credentials and the service")

    add("start", cmd_start, "run the auto-recorder in the background (no window)")
    add("stop", cmd_stop, "stop the background recorder (finishes a meeting in progress first)")
    add("status", cmd_status, "is it running, recording or paused?")
    add("pause", cmd_pause, "stop recording until resume or the end of this meeting")
    add("resume", cmd_resume, "continue recording after pause")
    s = add("autostart", cmd_autostart, "start the background recorder when you log in")
    s.add_argument("state", nargs="?", choices=["on", "off"])

    add("app", cmd_app, "open the desktop app window (starts the background service if needed)")
    s = add("update", cmd_update, "install the latest version (small download, settings and notes untouched)")
    s.add_argument("--check", action="store_true", help="only report whether an update exists")
    s = add("watch", cmd_watch, "auto-record in this terminal whenever a meeting is on screen")
    s.add_argument("--platforms", nargs="*", choices=["zoom", "teams", "meet"])
    s.add_argument("--background", action="store_true", help=argparse.SUPPRESS)

    s = add("record", cmd_record, "record now until Ctrl-C, `meetnotes stop` or --minutes")
    s.add_argument("--title", help="meeting title for the folder and notes")
    s.add_argument("--platform", help="zoom | teams | meet | anything")
    s.add_argument("--minutes", type=float, help="stop automatically after this many minutes")
    s.add_argument("--show", action="store_true", help="print the transcript live in this terminal")
    s.add_argument("--screen", action="store_true", help="also record the screen (screen.mp4)")

    s = add("summarize", cmd_summarize, "(re)write notes for a meeting folder, transcript.md or audio.wav")
    s.add_argument("target")
    add("recover", cmd_recover, "write notes for sessions cut off by a crash or shutdown")
    s = add("list", cmd_list, "list recorded meetings")
    s.add_argument("--limit", type=int, default=20)
    s = add("search", cmd_search, "find a word in all notes and transcripts")
    s.add_argument("term", nargs="+")
    s = add("ask", cmd_ask, "ask Claude a question about your past meetings")
    s.add_argument("question", nargs="+")
    s.add_argument("--last", type=int, default=20, help="how many recent meetings to include")
    add("open", cmd_open, "open the most recent notes")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    cfg = config_mod.load(args.config)
    try:
        return args.fn(args, cfg)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if args.verbose:
            raise
        log.error("error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

import plistlib

from meetnotes import service
from meetnotes.audio import pick_sink
from meetnotes.cli import build_parser, is_ignored
from meetnotes.library import ask_context, audio_verdict, meetings, search


def test_flags_and_status():
    assert not service.is_paused()
    service.pause()
    assert service.is_paused()
    service.resume()
    assert not service.is_paused()
    service.request_stop()
    assert service.stop_requested()
    service.clear_stop()
    assert not service.stop_requested()
    service.write_status(state="recording", meeting="Standup")
    assert service.read_status()["meeting"] == "Standup"
    assert service.running_pid() is None


def test_running_pid_detects_own_process():
    service.write_pid()  # pytest's cmdline doesn't contain "meetnotes" unless run from the repo
    pid = service.running_pid()
    assert pid is None or pid > 0
    service.clear_pid()
    assert service.running_pid() is None


def test_autostart_specs(tmp_path, monkeypatch):
    cmd = ["/usr/bin/python3", "-m", "meetnotes.cli", "watch", "--background"]
    path, body = service.autostart_spec("Darwin", tmp_path, cmd)
    assert path.name == "com.meetnotes.watch.plist"
    plist = plistlib.loads(body)
    assert plist["ProgramArguments"] == cmd and plist["RunAtLoad"] is True

    path, body = service.autostart_spec("Linux", tmp_path, ["/opt/my py/python", "-m", "meetnotes.cli"])
    text = body.decode()
    assert path == tmp_path / ".config/autostart/meetnotes.desktop"
    assert 'Exec="/opt/my py/python" -m meetnotes.cli' in text and "NoDisplay=true" in text

    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    path, body = service.autostart_spec("Windows", tmp_path, [r"C:\Py\pythonw.exe", "-m", "meetnotes.cli"])
    assert path.name == "meetnotes.vbs" and "Startup" in str(path)
    assert body.decode().startswith('CreateObject("WScript.Shell").Run """C:\\Py\\pythonw.exe"" ""-m""')
    assert ", 0, False" in body.decode()  # 0 = hidden window


def test_pick_sink_prefers_the_playing_output():
    sinks = ("1\talsa_output.pci.analog-stereo\tmodule-alsa-card.c\ts16le 2ch 48000Hz\tSUSPENDED\n"
             "2\tbluez_output.AirPods.1\tmodule-bluez5-device.c\ts16le 2ch 48000Hz\tRUNNING\n")
    assert pick_sink("alsa_output.pci.analog-stereo", sinks) == "bluez_output.AirPods.1"
    assert pick_sink("bluez_output.AirPods.1", sinks) == "bluez_output.AirPods.1"
    assert pick_sink("alsa_output.pci.analog-stereo", sinks.replace("RUNNING", "IDLE")) == \
        "alsa_output.pci.analog-stereo"


def test_ignore_titles():
    assert is_ignored("1:1 with HR", ["1:1"]) and not is_ignored("Standup", ["1:1", ""])


def test_cli_parses_every_command():
    p = build_parser()
    for argv in (["start"], ["stop"], ["status"], ["pause"], ["resume"], ["autostart", "on"],
                 ["check-audio", "--seconds", "3"], ["record", "--show", "--minutes", "1"],
                 ["ask", "what", "did", "Sam", "promise?"], ["search", "budget"], ["recover"]):
        assert p.parse_args(argv).fn


def test_library(tmp_path):
    a = tmp_path / "2026-10-28_1000_Standup"
    b = tmp_path / "2026-10-29_1500_Planning"
    for d, notes in ((a, "Budget is 120k"), (b, "Launch in November")):
        d.mkdir()
        (d / "notes.md").write_text(f"# x\n{notes}\n")
        (d / "transcript.md").write_text("[00:00:01] Others: the budget looks fine\n")
    assert [m.path for m in meetings(tmp_path)] == [b, a]
    hits = search(tmp_path, "BUDGET")
    assert len(hits) == 3
    docs = ask_context(tmp_path, last=5)
    assert len(docs) == 2 and "Launch in November" in docs[0][1] and "Planning" in docs[0][0]
    small = ask_context(tmp_path, last=5, budget_chars=60)
    assert all("Others:" not in text for _, text in small)  # transcripts dropped over budget


def test_audio_verdict():
    assert "OK" in audio_verdict(0.2, 0.2, True)[1]
    assert "silent" in audio_verdict(0.2, 0.0, True)[1]
    assert "silent" in audio_verdict(0.0, 0.2, True)[0]


def test_pick_meeting_and_manual_stop(tmp_path):
    from meetnotes.cli import make_stop_when, pick_meeting
    from meetnotes.config import Config
    from meetnotes.detect import Meeting

    zoom = Meeting("zoom", "Standup")
    assert pick_meeting({"zoom"}, lambda a: zoom) == zoom
    service.set_auto(False)
    assert pick_meeting({"zoom"}, lambda a: zoom) is None          # auto off
    service.force_record("Asiakas")
    assert pick_meeting({"zoom"}, lambda a: zoom) == Meeting("manual", "Asiakas")  # manual wins
    service.set_auto(True)

    class S:
        def idle_for(self):
            return 0

    stop = make_stop_when(Config(), Meeting("manual", "Asiakas"), {"zoom"}, lambda a: None)
    assert stop(S()) is None               # still forced
    service.clear_force()
    assert stop(S()) == "stopped by user"

    t = {"now": 0.0}
    stop = make_stop_when(Config(poll_seconds=5, end_grace_seconds=45), zoom, {"zoom"},
                          lambda a: None, clock=lambda: t["now"])
    service.request_skip()
    assert stop(S()) == "stopped by user"  # "Lopeta nauhoitus" in the app
    for t["now"] in (5, 10, 30):
        assert stop(S()) is None
    t["now"] = 60
    assert stop(S()) == "window closed"


def test_left_meet_tab_ends_recording(tmp_path):
    """After leaving a Google Meet call the tab still reads "Meet - abc-defg-hij";
    the browser letting go of the microphone is what tells the call is over."""
    from meetnotes.cli import make_stop_when, pick_meeting
    from meetnotes.config import Config
    from meetnotes.detect import Meeting

    meet = Meeting("meet", "abc-defg-hij")
    mic = {"on": True}
    assert pick_meeting({"meet"}, lambda a: meet, mic_fn=lambda: mic["on"]) == meet
    assert pick_meeting({"meet"}, lambda a: meet, mic_fn=lambda: None) == meet   # can't tell: trust the tab
    mic["on"] = False
    assert pick_meeting({"meet"}, lambda a: meet, mic_fn=lambda: mic["on"]) is None  # left tab: don't start again

    class S:
        def idle_for(self):
            return 0
    t = {"now": 0.0}
    mic["on"] = True
    stop = make_stop_when(Config(poll_seconds=5, end_grace_seconds=45), meet, {"meet"},
                          lambda a: meet, clock=lambda: t["now"], mic_fn=lambda: mic["on"])
    t["now"] = 10
    assert stop(S()) is None
    mic["on"] = False             # left the call, tab still open
    t["now"] = 20
    assert stop(S()) is None
    mic["on"] = True              # a blip (e.g. switching microphone) resets the wait
    t["now"] = 30
    assert stop(S()) is None
    mic["on"] = False
    for t["now"] in (40, 80, 125):
        assert stop(S()) is None
    t["now"] = 131
    assert stop(S()) == "left the meeting"


def test_recording_that_cannot_start_does_not_kill_the_service(tmp_path, monkeypatch):
    from meetnotes import cli, session as session_mod
    from meetnotes.config import Config

    class Broken(session_mod.MeetingSession):
        def start(self):
            raise RuntimeError("PortAudio library not found")

    monkeypatch.setattr(session_mod, "MeetingSession", Broken)
    cfg = Config(notes_dir=str(tmp_path / "notes"))
    assert cli._run_session(cfg, "Testi", "Manual", lambda s: None) == "error"
    assert cli.recent_error() == "PortAudio library not found"
    assert not any((tmp_path / "notes").iterdir())  # no empty folder left behind


def test_no_console_streams_are_replaced():
    import subprocess, sys
    code = ("import sys; sys.stdout = None; sys.stderr = None\n"
            "import meetnotes, os\n"
            "from tqdm import tqdm\n"
            "for _ in tqdm(range(3)): pass\n"
            "open(os.environ['OUT'], 'w').write(os.environ.get('HF_HUB_DISABLE_PROGRESS_BARS', ''))\n")
    import tempfile, os
    out = tempfile.mktemp()
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "OUT": out}, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert r.returncode == 0 and open(out).read() == "1"


def test_finishing_reports_progress(tmp_path, monkeypatch):
    """While a long meeting is still being transcribed, the app sees how much is left."""
    import threading
    from meetnotes import cli, session as session_mod
    from meetnotes.config import Config

    release = threading.Event()
    seen = []

    class Slow:
        def __init__(self, cfg, title, platform, **kw):
            self.title, self.dir, self.paused, self.safe_word_hit, self.warnings = title, tmp_path, False, False, []
            self.started_at = __import__("datetime").datetime.now()
            self.phase, self.backlog = "recording", 0

        def start(self):
            pass

        def stop(self):
            self.phase, self.backlog = "transcribing", 300
            release.wait(5)
            return None

    monkeypatch.setattr(session_mod, "MeetingSession", Slow)
    real_write = service.write_status
    monkeypatch.setattr(service, "write_status", lambda **kw: (seen.append(kw), real_write(**kw)))
    threading.Timer(1.5, release.set).start()
    cli._run_session(Config(notes_dir=str(tmp_path)), "Testi", "Manual", lambda s: "stopped by user")
    finishing = [kw for kw in seen if kw.get("state") == "finishing"]
    assert finishing and finishing[-1]["phase"] == "transcribing" and finishing[-1]["backlog"] == 300

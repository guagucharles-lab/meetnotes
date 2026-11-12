"""Background service plumbing: control flags, pid/status files, detached
start/stop, start-at-login, and optional desktop notifications.

The background watcher and the `meetnotes pause/resume/stop/status` commands
talk to each other only through small files in the state directory, so it
works the same on macOS, Windows and Linux.
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import state_dir, write_json

LABEL = "com.meetnotes.watch"


# --------------------------------------------------------------------------
# Control flags
# --------------------------------------------------------------------------

def _flag(name: str) -> Path:
    return state_dir() / name


def pause() -> None:
    _flag("paused").write_text(datetime.now().isoformat(timespec="seconds"))


def resume() -> None:
    _flag("paused").unlink(missing_ok=True)


def is_paused() -> bool:
    return _flag("paused").exists()


# Recording now (manual), started from the app or the phone. The file holds the title.
def force_record(title: str = "") -> None:
    _flag("force").write_text(title or "Meeting", encoding="utf-8")


def clear_force() -> None:
    _flag("force").unlink(missing_ok=True)


def force_title() -> str | None:
    try:
        return _flag("force").read_text(encoding="utf-8").strip() or "Meeting"
    except FileNotFoundError:
        return None


# End the meeting being recorded now (and don't restart it for that window).
def request_skip() -> None:
    _flag("skip").write_text(datetime.now().isoformat(timespec="seconds"))


def take_skip() -> bool:
    try:
        _flag("skip").unlink()
        return True
    except FileNotFoundError:
        return False


# Automatic recording on/off without stopping the service (so the phone can
# switch it back on).
def set_auto(enabled: bool) -> None:
    if enabled:
        _flag("auto_off").unlink(missing_ok=True)
    else:
        _flag("auto_off").write_text(datetime.now().isoformat(timespec="seconds"))


def auto_enabled() -> bool:
    return not _flag("auto_off").exists()


# The update installed a new version: restart the service when no meeting is running.
def request_restart() -> None:
    _flag("restart").write_text(datetime.now().isoformat(timespec="seconds"))


def restart_requested() -> bool:
    return _flag("restart").exists()


def clear_restart() -> None:
    _flag("restart").unlink(missing_ok=True)


def restart_background(config: Path | None = None) -> int:
    """Called by the watcher itself at the end of its run: release the pid
    file, start a fresh watcher (which loads the new code) and let the old
    process exit."""
    clear_restart()
    clear_pid()
    pid, _ = start_background(config)
    return pid


def request_stop() -> None:
    _flag("stop").write_text(datetime.now().isoformat(timespec="seconds"))


def stop_requested() -> bool:
    return _flag("stop").exists()


def clear_stop() -> None:
    _flag("stop").unlink(missing_ok=True)


def write_status(**data) -> None:
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    data["pid"] = os.getpid()
    try:
        write_json(_flag("status.json"), data)
    except OSError:
        pass  # a status update is never worth stopping a recording for


def read_status() -> dict:
    try:
        return json.loads(_flag("status.json").read_text())
    except Exception:
        return {}


def clear_status() -> None:
    _flag("status.json").unlink(missing_ok=True)


def log_path() -> Path:
    return _flag("meetnotes.log")


# --------------------------------------------------------------------------
# PID file / single instance
# --------------------------------------------------------------------------

def write_pid() -> None:
    _flag("watch.pid").write_text(str(os.getpid()))


def clear_pid() -> None:
    try:
        if int(_flag("watch.pid").read_text()) == os.getpid():
            _flag("watch.pid").unlink()
    except Exception:
        pass


def running_pid() -> int | None:
    """PID of the background watcher if it is alive."""
    try:
        pid = int(_flag("watch.pid").read_text())
    except Exception:
        return None
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return None
        try:  # guard against the OS reusing the pid for another program
            cmd = " ".join(psutil.Process(pid).cmdline())
            if "meetnotes" not in cmd:
                return None
        except (psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except psutil.NoSuchProcess:
            return None
    except ImportError:
        pass
    return pid


# --------------------------------------------------------------------------
# Detached start / graceful stop
# --------------------------------------------------------------------------

def python_for_background() -> str:
    """On Windows prefer pythonw.exe, which never opens a console window."""
    exe = Path(sys.executable)
    if platform.system() == "Windows":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def watch_command(config: Path | None = None) -> list[str]:
    cmd = [python_for_background(), "-m", "meetnotes.cli"]
    if config is not None:
        cmd += ["--config", str(config)]
    return cmd + ["watch", "--background"]


def start_background(config: Path | None = None) -> tuple[int, bool]:
    """Start the watcher detached from this terminal. Returns (pid, started_now)."""
    pid = running_pid()
    if pid:
        return pid, False
    clear_stop()
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True)
    if platform.system() == "Windows":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(watch_command(config), **kwargs)
    return proc.pid, True


def stop_background(timeout: float = 180.0, progress=None) -> str:
    """Ask the watcher to finish (writing notes for a meeting in progress)
    and wait for it. Returns 'not-running', 'stopped' or 'killed'."""
    pid = running_pid()
    if not pid:
        return "not-running"
    request_stop()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if running_pid() is None:
            clear_stop()
            return "stopped"
        if progress:
            progress()
        time.sleep(1)
    try:
        import psutil
        psutil.Process(pid).terminate()
    except Exception:
        pass
    clear_stop()
    return "killed"


# --------------------------------------------------------------------------
# Start at login
# --------------------------------------------------------------------------

def _desktop_quote(arg: str) -> str:
    if not any(c in arg for c in ' \t"\'\\$`'):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def autostart_spec(system: str | None = None, home: Path | None = None,
                   command: list[str] | None = None) -> tuple[Path, bytes]:
    """(path, file contents) of the login item for this OS."""
    system = system or platform.system()
    home = home or Path.home()
    command = command or watch_command()
    if system == "Darwin":
        path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        body = plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        })
        return path, body
    if system == "Windows":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        path = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "meetnotes.vbs"
        inner = " ".join(f'""{a}""' for a in command)
        # Run(..., 0, False): window style 0 = hidden, don't wait.
        return path, f'CreateObject("WScript.Shell").Run "{inner}", 0, False\r\n'.encode()
    path = home / ".config" / "autostart" / "meetnotes.desktop"
    body = ("[Desktop Entry]\nType=Application\nName=meetnotes\n"
            "Comment=Private meeting notes\n"
            f"Exec={' '.join(_desktop_quote(a) for a in command)}\n"
            "NoDisplay=true\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")
    return path, body.encode()


def enable_autostart() -> Path:
    path, body = autostart_spec()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if platform.system() == "Darwin" and shutil.which("launchctl"):
        subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True)
    return path


def disable_autostart() -> Path | None:
    path, _ = autostart_spec()
    if not path.exists():
        return None
    if platform.system() == "Darwin" and shutil.which("launchctl"):
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
    path.unlink()
    return path


def autostart_enabled() -> bool:
    return autostart_spec()[0].exists()


# --------------------------------------------------------------------------
# Notifications (off by default)
# --------------------------------------------------------------------------

def notify(title: str, body: str) -> None:  # pragma: no cover - OS specific
    system = platform.system()
    try:
        if system == "Darwin":
            esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e",
                            f'display notification "{esc(body)}" with title "{esc(title)}"'],
                           capture_output=True, timeout=5)
        elif system == "Windows":
            esc = lambda t: t.replace("'", "''")
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                f"$n.ShowBalloonTip(8000,'{esc(title)}','{esc(body)}',"
                "[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep 9;$n.Dispose()"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], capture_output=True, timeout=5)
    except Exception:
        pass

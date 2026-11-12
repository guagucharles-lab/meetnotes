"""Best-effort detection of a meeting in progress on this machine.

We never touch the meeting apps. We only look at window titles and process
names that the OS already exposes, which is how we know when to start and
stop recording.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass

PLATFORM_NAMES = {"zoom": "Zoom", "teams": "Microsoft Teams", "meet": "Google Meet",
                  "manual": "Manual", "phone": "Phone"}


@dataclass(frozen=True)
class Meeting:
    platform: str  # zoom | teams | meet
    title: str

    @property
    def pretty_platform(self) -> str:
        return PLATFORM_NAMES.get(self.platform, self.platform)


# Google Meet tab titles look like "Meet – abc-defg-hij" or "Meet - Weekly sync".
_MEET_RE = re.compile(r"^(?:\(\d+\)\s*)?Meet\s*[-–—]\s*(.+?)(?:\s*[-–—]\s*(Google Chrome|Microsoft Edge|Brave|Arc|Firefox|Chromium|Safari|Vivaldi|Opera).*)?$")
_MEET_URL_RE = re.compile(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", re.I)

# Zoom's in-meeting window.
_ZOOM_RE = re.compile(r"^Zoom (Meeting|Webinar)(?:\s*[-–—:]\s*(.+))?$|^(.+?) - Zoom Meeting$", re.I)

# Teams: the meeting pops out as "<Meeting name> | Microsoft Teams"; the main
# window is "Chat | Microsoft Teams", "Calendar | Microsoft Teams", etc.
_TEAMS_RE = re.compile(r"^(.*?)\s*\|\s*Microsoft Teams(?:\s+(?:classic|work or school|personal))?$", re.I)
_TEAMS_NON_MEETING = {
    "", "chat", "teams", "calendar", "activity", "calls", "files", "apps", "onedrive",
    "assignments", "grades", "planner", "tasks", "viva", "copilot", "settings", "notifications",
    "microsoft teams", "general", "posts",
}
_TEAMS_MEETING_HINTS = ("meeting", "call", "webinar", "town hall", "live event")


def classify_window_title(title: str) -> Meeting | None:
    """Pure function: decide whether a window title belongs to a live meeting."""
    t = title.strip()
    if not t:
        return None

    m = _MEET_URL_RE.search(t)
    if m:
        return Meeting("meet", m.group(1))
    m = _MEET_RE.match(t)
    if m:
        name = m.group(1).strip()
        return Meeting("meet", name or "Google Meet")

    m = _ZOOM_RE.match(t)
    if m:
        name = (m.group(2) or m.group(3) or "").strip()
        return Meeting("zoom", name or "Zoom Meeting")

    m = _TEAMS_RE.match(t)
    if m:
        name = m.group(1).strip()
        low = name.lower()
        # "(3) Chat | Microsoft Teams" style unread counters
        low = re.sub(r"^\(\d+\)\s*", "", low)
        if low in _TEAMS_NON_MEETING:
            return None
        # Chat threads with a person's name are not meetings unless a hint shows
        if low.startswith(("chat", "calendar", "activity", "files", "teams and channels")):
            return None
        if any(h in low for h in _TEAMS_MEETING_HINTS) or "," in name or len(name.split()) >= 2:
            return Meeting("teams", re.sub(r"^\(\d+\)\s*", "", name))
        # single word that is not a known tab: likely a meeting pop-out
        return Meeting("teams", name)
    return None


def classify_titles(titles: list[str], allowed: set[str] | None = None) -> Meeting | None:
    """First title that looks like a meeting (filtered by allowed platforms)."""
    for title in titles:
        meeting = classify_window_title(title)
        if meeting and (allowed is None or meeting.platform in allowed):
            return meeting
    return None


# --------------------------------------------------------------------------
# OS window title enumeration
# --------------------------------------------------------------------------

def _windows_titles() -> list[str]:  # pragma: no cover - OS specific
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles: list[str] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(proto(cb), 0)
    return titles


_OSASCRIPT = '''
set out to ""
tell application "System Events"
    repeat with p in (every process whose background only is false)
        try
            repeat with w in (every window of p)
                set out to out & (name of w) & linefeed
            end repeat
        end try
    end repeat
end tell
return out
'''


def _macos_titles() -> list[str]:  # pragma: no cover - OS specific
    try:
        res = subprocess.run(["osascript", "-e", _OSASCRIPT], capture_output=True,
                             text=True, timeout=5)
    except Exception:
        return []
    return [ln for ln in res.stdout.splitlines() if ln.strip()]


def _linux_titles() -> list[str]:  # pragma: no cover - OS specific
    if shutil.which("wmctrl"):
        try:
            res = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5)
            titles = []
            for ln in res.stdout.splitlines():
                parts = ln.split(None, 3)
                if len(parts) == 4:
                    titles.append(parts[3])
            if res.returncode == 0:
                return titles
            # wmctrl needs an EWMH window manager; fall through to xdotool
        except Exception:
            pass
    if shutil.which("xdotool"):
        try:
            ids = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "."],
                                 capture_output=True, text=True, timeout=5).stdout.split()
            titles = []
            for wid in ids[:200]:
                name = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True,
                                      text=True, timeout=2).stdout.strip()
                if name:
                    titles.append(name)
            return titles
        except Exception:
            pass
    return []


def window_titles() -> list[str]:  # pragma: no cover - OS specific
    system = platform.system()
    if system == "Windows":
        return _windows_titles()
    if system == "Darwin":
        return _macos_titles()
    return _linux_titles()


# Process that Zoom spawns only while you are in a meeting (Windows: CptHost.exe).
_ZOOM_MEETING_PROCS = {"cpthost.exe", "cpthost"}


def zoom_meeting_process_running() -> bool:  # pragma: no cover - OS specific
    try:
        import psutil
    except Exception:
        return False
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").lower() in _ZOOM_MEETING_PROCS:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def detect(allowed: set[str] | None = None) -> Meeting | None:  # pragma: no cover - OS specific
    """Return the meeting currently on screen, if any."""
    meeting = classify_titles(window_titles(), allowed)
    if meeting:
        return meeting
    if (allowed is None or "zoom" in allowed) and zoom_meeting_process_running():
        return Meeting("zoom", "Zoom Meeting")
    return None


BROWSER_EXES = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe")
_MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged"


def browser_mic_in_use() -> bool | None:  # pragma: no cover - Windows only
    """Is a web browser using the microphone right now? Windows keeps this
    (it drives the microphone icon in the taskbar). After you leave a Google
    Meet call the tab still says "Meet - abc-defg-hij", but the browser lets
    go of the microphone. None when it can't be told (other systems)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MIC_KEY)
    except OSError:
        return None
    seen = False
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if not name.lower().endswith(BROWSER_EXES):
                continue
            seen = True
            try:
                with winreg.OpenKey(root, name) as k:
                    stop, _ = winreg.QueryValueEx(k, "LastUsedTimeStop")
            except OSError:
                continue
            if stop == 0:
                return True
    return False if seen else None

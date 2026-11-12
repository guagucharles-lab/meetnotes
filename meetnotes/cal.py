"""Calendar (ICS feeds): what meetings are coming, and which one is on now.

Works with the private iCal address of a Google Calendar, a published Outlook
or Microsoft 365 calendar, Apple iCloud public calendars, or any .ics URL.
Read-only, no OAuth, nothing is written back. Events are cached in the state
folder so the app answers instantly and still works when the feed is slow.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("meetnotes")

MEETING_LINK = re.compile(r"https?://(?:[\w.-]*zoom\.us/j/\S+|meet\.google\.com/[a-z-]+|teams\.microsoft\.com/l/meetup-join/\S+|teams\.live\.com/meet/\S+)", re.I)


@dataclass
class Event:
    uid: str
    title: str
    start: str                 # ISO local time "2026-11-14T10:00"
    end: str
    all_day: bool = False
    attendees: list[str] = field(default_factory=list)
    organizer: str = ""
    description: str = ""
    location: str = ""
    link: str = ""             # Zoom / Meet / Teams join link, if any
    calendar: str = ""         # feed name

    def starts(self) -> datetime:
        return datetime.fromisoformat(self.start)

    def ends(self) -> datetime:
        return datetime.fromisoformat(self.end)

    def platform(self) -> str:
        l = (self.link or self.location or "").lower()
        if "zoom" in l:
            return "Zoom"
        if "meet.google" in l:
            return "Google Meet"
        if "teams" in l:
            return "Microsoft Teams"
        return ""


def _display_name(value, default: str = "") -> str:
    """ATTENDEE;CN=Jari Kontio:mailto:jari@... -> 'Jari Kontio', else the address."""
    params = getattr(value, "params", {}) or {}
    cn = str(params.get("CN", "") or "").strip().strip('"')
    if cn and "@" not in cn:
        return cn
    addr = str(value).replace("mailto:", "").replace("MAILTO:", "").strip()
    return cn or addr or default


def _local(dt) -> datetime:
    """Any ICS date/datetime -> naive local datetime."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.replace(tzinfo=None, microsecond=0)
    return datetime(dt.year, dt.month, dt.day)


def parse_ics(text: str, start: datetime, end: datetime, calendar_name: str = "") -> list[Event]:
    """Events (recurrences expanded) between start and end, from ICS text."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(text)
    out = []
    for ev in recurring_ical_events.of(cal, skip_bad_series=True).between(start, end):
        dtstart = ev.get("DTSTART")
        if dtstart is None:
            continue
        s = _local(dtstart.dt)
        dtend = ev.get("DTEND")
        all_day = not isinstance(dtstart.dt, datetime)
        e = _local(dtend.dt) if dtend is not None else (s + timedelta(days=1) if all_day else s + timedelta(hours=1))
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        attendees = ev.get("ATTENDEE", [])
        if not isinstance(attendees, list):
            attendees = [attendees]
        names = []
        for a in attendees:
            params = getattr(a, "params", {}) or {}
            if str(params.get("PARTSTAT", "")).upper() == "DECLINED" and str(params.get("ROLE", "")).upper() != "NON-PARTICIPANT":
                continue
            n = _display_name(a)
            if n and n not in names:
                names.append(n)
        description = str(ev.get("DESCRIPTION", "") or "")
        location = str(ev.get("LOCATION", "") or "")
        url = str(ev.get("URL", "") or "")
        m = MEETING_LINK.search(" ".join((url, location, description)))
        out.append(Event(
            uid=str(ev.get("UID", "")) + "@" + s.isoformat(timespec="minutes"),
            title=str(ev.get("SUMMARY", "") or "(nimetön)").strip(),
            start=s.isoformat(timespec="minutes"), end=e.isoformat(timespec="minutes"), all_day=all_day,
            attendees=names, organizer=_display_name(ev.get("ORGANIZER"), "") if ev.get("ORGANIZER") else "",
            description=description.strip(), location=location.strip(), link=m.group(0) if m else "",
            calendar=calendar_name,
        ))
    out.sort(key=lambda e: e.start)
    return out


def fetch_ics(url: str, timeout: float = 20.0) -> str:
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith(("https://", "http://")):
        raise ValueError("calendar address must start with https://")
    req = urllib.request.Request(url, headers={"User-Agent": "meetnotes/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def agenda_from_description(description: str) -> list[str]:
    """Numbered or bulleted lines in an invite become agenda items."""
    items = []
    for line in description.splitlines():
        m = re.match(r"^\s*(?:\d+[.)]|[-*•–])\s+(.+)$", line)
        if m and len(m.group(1).strip()) > 2:
            items.append(m.group(1).strip())
    return items


class Calendar:
    """Keeps a cache of upcoming events from the configured feeds."""

    def __init__(self, urls: list[str], cache_path: Path, days_ahead: int = 14,
                 refresh_seconds: int = 900, fetch=fetch_ics, clock=time.monotonic) -> None:
        self.urls = [u.strip() for u in urls if u.strip()]
        self.cache_path = cache_path
        self.days_ahead = days_ahead
        self.refresh_seconds = refresh_seconds
        self._fetch = fetch
        self._clock = clock
        self.events: list[Event] = []
        self.error = ""
        self.fetched_at = ""
        self._last = -1e9
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._load_cache()

    # -- cache ---------------------------------------------------------------

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.events = [Event(**e) for e in data.get("events", [])]
            self.fetched_at = data.get("fetched_at", "")
        except Exception:
            self.events = []

    def _save_cache(self) -> None:
        from .config import write_json
        try:
            write_json(self.cache_path, {"fetched_at": self.fetched_at, "events": [asdict(e) for e in self.events]})
        except OSError:
            pass

    # -- refresh -------------------------------------------------------------

    def refresh(self, force: bool = False) -> bool:
        """Fetch all feeds. Returns True if the cache changed."""
        if not self.urls:
            self.events, self.error = [], ""
            return False
        if not force and self._clock() - self._last < self.refresh_seconds:
            return False
        self._last = self._clock()
        start = datetime.now() - timedelta(days=1)
        end = datetime.now() + timedelta(days=self.days_ahead)
        events, errors = [], []
        for i, url in enumerate(self.urls):
            try:
                events += parse_ics(self._fetch(url), start, end, calendar_name=f"kalenteri {i + 1}" if len(self.urls) > 1 else "")
            except Exception as exc:
                errors.append(f"kalenteri {i + 1}: {str(exc)[:120]}")
        with self._lock:
            if errors and not events:
                self.error = "; ".join(errors)
                return False
            self.error = "; ".join(errors)
            seen, merged = set(), []
            for e in sorted(events, key=lambda e: e.start):
                if e.uid not in seen:
                    seen.add(e.uid)
                    merged.append(e)
            changed = [asdict(e) for e in merged] != [asdict(e) for e in self.events]
            self.events = merged
            self.fetched_at = datetime.now().isoformat(timespec="seconds")
        self._save_cache()
        return changed

    def start_background(self) -> None:
        def loop():
            while not self._stop.is_set():
                try:
                    self.refresh()
                except Exception as exc:
                    log.warning("calendar refresh failed: %s", exc)
                self._stop.wait(60)
        threading.Thread(target=loop, name="calendar", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # -- queries -------------------------------------------------------------

    def upcoming(self, now: datetime | None = None, limit: int = 20) -> list[Event]:
        now = now or datetime.now()
        with self._lock:
            return [e for e in self.events if e.ends() >= now and not e.all_day][:limit]

    def current(self, now: datetime | None = None, early_minutes: int = 10, late_minutes: int = 15) -> Event | None:
        """The meeting happening now: started up to `late_minutes` ago or starting
        within `early_minutes`, not over yet. Timed events only."""
        now = now or datetime.now()
        best = None
        with self._lock:
            for e in self.events:
                if e.all_day:
                    continue
                if e.starts() - timedelta(minutes=early_minutes) <= now <= e.ends() and \
                        now <= e.starts() + timedelta(minutes=max(late_minutes, 1)) + (e.ends() - e.starts()):
                    if best is None or abs((e.starts() - now).total_seconds()) < abs((best.starts() - now).total_seconds()):
                        best = e
        return best

    def today(self, day: date | None = None) -> list[Event]:
        day = day or date.today()
        with self._lock:
            return [e for e in self.events if e.starts().date() == day]

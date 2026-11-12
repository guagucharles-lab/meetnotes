import time
from datetime import datetime, timedelta

import pytest

from meetnotes import cal
from meetnotes.assistant import briefing_from_event, list_briefings

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VTIMEZONE
TZID:Europe/Helsinki
BEGIN:STANDARD
DTSTART:19701025T040000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
TZOFFSETFROM:+0300
TZOFFSETTO:+0200
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T030000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
TZOFFSETFROM:+0200
TZOFFSETTO:+0300
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:kontio@example.com
DTSTART;TZID=Europe/Helsinki:20261114T100000
DTEND;TZID=Europe/Helsinki:20261114T110000
SUMMARY:Kontio Oy – sopimusneuvottelu
DESCRIPTION:Agenda:\\n1. Hinta ja maksuehdot\\n2. Toimitusaika\\n- Tuki\\n\\nLiity: https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc
ORGANIZER;CN=Mira Lahti:mailto:mira@example.com
ATTENDEE;CN=Jari Kontio;PARTSTAT=ACCEPTED:mailto:jari@kontio.fi
ATTENDEE;CN=Pekka Poissa;PARTSTAT=DECLINED:mailto:pekka@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:anna@example.com
END:VEVENT
BEGIN:VEVENT
UID:weekly@example.com
DTSTART;TZID=Europe/Helsinki:20261102T090000
DTEND;TZID=Europe/Helsinki:20261102T093000
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Viikkopalaveri
LOCATION:https://meet.google.com/abc-defg-hij
END:VEVENT
BEGIN:VEVENT
UID:allday@example.com
DTSTART;VALUE=DATE:20261113
DTEND;VALUE=DATE:20261114
SUMMARY:Loma
END:VEVENT
BEGIN:VEVENT
UID:cancelled@example.com
DTSTART:20261114T120000Z
DTEND:20261114T123000Z
SUMMARY:Peruttu
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture(autouse=True)
def helsinki(monkeypatch):
    """The user's machine runs in Finnish local time; the test box runs UTC."""
    monkeypatch.setenv("TZ", "Europe/Helsinki")
    time.tzset()
    yield
    monkeypatch.delenv("TZ")
    time.tzset()


def test_parse_ics_expands_recurrence_and_reads_details():
    events = cal.parse_ics(ICS, datetime(2026, 11, 9), datetime(2026, 11, 20))
    titles = [(e.title, e.start) for e in events]
    assert ("Viikkopalaveri", "2026-11-09T09:00") in titles and ("Viikkopalaveri", "2026-11-16T09:00") in titles
    assert not any(t == "Peruttu" for t, _ in titles)
    kontio = next(e for e in events if e.title.startswith("Kontio"))
    assert kontio.attendees == ["Jari Kontio", "anna@example.com"]   # declined one dropped, no CN -> address
    assert kontio.organizer == "Mira Lahti" and kontio.platform() == "Microsoft Teams"
    assert kontio.link.startswith("https://teams.microsoft.com/l/meetup-join/")
    assert cal.agenda_from_description(kontio.description) == ["Hinta ja maksuehdot", "Toimitusaika", "Tuki"]
    weekly = next(e for e in events if e.title == "Viikkopalaveri")
    assert weekly.platform() == "Google Meet" and weekly.link == "https://meet.google.com/abc-defg-hij"
    loma = next(e for e in events if e.title == "Loma")
    assert loma.all_day and loma.start == "2026-11-13T00:00"
    uids = [e.uid for e in events]
    assert len(uids) == len(set(uids))


def test_calendar_cache_current_and_upcoming(tmp_path):
    t = {"now": 0.0}
    fetches = []

    def fetch(url):
        fetches.append(url)
        return ICS

    c = cal.Calendar(["https://example.com/a.ics"], tmp_path / "cal.json", fetch=fetch, clock=lambda: t["now"])
    assert c.events == []
    # freeze "now" inside the ICS range by asking with explicit times
    assert c.refresh(force=True) is True and len(fetches) == 1
    assert c.refresh() is False                         # within refresh interval, no fetch
    t["now"] += 1000
    c.refresh()
    assert len(fetches) == 2

    now = datetime(2026, 11, 14, 9, 55)
    cur = c.current(now)
    assert cur is not None and cur.title.startswith("Kontio")        # starts in 5 min
    assert c.current(datetime(2026, 11, 14, 11, 10)) is None          # over
    assert c.current(datetime(2026, 11, 13, 10, 0)) is None           # all-day never counts
    up = c.upcoming(datetime(2026, 11, 14, 9, 0))
    assert [e.title for e in up][:2] == ["Kontio Oy – sopimusneuvottelu", "Viikkopalaveri"]
    assert [e.title for e in c.today(datetime(2026, 11, 16).date())] == ["Viikkopalaveri"]

    # cache survives a restart; a broken feed keeps the old events
    c2 = cal.Calendar(["https://example.com/a.ics"], tmp_path / "cal.json", fetch=lambda u: (_ for _ in ()).throw(OSError("down")))
    assert len(c2.events) == len(c.events)
    assert c2.refresh(force=True) is False and "down" in c2.error and len(c2.events) == len(c.events)
    assert cal.Calendar([], tmp_path / "none.json").refresh(force=True) is False


def test_briefing_from_event_is_prefilled_and_reused(tmp_path):
    ev = cal.parse_ics(ICS, datetime(2026, 11, 14), datetime(2026, 11, 15))[0]
    assert ev.title.startswith("Kontio")
    b = briefing_from_event(tmp_path, ev)
    assert b.title == ev.title and b.when == "2026-11-14 10:00" and b.participants == ["Jari Kontio", "anna@example.com"]
    assert b.agenda == ["Hinta ja maksuehdot", "Toimitusaika", "Tuki"] and "Liity:" in b.materials
    assert briefing_from_event(tmp_path, ev).id == b.id and len(list_briefings(tmp_path)) == 1


def test_fetch_rejects_non_https():
    with pytest.raises(ValueError):
        cal.fetch_ics("file:///etc/passwd")

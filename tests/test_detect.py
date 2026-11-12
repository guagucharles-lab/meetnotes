from meetnotes.detect import classify_window_title, classify_titles, Meeting


def test_google_meet_titles():
    assert classify_window_title("Meet – abc-defg-hij – Google Chrome") == Meeting("meet", "abc-defg-hij")
    assert classify_window_title("Meet - Weekly sync - Microsoft Edge") == Meeting("meet", "Weekly sync")
    assert classify_window_title("(2) Meet – Design review") == Meeting("meet", "Design review")
    assert classify_window_title("meet.google.com/abc-defg-hij - Brave") == Meeting("meet", "abc-defg-hij")
    assert classify_window_title("Google Meet - Google Chrome") is None  # landing page, not a call


def test_zoom_titles():
    assert classify_window_title("Zoom Meeting") == Meeting("zoom", "Zoom Meeting")
    assert classify_window_title("Zoom Webinar") == Meeting("zoom", "Zoom Meeting") or \
        classify_window_title("Zoom Webinar").platform == "zoom"
    assert classify_window_title("Zoom Meeting - Standup") == Meeting("zoom", "Standup")
    assert classify_window_title("Zoom Workplace") is None
    assert classify_window_title("Zoom") is None


def test_teams_titles():
    assert classify_window_title("Weekly planning | Microsoft Teams") == Meeting("teams", "Weekly planning")
    assert classify_window_title("Meeting with Sam | Microsoft Teams").platform == "teams"
    assert classify_window_title("Chat | Microsoft Teams") is None
    assert classify_window_title("(3) Chat | Microsoft Teams") is None
    assert classify_window_title("Calendar | Microsoft Teams") is None
    assert classify_window_title("Microsoft Teams") is None
    assert classify_window_title("Activity | Microsoft Teams") is None


def test_unrelated_titles():
    for t in ["Inbox - Gmail", "main.py - Visual Studio Code", "", "   ", "Zoom - Settings"]:
        assert classify_window_title(t) is None


def test_classify_titles_respects_allowed_platforms():
    titles = ["Inbox - Gmail", "Zoom Meeting", "Standup | Microsoft Teams"]
    assert classify_titles(titles).platform == "zoom"
    assert classify_titles(titles, {"teams"}).platform == "teams"
    assert classify_titles(titles, {"meet"}) is None

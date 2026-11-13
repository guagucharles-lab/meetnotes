"""Translation with a stand-in for the API client (no network)."""
import json
from types import SimpleNamespace

import pytest

from meetnotes import translate as tr
from meetnotes.config import Config
from meetnotes.transcribe import Segment


class FakeStream:
    def __init__(self, reply):
        self.reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=self.reply)])


class FakeClient:
    """Translates by prefixing "EN:"; can drop one id to test robustness."""

    def __init__(self, drop=None):
        self.calls = []
        self.drop = drop
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kw):
        self.calls.append(kw)
        body = kw["messages"][0]["content"]
        if "lines" in json.dumps(kw.get("output_config", {})):
            items = json.loads(body)["lines"]
            out = [{"id": x["id"], "text": "EN: " + x["text"]} for x in items if x["id"] != self.drop]
            return FakeStream(json.dumps({"lines": out}))
        return FakeStream(body.replace("# Muistiinpanot", "# Notes"))


def test_transcript_keeps_timestamps_speakers_and_notes():
    text = "# Transcript\n\n[00:00:01] Me: Hei kaikki\n[00:00:05] Others: Moi\n[00:01:00] (recording paused)\n[00:02:00] Ilman puhujaa\n"
    c = FakeClient()
    out = tr.translate_transcript_text(text, "en", "claude-opus-5-5", client=c)
    assert out.splitlines() == ["# Transcript", "", "[00:00:01] Me: EN: Hei kaikki", "[00:00:05] Others: EN: Moi",
                                "[00:01:00] (recording paused)", "[00:02:00] EN: Ilman puhujaa"]
    assert "English" in c.calls[0]["system"] and c.calls[0]["fallbacks"] == "default"


def test_missing_line_stays_original_and_batches_are_split(monkeypatch):
    monkeypatch.setattr(tr, "BATCH", 2)
    c = FakeClient(drop=1)
    out = tr.translate_lines(["a", "b", "c"], "en", "m", client=c)
    assert out == ["EN: a", "b", "EN: c"] and len(c.calls) == 2


def test_unknown_language_rejected():
    with pytest.raises(ValueError):
        tr.translate_lines(["x"], "xx", "m", client=FakeClient())


def test_translate_meeting_writes_files_and_meta(tmp_path):
    (tmp_path / "transcript.md").write_text("[00:00:01] Me: Hei\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# Muistiinpanot\n\n- asia\n", encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({"title": "T", "translating": "en"}))
    tr.translate_meeting(tmp_path, Config(), "en", client=FakeClient())
    assert (tmp_path / "transcript.en.md").read_text().strip() == "[00:00:01] Me: EN: Hei"
    assert (tmp_path / "notes.en.md").read_text().startswith("# Notes")
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["translations"] == ["en"] and "translating" not in meta
    got = tr.translations(tmp_path)
    assert got["en"]["name"] == "English" and got["en"]["notes"].startswith("# Notes")


def test_live_translator_batches_and_saves(tmp_path):
    c = FakeClient()
    t = tr.LiveTranslator("en", "m", client=c, wait=0.2)
    t.start()
    t.add([Segment(1, 2, "Me", "Hei"), Segment(3, 4, "Others", "Moi"), Segment(5, 5, "", "(recording paused)")])
    import time
    for _ in range(50):
        if len(t.snapshot()) == 2:
            break
        time.sleep(0.1)
    t.stop()
    assert [x["text"] for x in t.snapshot()] == ["EN: Hei", "EN: Moi"]
    assert len(c.calls) == 1  # both lines in one request
    t.save(tmp_path)
    assert (tmp_path / "live-translation.en.md").read_text().splitlines()[0] == "[00:00:01] Me: EN: Hei"

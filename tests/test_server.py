"""The app server over real HTTP(S), with fake transcription and notes."""
import http.client
import json
import ssl
import subprocess
import time

import pytest

from meetnotes import credentials, service
from meetnotes.config import Config, load
from meetnotes.phone import PhoneRecorder
from meetnotes.server import App
from meetnotes.transcribe import Segment


class FakeTranscriber:
    def transcribe(self, samples, offset, speaker=""):
        if len(samples) < 8000:
            return []
        return [Segment(offset, offset + 2, speaker,
                        "we agreed to ship the new pricing page next Tuesday and Sami owns it")]


@pytest.fixture
def app(tmp_path, monkeypatch):
    cfg = Config(notes_dir=str(tmp_path / "notes"), chunk_seconds=5)
    notes_written = []

    def fake_notes(folder, text, meta, cfg):
        notes_written.append((folder, text, meta))
        (folder / "notes.md").write_text(f"# {meta.title}\n\n## Summary\n\nok\n")
        return folder / "notes.md"

    phone = PhoneRecorder(cfg, transcriber=FakeTranscriber(), notes_fn=fake_notes)
    a = App(cfg, config_path=tmp_path / "config.toml", phone=phone,
            ask_fn=lambda q, docs, model: f"answer to {q} from {len(docs)} meetings")
    a.start(port=0)
    a.notes_written = notes_written
    yield a
    a.stop()


def call(app, method, path, body=None, token=True, host=None, raw=None, lan=False):
    if lan:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", app.lan.server_address[1], timeout=10, context=ctx)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", app.local.server_address[1], timeout=10)
    headers = {"Host": host or "127.0.0.1"}
    if token:
        headers["X-Meetnotes-Token"] = app.token if token is True else token
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    payload = res.read()
    conn.close()
    try:
        return res.status, json.loads(payload)
    except ValueError:
        return res.status, payload


def test_access_control(app):
    assert call(app, "GET", "/api/ping", token=False) == (200, {"ok": True, "app": "meetnotes"})
    status, page = call(app, "GET", "/", token=False)
    assert status == 200 and b"Meetnotes" in page          # the page holds no data
    assert call(app, "GET", "/api/status", token=False)[0] == 401
    assert call(app, "GET", "/api/status", token="wrong")[0] == 401
    assert call(app, "POST", "/api/record", {"on": True}, token=False)[0] == 401
    assert call(app, "GET", "/api/status", host="evil.example.com")[0] == 403  # DNS rebinding
    assert service.force_title() is None


def test_controls(app):
    status, st = call(app, "GET", "/api/status")
    assert status == 200 and st["device"] == "computer" and st["safe_word"] == "omerta"
    call(app, "POST", "/api/record", {"on": True, "title": "Asiakaspalaveri"})
    assert service.force_title() == "Asiakaspalaveri"
    call(app, "POST", "/api/record", {"on": False})
    assert service.take_skip()
    call(app, "POST", "/api/pause", {"on": True})
    assert service.is_paused()
    call(app, "POST", "/api/pause", {"on": False})
    assert not service.is_paused()
    _, st = call(app, "POST", "/api/auto", {"on": False})
    assert st["auto"] is False and not service.auto_enabled()
    call(app, "POST", "/api/auto", {"on": True})
    assert service.auto_enabled()


def test_meetings_read_and_delete(app):
    root = app.cfg.notes_root
    folder = root / "2026-10-30_0900_Standup"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"title": "Standup", "status": "done",
                                                   "started_at": "2026-10-30T09:00:00", "duration_s": 600}))
    (folder / "notes.md").write_text("# Standup\n")
    (folder / "transcript.md").write_text("[00:00:01] Me: hello\n")
    (root / "secret.txt").write_text("not a meeting")

    _, data = call(app, "GET", "/api/meetings")
    assert [m["id"] for m in data["meetings"]] == ["2026-10-30_0900_Standup"]
    _, m = call(app, "GET", "/api/meetings/2026-10-30_0900_Standup")
    assert m["notes"] == "# Standup\n" and "Me: hello" in m["transcript"]
    for bad in ("..", "..%2Fsecret.txt", "secret.txt", "nope"):
        assert call(app, "GET", f"/api/meetings/{bad}")[0] == 404
    assert call(app, "POST", "/api/meetings/2026-10-30_0900_Standup/delete", {})[0] == 200
    assert not folder.exists() and (root / "secret.txt").exists()


def test_settings(app, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(credentials, "set_api_key", lambda k: saved.setdefault("key", k))
    status, res = call(app, "POST", "/api/settings", {
        "language": "fi", "whisper_model": "large-v3-turbo", "vocabulary": "Sami, Kesko",
        "safe_word": "omerta", "keep_audio": False, "api_key": "sk-ant-test"})
    assert status == 200 and res["errors"] == {}
    assert saved["key"] == "sk-ant-test"
    on_disk = load(tmp_path / "config.toml")
    assert on_disk.language == "fi" and on_disk.vocabulary == ["Sami", "Kesko"] and on_disk.keep_audio is False
    _, s = call(app, "GET", "/api/settings")
    assert "api_key" not in s and s["whisper_model"] == "large-v3-turbo"
    status, res = call(app, "POST", "/api/settings", {"whisper_model": "huge", "notes_dir": "/tmp/x"})
    assert status == 400 and set(res["errors"]) == {"whisper_model", "notes_dir"}
    assert app.cfg.whisper_model == "large-v3-turbo"


def test_ask(app):
    folder = app.cfg.notes_root / "2026-10-30_0900_x"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("# x\nBudget 120k\n")
    status, res = call(app, "POST", "/api/ask", {"question": "Mitä budjetista?"})
    assert status == 200 and res["answer"] == "answer to Mitä budjetista? from 1 meetings"
    assert call(app, "POST", "/api/ask", {"question": " "})[0] == 400


def test_phone_access_over_https(app):
    assert call(app, "GET", "/api/pair")[1]["enabled"] is False
    app.start_lan(port=0)
    _, st = call(app, "GET", "/api/status", lan=True, host="192.168.1.20")
    assert st["device"] == "phone"
    assert call(app, "GET", "/api/status", lan=True, host="192.168.1.20", token=False)[0] == 401
    assert call(app, "GET", "/api/pair", lan=True, host="192.168.1.20")[0] == 403  # QR only on the computer
    assert call(app, "GET", "/api/status", lan=True, host="phone.attacker.test")[0] == 403
    pair = call(app, "GET", "/api/pair")[1]
    assert pair["enabled"] is True
    if pair["url"]:  # needs a network interface
        assert pair["url"].startswith("https://") and app.token in pair["url"] and "<svg" in pair["qr_svg"]
    app.stop_lan()


def test_manifest_carries_token_only_when_valid(app):
    _, m = call(app, "GET", f"/manifest.webmanifest?t={app.token}", token=False)
    assert m["start_url"] == f"/?t={app.token}"
    _, m = call(app, "GET", "/manifest.webmanifest?t=guess", token=False)
    assert m["start_url"] == "/"


def test_phone_recording_end_to_end(app, tmp_path):
    """A real WebM/Opus file (what Android Chrome records), uploaded in pieces."""
    webm = tmp_path / "rec.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=12",
                    "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(webm)], check=True)
    data = webm.read_bytes()

    _, res = call(app, "POST", "/api/phone/start", {"title": "Asiakastapaaminen", "mime": "audio/webm;codecs=opus"})
    sid = res["id"]
    pieces = [data[i:i + 4000] for i in range(0, len(data), 4000)]
    for piece in pieces:
        status, res = call(app, "POST", f"/api/phone/{sid}/chunk", raw=piece)
        assert status == 200
    assert res["bytes"] == len(data)
    call(app, "POST", f"/api/phone/{sid}/finish", {})
    for _ in range(100):
        _, st = call(app, "GET", f"/api/phone/{sid}")
        if st["status"] != "processing":
            break
        time.sleep(0.1)
    assert st["status"] == "done", st

    folder = app.cfg.notes_root / sid
    assert (folder / "notes.md").exists() and not list(folder.glob("phone.*"))
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["platform"] == "Phone" and 11.5 < meta["duration_s"] < 12.5
    from meetnotes.audio import read_wav_channels
    left, _, _ = read_wav_channels(folder / "audio.wav")
    assert abs(len(left) / 16000 - 12) < 0.5
    folder_, text, m = app.notes_written[0]
    assert "Sami owns it" in text and m.platform == "Phone (in person)"
    assert call(app, "POST", f"/api/phone/{sid}/chunk", raw=b"late")[0] == 404  # closed


def test_phone_recording_with_safe_word_discard(app, tmp_path):
    class SaysIt(FakeTranscriber):
        def transcribe(self, samples, offset, speaker=""):
            return [Segment(offset, offset + 1, speaker, "okei omerta")] if offset >= 5 else \
                super().transcribe(samples, offset, speaker)

    app.phone._transcriber = SaysIt()
    app.cfg.safe_word_action = "discard"
    wav = tmp_path / "rec.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=12",
                    "-ac", "1", "-ar", "16000", str(wav)], check=True)
    sid = call(app, "POST", "/api/phone/start", {"title": "x", "mime": "audio/wav"})[1]["id"]
    call(app, "POST", f"/api/phone/{sid}/chunk", raw=wav.read_bytes())
    app.phone.finish(sid, background=False)
    assert app.phone.status(sid)["status"] == "empty"
    assert not (app.cfg.notes_root / sid).exists()


def test_retry_notes_from_the_app(app):
    folder = app.cfg.notes_root / "2026-10-30_1617_Testi"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"title": "Testi", "status": "recording",
                                                   "started_at": "2026-10-30T16:17:00"}))
    calls = []

    def fail_then_succeed(f, cfg):
        calls.append(f)
        if len(calls) == 1:
            raise RuntimeError("401 invalid x-api-key")
        (f / "notes.md").write_text("# Testi\n")
        m = json.loads((f / "meta.json").read_text()); m["status"] = "done"
        (f / "meta.json").write_text(json.dumps(m))

    app.summarize_fn = fail_then_succeed

    def wait_status():
        for _ in range(50):
            st = call(app, "GET", "/api/meetings/2026-10-30_1617_Testi")[1]["meta"]["status"]
            if st != "summarizing":
                return st
            time.sleep(0.05)

    assert call(app, "POST", "/api/meetings/2026-10-30_1617_Testi/summarize", {})[0] == 202
    assert wait_status() == "notes_failed"
    meta = call(app, "GET", "/api/meetings/2026-10-30_1617_Testi")[1]["meta"]
    assert "invalid x-api-key" in meta["notes_error"]
    call(app, "POST", "/api/meetings/2026-10-30_1617_Testi/summarize", {})
    assert wait_status() == "done"

    # A meeting that is still being recorded can't be summarised yet
    service.write_status(state="recording", folder=str(folder))
    assert call(app, "POST", "/api/meetings/2026-10-30_1617_Testi/summarize", {})[0] == 400
    _, st = call(app, "GET", "/api/status")
    assert st["folder_id"] == "2026-10-30_1617_Testi"


def test_android_pairing_code(app, monkeypatch):
    app.start_lan(port=0)
    lan = dict(lan=True, host="192.168.1.20", token=False)
    pair = call(app, "GET", "/api/pair")[1]
    code = pair["code"]
    assert len(code) == 6 and code.isdigit() and 590 <= pair["code_expires_in"] <= 600
    assert len(pair["fingerprint"]) == 64
    if pair["android_url"]:
        assert pair["android_url"].startswith("meetnotes://pair?u=https%3A%2F%2F") and pair["fingerprint"] in pair["android_url"]
        assert "<svg" in pair["android_qr_svg"]

    # The fingerprint matches the certificate the phone actually sees
    import hashlib
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    with ctx.wrap_socket(__import__("socket").create_connection(("127.0.0.1", app.lan.server_address[1])), server_hostname="x") as s:
        assert hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest() == pair["fingerprint"]

    assert call(app, "POST", "/api/pair/claim", {"code": "000000" if code != "000000" else "111111"}, **lan)[0] == 403
    status, res = call(app, "POST", "/api/pair/claim", {"code": code[:3] + " " + code[3:]}, **lan)
    assert status == 200 and res["token"] == app.token and res["fingerprint"] == pair["fingerprint"]
    assert call(app, "POST", "/api/pair/claim", {"code": code}, **lan)[0] == 403        # used once only
    assert call(app, "POST", "/api/pair/claim", {"code": code}, token=False)[0] == 404  # not on the computer side

    # Guessing: five wrong codes lock it and replace the code
    new_code = call(app, "GET", "/api/pair")[1]["code"]
    for _ in range(5):
        call(app, "POST", "/api/pair/claim", {"code": "999999" if new_code != "999999" else "888888"}, **lan)
    status, res = call(app, "POST", "/api/pair/claim", {"code": new_code}, **lan)
    assert status == 429 and "attempts" in res["error"]
    assert call(app, "POST", "/api/pair/claim", {"code": new_code}, **lan)[0] in (403, 429)  # thrown away
    app._pair_locked_until = 0
    assert call(app, "GET", "/api/pair")[1]["code"] != new_code

    # Pairing is open only while the code is on the computer's screen
    code = call(app, "GET", "/api/pair")[1]["code"]
    app._pair_shown -= app.PAIR_SHOWN_S + 1   # settings page closed a while ago
    status, res = call(app, "POST", "/api/pair/claim", {"code": code}, **lan)
    assert status == 403 and "open Settings" in res["error"]
    app.stop_lan()


def test_forget_devices_replaces_the_token(app):
    app.start_lan(port=0)
    old = app.token
    assert call(app, "GET", "/api/status", lan=True, host="192.168.1.20", token=old)[0] == 200
    assert call(app, "POST", "/api/pair/forget", {}, lan=True, host="192.168.1.20", token=old)[0] == 400  # not from a phone
    status, res = call(app, "POST", "/api/pair/forget", {})
    assert status == 200 and res["token"] != old and len(res["token"]) >= 32
    assert call(app, "GET", "/api/status", lan=True, host="192.168.1.20", token=old)[0] == 401   # the lost phone is out
    assert call(app, "GET", "/api/status", token=res["token"])[0] == 200
    from meetnotes.server import app_token
    assert app_token() == res["token"]   # survives a restart; the desktop tray reads it from here
    app.stop_lan()


def test_phone_cannot_redirect_notes_or_updates(app):
    app.start_lan(port=0)
    lan = dict(lan=True, host="192.168.1.20")
    status, res = call(app, "POST", "/api/settings", {"webhook_url": "https://evil.example/hook",
                                                      "calendar_urls": "https://evil.example/c.ics",
                                                      "update_url": "https://evil.example/u.json",
                                                      "watch_words": "hinta"}, **lan)
    assert status in (200, 400), res
    assert "webhook_url" not in res.get("errors", {}) and "calendar_urls" not in res.get("errors", {})
    assert app.cfg.webhook_url == "" and app.cfg.calendar_urls == [] and app.cfg.update_url == ""
    assert app.cfg.watch_words == ["hinta"]               # ordinary settings still work from the phone
    call(app, "POST", "/api/settings", {"update_url": "https://evil.example/u.json"})
    assert app.cfg.update_url == ""                       # not editable through the app at all
    call(app, "POST", "/api/settings", {"webhook_url": "https://hooks.example/x"})
    assert app.cfg.webhook_url == "https://hooks.example/x"   # the computer can
    app.stop_lan()

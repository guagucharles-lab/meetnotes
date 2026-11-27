"""The app: a small web server inside the background service.

* http://127.0.0.1:<app_port>  - this computer only; the desktop window shows it
* https://<wifi-ip>:<phone_port> - only when phone_access is on; for your phone

Every /api call needs the pairing token (a long random secret kept in the
state folder). The page itself holds no data, so it is served openly, and the
token is sent in a custom header, which browsers never attach on their own
(so other websites cannot drive the app). The token reaches the phone only
through the QR code shown in the desktop app.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config as config_mod, live, service
from .config import Config, state_dir
from .translate import LANGUAGES, translations

log = logging.getLogger("meetnotes")
WEB = Path(__file__).parent / "web"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
MEETING_ID = re.compile(r"^[\w.\-]{1,120}$")

# Settings the app may change, with their types.
EDITABLE = {
    "language": str, "whisper_model": str, "notes_language": str, "safe_word": str,
    "safe_word_action": str, "keep_audio": bool, "live_notes": bool, "watch_platforms": list,
    "ignore_titles": list, "vocabulary": list, "idle_stop_minutes": int, "phone_access": bool,
    "tray_icon": bool, "extra_instructions": str,
    "record_screen": bool, "screen_monitor": int, "screen_fps": int, "transcribe_mode": str,
    "assistant_live": bool, "assistant_interval": int, "assistant_style": str, "assistant_model": str, "claude_model": str,
    "translate_to": str, "translate_live": bool,
    "calendar_urls": list, "notes_template": str, "watch_words": list, "webhook_url": str,
    "update_url": str, "update_check": bool,
}
CHOICES = {
    "whisper_model": {"tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"},
    "safe_word_action": {"stop", "discard"},
    "screen_monitor": {0, 1, 2, 3},
    "screen_fps": {2, 5, 10},
    "transcribe_mode": {"mixed", "separate"},
    "assistant_interval": {30, 45, 60, 90, 120},
    "assistant_style": {"quiet", "balanced", "active"},
    "assistant_model": {"claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5"},
    "claude_model": {"claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5"},
    "translate_to": {"", *LANGUAGES},
}


def app_token() -> str:
    path = state_dir() / "app_token"
    try:
        token = path.read_text().strip()
        if len(token) >= 32:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    path.write_text(token)
    if os.name == "posix":
        os.chmod(path, 0o600)
    return token


def lan_ip() -> str | None:
    """This computer's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class App:
    """Everything the request handler needs, shared across both listeners."""

    def __init__(self, cfg: Config, config_path: Path | None = None, phone=None, ask_fn=None) -> None:
        from .phone import PhoneRecorder
        self.cfg = cfg
        self.config_path = config_path
        self.token = app_token()
        self.phone = phone or PhoneRecorder(cfg)
        self.ask_fn = ask_fn
        self.local: ThreadingHTTPServer | None = None
        self.lan: ThreadingHTTPServer | None = None
        self.lan_error = ""
        self.cert_fp = ""
        self._pair_code = ""
        self._pair_expires = 0.0
        self._pair_fails = 0
        self._pair_locked_until = 0.0
        self._pair_lock = threading.Lock()
        self.summarizing: set[str] = set()
        self._audio_lock = threading.Lock()
        self.summarize_fn = None  # tests swap in a fake
        self.preparing: set[str] = set()
        self.prepare_fn = None    # tests swap in a fake
        self.chat_fn = None
        self.calendar = None      # cal.Calendar, set by the watcher

    # -- listeners -----------------------------------------------------------

    def start(self, port: int | None = None) -> None:
        self.local = self._serve(("127.0.0.1", self.cfg.app_port if port is None else port), lan=False)
        if self.cfg.phone_access:
            self.start_lan()
        threading.Thread(target=self.phone.recover, daemon=True).start()

    def _serve(self, addr, lan: bool, ssl_ctx: ssl.SSLContext | None = None) -> ThreadingHTTPServer:
        handler = type("BoundHandler", (Handler,), {"app": self, "lan": lan})
        server = QuietServer(addr, handler)
        server.daemon_threads = True
        if ssl_ctx is not None:
            # Handshake in the request thread, so one slow phone can't block accept().
            server.socket = ssl_ctx.wrap_socket(server.socket, server_side=True,
                                                do_handshake_on_connect=False)
        threading.Thread(target=server.serve_forever, daemon=True,
                         name=f"app-{'lan' if lan else 'local'}").start()
        log.info("App listening on %s://%s:%s", "https" if ssl_ctx else "http", *server.server_address[:2])
        return server

    def start_lan(self, port: int | None = None) -> None:
        if self.lan is not None:
            return
        from .tls import ensure_cert, fingerprint
        ip = lan_ip()
        try:
            cert, key = ensure_cert(state_dir(), [ip] if ip else [])
            self.cert_fp = fingerprint(cert)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert, key)
            self.lan = self._serve(("0.0.0.0", self.cfg.phone_port if port is None else port),
                                   lan=True, ssl_ctx=ctx)
            self.lan_error = ""
        except OSError as exc:
            self.lan_error = f"Phone access could not start: {exc}"
            log.warning(self.lan_error)

    def stop_lan(self) -> None:
        if self.lan is not None:
            self.lan.shutdown()
            self.lan.server_close()
            self.lan = None

    def stop(self) -> None:
        self.stop_lan()
        if self.local is not None:
            self.local.shutdown()
            self.local.server_close()
            self.local = None

    def local_url(self) -> str:
        port = self.local.server_address[1] if self.local else self.cfg.app_port
        return f"http://127.0.0.1:{port}/?t={self.token}"

    def phone_url(self) -> str | None:
        ip = lan_ip()
        if not ip or self.lan is None:
            return None
        return f"https://{ip}:{self.lan.server_address[1]}/?t={self.token}&fp={self.cert_fp}"

    def android_url(self) -> str | None:
        """Deep link the Android app understands (meetnotes://pair?...)."""
        from urllib.parse import quote
        ip = lan_ip()
        if not ip or self.lan is None:
            return None
        server = f"https://{ip}:{self.lan.server_address[1]}"
        return f"meetnotes://pair?u={quote(server, safe='')}&t={self.token}&fp={self.cert_fp}"

    # -- pairing code for the Android app ------------------------------------
    # A short code you type into the app, instead of the long token. It lasts
    # 10 minutes, works once, and five wrong guesses lock it for a minute and
    # replace it.

    def _new_code(self) -> None:
        self._pair_code = f"{secrets.randbelow(10 ** 6):06d}"
        self._pair_expires = time.monotonic() + 600
        self._pair_fails = 0

    def pair_code(self) -> tuple[str, int]:
        with self._pair_lock:
            if not self._pair_code or time.monotonic() >= self._pair_expires:
                self._new_code()
            return self._pair_code, int(self._pair_expires - time.monotonic())

    def claim(self, code: str) -> dict:
        with self._pair_lock:
            now = time.monotonic()
            if now < self._pair_locked_until:
                raise PermissionError("too many attempts, wait a minute")
            code = "".join(ch for ch in str(code) if ch.isdigit())
            if not self._pair_code or now >= self._pair_expires or \
                    not hmac.compare_digest(code, self._pair_code):
                self._pair_fails += 1
                if self._pair_fails >= 5:
                    self._pair_locked_until = now + 60
                    self._new_code()
                raise PermissionError("wrong or expired code")
            self._new_code()  # one use only
            return {"token": self.token, "fingerprint": self.cert_fp, "app": "meetnotes"}

    # -- API -----------------------------------------------------------------

    def status(self) -> dict:
        st = service.read_status()
        from .credentials import has_api_key
        from .update import read_state, _summary
        upd = _summary(read_state())
        return {
            "version": __version__,
            "state": st.get("state", "watching"),
            "meeting": st.get("meeting"),
            "platform": st.get("platform"),
            "since": st.get("since"),
            "error": st.get("error") or "",
            "phase": st.get("phase") or "",
            "notice": st.get("notice") or "",
            "backlog": st.get("backlog") or 0,
            "folder_id": Path(st["folder"]).name if st.get("folder") else "",
            "paused": service.is_paused(),
            "auto": service.auto_enabled(),
            "forced": service.force_title() is not None,
            "safe_word": self.cfg.safe_word,
            "record_screen": self.cfg.record_screen,
            "assistant_live": self.cfg.assistant_live,
            "update_available": upd.get("latest", "") if upd.get("available") else "",
            "update_progress": upd.get("progress", ""),
            "phone_access": self.lan is not None,
            "has_api_key": has_api_key(),
            "speech": _speech_state(),
        }

    def settings(self) -> dict:
        from .credentials import has_api_key
        data = {k: getattr(self.cfg, k) for k in EDITABLE}
        data.update(has_api_key=has_api_key(), autostart=service.autostart_enabled(),
                    notes_dir=str(self.cfg.notes_root))
        return data

    def update_settings(self, data: dict) -> dict:
        errors = {}
        for key, value in data.items():
            if key == "calendar_urls" and isinstance(value, str):
                value = [v.strip() for v in re.split(r"[\s,]+", value) if v.strip()]
            if key == "notes_template":
                from .templates import all_templates
                if str(value) not in all_templates(self.cfg.templates):
                    errors[key] = "unknown template"
                    continue
            if key == "api_key":
                from .credentials import set_api_key
                try:
                    set_api_key(str(value))
                except Exception as exc:
                    errors[key] = f"could not save the key: {exc}"
                continue
            if key == "autostart":
                service.enable_autostart() if value else service.disable_autostart()
                continue
            kind = EDITABLE.get(key)
            if kind is None:
                errors[key] = "not editable"
                continue
            if kind is list:
                if isinstance(value, str):
                    value = [v.strip() for v in value.split(",") if v.strip()]
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    errors[key] = "expected a list of words"
                    continue
            elif kind is bool:
                value = bool(value)
            elif kind is int:
                try:
                    value = max(0, int(value))
                except (TypeError, ValueError):
                    errors[key] = "expected a number"
                    continue
            else:
                value = str(value).strip()
            if key in CHOICES and value not in CHOICES[key]:
                errors[key] = f"must be one of {sorted(CHOICES[key])}"
                continue
            setattr(self.cfg, key, value)
        config_mod.save(self.cfg, self.config_path)
        if "calendar_urls" in data and self.calendar is not None:
            self.calendar.urls = list(self.cfg.calendar_urls)
            threading.Thread(target=lambda: self.calendar.refresh(force=True), daemon=True).start()
        if self.cfg.phone_access and self.lan is None:
            self.start_lan()
        elif not self.cfg.phone_access and self.lan is not None:
            self.stop_lan()
        return errors

    def summarize(self, mid: str) -> None:
        """(Re)write the notes of a past meeting in the background."""
        folder = self.meeting_folder(mid)
        st = service.read_status()
        if st.get("folder") and Path(st["folder"]).name == mid and \
                st.get("state") in ("recording", "paused", "finishing", "writing-notes"):
            raise ValueError("this meeting is still being recorded")
        if mid in self.summarizing:
            return
        self.summarizing.add(mid)
        from .session import _update_meta, resummarize
        _update_meta(folder, status="summarizing")

        def run():
            try:
                (self.summarize_fn or resummarize)(folder, self.cfg)
            except Exception as exc:
                log.warning("Notes for %s failed: %s", mid, exc)
                _update_meta(folder, status="notes_failed", notes_error=str(exc)[:500])
            finally:
                self.summarizing.discard(mid)
        threading.Thread(target=run, daemon=True, name=f"notes-{mid}").start()

    # -- assistant -----------------------------------------------------------

    def briefing_dict(self, b) -> dict:
        from dataclasses import asdict
        d = asdict(b)
        d["date"] = b.date()
        return d

    def briefings(self) -> list[dict]:
        from .assistant import list_briefings
        return [{"id": b.id, "title": b.title, "when": b.when, "date": b.date(), "agenda": b.agenda,
                 "prep_status": b.prep_status, "used_by": b.used_by, "participants": b.participants}
                for b in list_briefings(self.cfg.notes_root)]

    def save_briefing(self, data: dict):
        from .assistant import load_briefing, new_briefing, save_briefing, update_briefing
        root = self.cfg.notes_root
        if data.get("id"):
            b = load_briefing(root, str(data["id"]))
            update_briefing(b, data)
            save_briefing(root, b)
        else:
            b = new_briefing(root, **{k: v for k, v in data.items() if k != "id"})
        return b

    def prepare(self, bid: str) -> None:
        from .assistant import load_briefing, prepare, save_briefing
        root = self.cfg.notes_root
        b = load_briefing(root, bid)
        if bid in self.preparing:
            return
        self.preparing.add(bid)
        b.prep_status, b.prep_error = "preparing", ""
        save_briefing(root, b)

        def run():
            try:
                (self.prepare_fn or prepare)(root, b, self.cfg.claude_model)
            except Exception as exc:
                b.prep_status, b.prep_error = "failed", str(exc)[:300]
                log.warning("preparation for %s failed: %s", bid, exc)
            finally:
                save_briefing(root, b)
                self.preparing.discard(bid)
        threading.Thread(target=run, daemon=True, name=f"prep-{bid}").start()

    def briefing_chat(self, bid: str, question: str) -> str:
        from .assistant import chat, load_briefing, save_briefing
        root = self.cfg.notes_root
        b = load_briefing(root, bid)
        answer = (self.chat_fn or chat)(root, b, question, self.cfg.claude_model)
        save_briefing(root, b)
        return answer

    def calendar_state(self) -> dict:
        c = self.calendar
        from dataclasses import asdict
        if c is None or not self.cfg.calendar_urls:
            return {"configured": bool(self.cfg.calendar_urls), "events": [], "error": "", "fetched_at": ""}
        from .assistant import list_briefings
        by_uid = {b.event_uid: b.id for b in list_briefings(self.cfg.notes_root) if b.event_uid}
        now = __import__("datetime").datetime.now()
        events = []
        for e in c.upcoming(now, limit=30):
            d = asdict(e)
            d["briefing_id"] = by_uid.get(e.uid, "")
            d["platform"] = e.platform()
            d["now"] = e.starts() <= now <= e.ends()
            events.append(d)
        return {"configured": True, "events": events, "error": c.error, "fetched_at": c.fetched_at}

    def briefing_from_event(self, uid: str):
        from .assistant import briefing_from_event
        c = self.calendar
        ev = next((e for e in (c.events if c else []) if e.uid == uid), None)
        if ev is None:
            raise KeyError(uid)
        return briefing_from_event(self.cfg.notes_root, ev)

    def live_state(self) -> dict:
        s = live.current()
        if s is None:
            return {"active": False}
        from .assistant import list_briefings
        todays = [{"id": b.id, "title": b.title} for b in list_briefings(self.cfg.notes_root) if not b.used_by]
        return {"active": True, "meeting": s.title, "folder_id": s.dir.name,
                "enabled": s.assistant is not None, "assistant": s.assistant_snapshot(),
                "alerts": list(getattr(s, "alerts", [])), "my_notes": s.my_notes(),
                "template": getattr(s, "template", ""),
                "translation": ({"lang": s.translator.code, "lines": s.translator.snapshot(), "error": s.translator.error}
                                if getattr(s, "translator", None) is not None else None),
                "event": {"title": s.event.title, "attendees": s.event.attendees} if getattr(s, "event", None) else None,
                "briefing": {"id": s.briefing.id, "title": s.briefing.title} if s.briefing else None,
                "briefings": todays[:10]}

    def meeting_folder(self, mid: str) -> Path:
        if not MEETING_ID.match(mid) or mid in (".", ".."):
            raise KeyError(mid)
        folder = self.cfg.notes_root / mid
        if not folder.is_dir() or folder.resolve().parent != self.cfg.notes_root.resolve():
            raise KeyError(mid)
        return folder

    def _shown_status(self, mid: str, status: str) -> str:
        """A meeting still marked recording/processing that nothing is working
        on was cut off (crash, shutdown, reinstall): show it as interrupted."""
        if status not in ("recording", "processing", "transcribed", "summarizing"):
            return status
        s = live.current()
        if s is not None and s.dir.name == mid:
            return status
        ph = self.phone.sessions.get(mid) if hasattr(self.phone, "sessions") else None
        if ph is not None and ph.get("status") in ("recording", "processing"):
            return status
        if mid in self.summarizing:
            return "summarizing"
        return "interrupted"

    def meetings(self) -> list[dict]:
        from .library import meetings
        out = []
        for m in meetings(self.cfg.notes_root)[:200]:
            out.append({"id": m.path.name, "title": m.title, "status": self._shown_status(m.path.name, m.meta.get("status", "?")),
                        "platform": m.meta.get("platform", ""), "started": m.meta.get("started_at"),
                        "duration": m.meta.get("duration_s"),
                        "has_notes": (m.path / "notes.md").exists()})
        return out

    def meeting(self, mid: str) -> dict:
        folder = self.meeting_folder(mid)

        def read(name):
            f = folder / name
            return f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
        meta = {}
        try:
            meta = json.loads(read("meta.json") or "{}")
        except ValueError:
            pass
        if not read("notes.md"):
            meta["status"] = self._shown_status(mid, meta.get("status", ""))
        notes_json = {}
        try:
            notes_json = json.loads(read("notes.json") or "{}")
        except ValueError:
            pass
        return {"id": mid, "meta": meta, "notes": read("notes.md"), "notes_json": notes_json,
                "transcript": read("transcript.md"), "live_notes": read("live-notes.md"),
                "my_notes": read("my-notes.md"), "has_screen": (folder / "screen.mp4").exists(),
                "has_audio": self._audio_ready(mid, folder),
                "translations": translations(folder)}

    def _audio_ready(self, mid: str, folder: Path) -> bool:
        wav, m4a = folder / "audio.wav", folder / "audio.m4a"
        s = live.current()
        if s is not None and s.dir.name == mid:
            return False  # still being recorded
        return m4a.exists() or (wav.exists() and wav.stat().st_size > 16000)

    def meeting_audio(self, mid: str) -> Path:
        """audio.m4a for listening, made from audio.wav the first time (and
        again if the WAV changed, e.g. cut by the safe word)."""
        folder = self.meeting_folder(mid)
        if not self._audio_ready(mid, folder):
            raise KeyError(mid)
        wav, m4a = folder / "audio.wav", folder / "audio.m4a"
        with self._audio_lock:
            if wav.exists() and (not m4a.exists() or m4a.stat().st_mtime < wav.stat().st_mtime):
                from .screen import wav_to_m4a
                t0 = time.monotonic()
                wav_to_m4a(wav, m4a)
                log.info("Made %s (%.1f MB) in %.0f s", m4a, m4a.stat().st_size / 1e6, time.monotonic() - t0)
        return m4a

    def reveal(self, mid: str, name: str = "audio.m4a") -> None:
        """Show the file in Explorer / Finder."""
        folder = self.meeting_folder(mid)
        target = folder / name if (folder / name).exists() else folder
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])


def _speech_state() -> dict:
    from .whisperproc import state
    st = state()
    return {k: st.get(k) for k in ("phase", "downloaded_mb", "error", "model", "device")}


class QuietServer(ThreadingHTTPServer):
    """A phone rejecting the self-signed certificate (before you accept it) or
    a dropped Wi-Fi connection is normal; don't print tracebacks for it."""

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)):
            log.debug("connection from %s dropped: %s", client_address[0], exc)
        else:
            log.warning("app request failed", exc_info=True)


class Handler(BaseHTTPRequestHandler):
    app: App
    lan: bool
    server_version = "meetnotes"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; nothing sensitive in the log
        log.debug("app %s %s", self.command, self.path.split("?")[0])

    # -- helpers -------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), geolocation=()")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str, extra: dict | None = None) -> None:
        """A file with HTTP Range support, so the video can be scrubbed."""
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            first, _, last = rng[6:].split(",")[0].strip().partition("-")
            try:
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
            except ValueError:
                start, end = size, 0
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        remaining = end - start + 1
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the player seeks by dropping the connection

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        host = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0][1:]
        if host in ("127.0.0.1", "localhost"):
            return True
        return self.lan and _is_ip(host)  # blocks DNS-rebinding names

    def _authed(self) -> bool:
        token = self.headers.get("X-Meetnotes-Token", "")
        return bool(token) and hmac.compare_digest(token, self.app.token)

    def _body(self, limit: int = 1_000_000) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("body too large")
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        raw = self._body()
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    # -- routing -------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        url = urlparse(self.path)
        path = url.path
        if path == "/api/ping":
            return self._json(200, {"ok": True, "app": "meetnotes"})
        if path == "/manifest.webmanifest":
            return self._manifest(parse_qs(url.query).get("t", [""])[0])
        m = re.fullmatch(r"/api/meetings/([^/]+)/screen\.mp4", path)
        if m:
            # <video> can't send headers, so this one file takes the token in the URL.
            token = parse_qs(url.query).get("t", [""])[0]
            if not (token and hmac.compare_digest(token, self.app.token)):
                return self._json(401, {"error": "pair this device first"})
            try:
                video = self.app.meeting_folder(m.group(1)) / "screen.mp4"
            except KeyError:
                video = None
            if video is None or not video.exists():
                return self._json(404, {"error": "not found"})
            return self._send_file(video, "video/mp4")
        m = re.fullmatch(r"/api/meetings/([^/]+)/audio\.m4a", path)
        if m:
            q = parse_qs(url.query)
            token = q.get("t", [""])[0]
            if not (token and hmac.compare_digest(token, self.app.token)):
                return self._json(401, {"error": "pair this device first"})
            try:
                audio = self.app.meeting_audio(m.group(1))
            except KeyError:
                return self._json(404, {"error": "not found"})
            extra = None
            if q.get("download"):
                from urllib.parse import quote
                extra = {"Content-Disposition": f"attachment; filename=\"meeting.m4a\"; filename*=UTF-8''{quote(m.group(1))}.m4a"}
            return self._send_file(audio, "audio/mp4", extra)
        if path in STATIC:
            name, ctype = STATIC[path]
            f = WEB / name
            if not f.exists():
                return self._json(404, {"error": "not found"})
            extra = {"Service-Worker-Allowed": "/"} if name == "sw.js" else None
            return self._send(200, f.read_bytes(), ctype, extra)
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return self._json(401, {"error": "pair this device first"})
        try:
            self._api_get(path)
        except KeyError:
            self._json(404, {"error": "not found"})
        except Exception as exc:
            log.warning("api error %s: %s", path, exc)
            self._json(500, {"error": str(exc)})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        if self.path.split("?")[0] == "/api/pair/claim":
            if not self.lan:
                return self._json(404, {"error": "not found"})
            try:
                return self._json(200, self.app.claim(self._json_body().get("code", "")))
            except PermissionError as exc:
                return self._json(429 if "attempts" in str(exc) else 403, {"error": str(exc)})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
        if not self._authed():
            return self._json(401, {"error": "pair this device first"})
        path = urlparse(self.path).path
        try:
            self._api_post(path)
        except KeyError:
            self._json(404, {"error": "not found"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            log.warning("api error %s: %s", path, exc)
            self._json(500, {"error": str(exc)})

    def _manifest(self, token: str) -> None:
        start = "/"
        if token and hmac.compare_digest(token, self.app.token):
            start = f"/?t={token}"  # installed phone apps get their own storage on iOS
        manifest = {
            "name": "Meetnotes", "short_name": "Meetnotes", "start_url": start, "scope": "/",
            "display": "standalone", "background_color": "#0f1115", "theme_color": "#0f1115",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
        }
        self._send(200, json.dumps(manifest).encode(), "application/manifest+json")

    def _api_get(self, path: str) -> None:
        app = self.app
        if path == "/api/status":
            data = app.status()
            data["device"] = "phone" if self.lan else "computer"
            return self._json(200, data)
        if path == "/api/meetings":
            return self._json(200, {"meetings": app.meetings()})
        m = re.fullmatch(r"/api/meetings/([^/]+)", path)
        if m:
            return self._json(200, app.meeting(m.group(1)))
        if path == "/api/settings":
            return self._json(200, app.settings())
        if path == "/api/briefings":
            return self._json(200, {"briefings": app.briefings()})
        m = re.fullmatch(r"/api/briefings/([^/]+)", path)
        if m:
            from .assistant import load_briefing
            return self._json(200, app.briefing_dict(load_briefing(app.cfg.notes_root, m.group(1))))
        if path == "/api/live":
            return self._json(200, app.live_state())
        if path == "/api/calendar":
            return self._json(200, app.calendar_state())
        if path == "/api/update":
            from .update import check
            return self._json(200, check(app.cfg.update_url))
        if path == "/api/speech":
            from .whisperproc import info
            return self._json(200, info(app.cfg))
        if path == "/api/diagnostics":
            from .whisperproc import diagnostics_text
            return self._json(200, {"text": diagnostics_text(app.cfg)})
        if path == "/api/tasks":
            from .tasks import all_tasks
            return self._json(200, {"tasks": all_tasks(app.cfg.notes_root)})
        if path == "/api/templates":
            from .templates import all_templates
            return self._json(200, {"templates": [{"id": k, "name": v["name"], "sections": v["sections"]}
                                                  for k, v in all_templates(app.cfg.templates).items()],
                                    "default": app.cfg.notes_template})
        if path == "/api/pair":
            if self.lan:  # the pairing code is only shown on the computer itself
                return self._json(403, {"error": "only on the computer"})
            url = app.phone_url()
            android = app.android_url()
            code, expires = app.pair_code() if app.lan is not None else ("", 0)
            return self._json(200, {"enabled": app.lan is not None, "url": url,
                                    "qr_svg": qr_svg(url) if url else "",
                                    "android_url": android, "android_qr_svg": qr_svg(android) if android else "",
                                    "code": code, "code_expires_in": expires,
                                    "address": f"{lan_ip()}" + ("" if not app.lan or app.lan.server_address[1] == 47801
                                                                else f":{app.lan.server_address[1]}"),
                                    "fingerprint": app.cert_fp, "error": app.lan_error, "ip": lan_ip()})
        m = re.fullmatch(r"/api/phone/([^/]+)", path)
        if m:
            return self._json(200, app.phone.status(m.group(1)))
        raise KeyError(path)

    def _api_post(self, path: str) -> None:
        app = self.app
        m = re.fullmatch(r"/api/phone/([^/]+)/chunk", path)
        if m:  # raw audio bytes, not JSON
            from .phone import MAX_CHUNK
            total = app.phone.chunk(m.group(1), self._body(limit=MAX_CHUNK))
            return self._json(200, {"bytes": total})
        data = self._json_body()
        if path == "/api/auto":
            service.set_auto(bool(data.get("on")))
            return self._json(200, app.status())
        if path == "/api/record":
            if data.get("on"):
                service.force_record(str(data.get("title") or "").strip()[:120])
            else:
                service.request_skip()
            return self._json(200, app.status())
        if path == "/api/pause":
            service.pause() if data.get("on") else service.resume()
            return self._json(200, app.status())
        if path == "/api/settings":
            errors = app.update_settings(data)
            return self._json(200 if not errors else 400, {"errors": errors, "settings": app.settings()})
        if path == "/api/ask":
            question = str(data.get("question", "")).strip()
            if not question:
                raise ValueError("empty question")
            from .library import ask_context
            docs = ask_context(app.cfg.notes_root, last=int(data.get("last") or 20))
            if not docs:
                return self._json(200, {"answer": "Ei vielä kokouksia, joista kysyä."})
            ask = app.ask_fn
            if ask is None:
                from .summarize import ask
            answer = ask(question, docs, app.cfg.claude_model)
            return self._json(200, {"answer": answer})
        if path == "/api/briefings":
            b = app.save_briefing(data)
            return self._json(200, app.briefing_dict(b))
        m = re.fullmatch(r"/api/briefings/([^/]+)/delete", path)
        if m:
            from .assistant import delete_briefing
            delete_briefing(app.cfg.notes_root, m.group(1))
            return self._json(200, {"deleted": m.group(1)})
        m = re.fullmatch(r"/api/briefings/([^/]+)/prepare", path)
        if m:
            app.prepare(m.group(1))
            return self._json(202, {"started": m.group(1)})
        m = re.fullmatch(r"/api/briefings/([^/]+)/chat", path)
        if m:
            q = str(data.get("question", "")).strip()
            if not q:
                raise ValueError("empty question")
            return self._json(200, {"answer": app.briefing_chat(m.group(1), q)})
        if path == "/api/live/finish-now":
            s = live.current()
            if s is None:
                return self._json(409, {"error": "no meeting is being recorded"})
            s.abandon()
            return self._json(200, {"ok": True})
        if path == "/api/live/notes":
            s = live.current()
            if s is None:
                return self._json(409, {"error": "no meeting is being recorded"})
            s.set_my_notes(str(data.get("text", ""))[:200_000])
            return self._json(200, {"saved": True})
        if path == "/api/live/template":
            s = live.current()
            if s is None:
                return self._json(409, {"error": "no meeting is being recorded"})
            from .templates import all_templates
            t = str(data.get("template", ""))
            if t not in all_templates(app.cfg.templates):
                raise ValueError("unknown template")
            s.template = t
            return self._json(200, {"template": t})
        if path == "/api/update/check":
            from .update import check
            return self._json(200, check(app.cfg.update_url, force=True))
        if path == "/api/speech/test":
            from .whisperproc import test_in_background
            return self._json(202, {"started": test_in_background(app.cfg)})
        if path == "/api/speech/reset":
            from .whisperproc import reset, test_in_background
            reset(app.cfg)
            return self._json(202, {"started": test_in_background(app.cfg)})
        if path == "/api/update/apply":
            from .update import apply_in_background, check
            st = check(app.cfg.update_url)
            if not st["available"]:
                raise ValueError("no update available")
            started = apply_in_background(app.cfg.update_url)
            return self._json(202 if started else 409, {"started": started})
        if path == "/api/calendar/refresh":
            if app.calendar is not None:
                app.calendar.refresh(force=True)
            return self._json(200, app.calendar_state())
        m = re.fullmatch(r"/api/calendar/([^/]+)/briefing", path)
        if m:
            from urllib.parse import unquote
            b = app.briefing_from_event(unquote(m.group(1)))
            return self._json(200, app.briefing_dict(b))
        m = re.fullmatch(r"/api/tasks/([0-9a-f]{12})", path)
        if m:
            from .tasks import set_done
            set_done(app.cfg.notes_root, m.group(1), bool(data.get("done")))
            return self._json(200, {"id": m.group(1), "done": bool(data.get("done"))})
        if path == "/api/live/ask":
            s = live.current()
            if s is None:
                return self._json(409, {"error": "no meeting is being recorded"})
            q = str(data.get("question", "")).strip()
            if not q:
                raise ValueError("empty question")
            return self._json(200, {"answer": s.ask(q), "assistant": s.assistant_snapshot()})
        if path == "/api/live/enable":
            s = live.current()
            if s is None:
                return self._json(409, {"error": "no meeting is being recorded"})
            if data.get("on"):
                if s.assistant is None:
                    s.start_assistant(s.briefing or s.find_briefing())
            else:
                s.stop_assistant()
            return self._json(200, app.live_state())
        if path == "/api/live/attach":
            s = live.current()
            if s is None:
                return self._json(409, {"error": "no meeting is being recorded"})
            from .assistant import load_briefing
            s.start_assistant(load_briefing(app.cfg.notes_root, str(data.get("briefing_id", ""))))
            return self._json(200, app.live_state())
        m = re.fullmatch(r"/api/meetings/([^/]+)/summarize", path)
        if m:
            app.summarize(m.group(1))
            return self._json(202, {"started": m.group(1)})
        m = re.fullmatch(r"/api/meetings/([^/]+)/translate", path)
        if m:
            from .session import translate_in_background
            from .translate import lang_name
            code = str(data.get("lang", ""))
            lang_name(code)  # ValueError -> 400
            folder = app.meeting_folder(m.group(1))
            if not (folder / "transcript.md").exists() and not (folder / "notes.md").exists():
                raise ValueError("nothing to translate yet")
            translate_in_background(folder, app.cfg, code)
            return self._json(202, {"started": code})
        if path == "/api/live/translate":
            s = live.current()
            if s is None:
                return self._json(409, {"error": "no meeting is being recorded"})
            code = str(data.get("lang", ""))
            if data.get("on", True):
                s.start_translation(code)
            else:
                s.stop_translation()
            return self._json(200, app.live_state())
        m = re.fullmatch(r"/api/meetings/([^/]+)/reveal", path)
        if m:
            if self.lan:
                raise ValueError("only on the computer")
            app.meeting_audio(m.group(1))
            app.reveal(m.group(1))
            return self._json(200, {"ok": True})
        m = re.fullmatch(r"/api/meetings/([^/]+)/delete", path)
        if m:
            folder = app.meeting_folder(m.group(1))
            shutil.rmtree(folder)
            return self._json(200, {"deleted": m.group(1)})
        if path == "/api/phone/start":
            sid = app.phone.start(str(data.get("title") or "").strip()[:120], str(data.get("mime") or ""))
            return self._json(200, {"id": sid})
        m = re.fullmatch(r"/api/phone/([^/]+)/finish", path)
        if m:
            app.phone.finish(m.group(1))
            return self._json(200, app.phone.status(m.group(1)))
        raise KeyError(path)


def qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return img.to_string(encoding="unicode")

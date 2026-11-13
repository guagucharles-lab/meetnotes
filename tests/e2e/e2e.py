"""Meetnotes end-to-end test: real service process, real audio devices, real
window detection, real browser; Claude and Whisper are stand-ins."""
import hashlib, json, os, re, signal, socket, ssl, subprocess, sys, threading, time, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

D = Path(sys.argv[1]); ROOT = Path("/home/user/meetnotes")
for sub in ("state", "notes", "shots"):
    (D / sub).mkdir(parents=True, exist_ok=True)
RESULTS = []
def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)

APP, PHONE, MOCK, CAL, HOOK = 47960, 47961, 47970, 47972, 47973
ENV = {**os.environ, "MEETNOTES_STATE_DIR": str(D / "state"), "MEETNOTES_E2E_TRANSCRIBER": str(D / "script.txt"),
       "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{MOCK}", "ANTHROPIC_API_KEY": "e2e-key", "PYTHONPATH": str(ROOT),
       "XDG_CONFIG_HOME": str(D / "xdgconfig"),
       # the PulseAudio client finds its server and cookie through XDG_CONFIG_HOME; keep the real ones
       "PULSE_COOKIE": os.environ.get("PULSE_COOKIE") or str(Path.home() / ".config" / "pulse" / "cookie")}
_srv = re.search(r"Server String: (\S+)", subprocess.run(["pactl", "info"], capture_output=True, text=True).stdout)
if _srv and not ENV.get("PULSE_SERVER"):
    ENV["PULSE_SERVER"] = "unix:" + _srv.group(1) if _srv.group(1).startswith("/") else _srv.group(1)
SCRIPT = D / "script.txt"
def script(*lines):
    SCRIPT.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ---- mock Claude, calendar feed, webhook receiver
(D / "requests.jsonl").unlink(missing_ok=True)
mock = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "mock_claude.py"), str(MOCK), str(D / "requests.jsonl")])
now = datetime.now()
def ics_time(t): return t.strftime("%Y%m%dT%H%M%S")
ICS = f"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//e2e//EN\r\nBEGIN:VEVENT\r\nUID:kontio-e2e\r\nDTSTART:{ics_time(now - timedelta(minutes=5))}\r\nDTEND:{ics_time(now + timedelta(minutes=55))}\r\nSUMMARY:Kontio Oy – sopimusneuvottelu\r\nDESCRIPTION:1. Hinta ja maksuehdot\\n2. Toimitusaika\r\nATTENDEE;CN=Jari Kontio:mailto:jari@example.com\r\nATTENDEE;CN=Mira Lahti:mailto:mira@example.com\r\nLOCATION:https://zoom.us/j/123456\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:budget-e2e\r\nDTSTART:{ics_time(now + timedelta(days=1))}\r\nDTEND:{ics_time(now + timedelta(days=1, hours=1))}\r\nSUMMARY:Budjettikatsaus\r\nDESCRIPTION:- Q4 toteuma\\n- Raamit\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
HOOKS = []
class Srv(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = ICS.encode(); self.send_response(200); self.send_header("Content-Type", "text/calendar"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        HOOKS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204); self.end_headers()
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", CAL), Srv).serve_forever, daemon=True).start()
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", HOOK), Srv).serve_forever, daemon=True).start()

# ---- config
sys.path.insert(0, str(ROOT))
os.environ.update({k: ENV[k] for k in ("MEETNOTES_STATE_DIR", "XDG_CONFIG_HOME")})
from meetnotes.config import Config, save
CFG = D / "config.toml"
save(Config(notes_dir=str(D / "notes"), app_port=APP, phone_port=PHONE, phone_access=True,
            calendar_urls=[f"http://127.0.0.1:{CAL}/cal.ics"], assistant_live=True, assistant_interval=15,
            watch_words=["kilpailija"], webhook_url=f"http://127.0.0.1:{HOOK}/hook", chunk_seconds=8,
            whisper_model="small", transcribe_mode="mixed", poll_seconds=2, end_grace_seconds=6,
            idle_stop_minutes=0, keep_audio=True, record_screen=True, screen_fps=2, tray_icon=False), CFG)

def cli(*args, timeout=200):
    return subprocess.run([sys.executable, "-m", "meetnotes.cli", "--config", str(CFG), *args], env=ENV, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=timeout)
TOKEN = None
def api(path, body=None, port=APP, tls=False, token=True, timeout=30):
    url = f"{'https' if tls else 'http'}://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"X-Meetnotes-Token": TOKEN} if token and TOKEN else {})},
                                 method="GET" if body is None else "POST")
    ctx = ssl._create_unverified_context() if tls else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
def wait(cond, timeout, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None
def play(dev, wav): subprocess.run(["paplay", f"--device={dev}", str(D / wav)], env=ENV)
def node(js, timeout=240):
    f = D / "step.js"; f.write_text(js)
    r = subprocess.run(["node", str(f)], env={**ENV, "NODE_PATH": subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()},
                       capture_output=True, text=True, timeout=timeout)
    out = (r.stdout + r.stderr).strip()
    try:
        return json.loads(out.splitlines()[-1])
    except Exception:
        return {"error": out[-1500:]}

# ---- audio clips
for name, text in (("me.wav", "Sovitaan, että lähetän raportin perjantaina."), ("others.wav", "Hyvä. Kilpailija tarjosi halvemmalla, mutta katsotaan tulokset maanantaina.")):
    if not (D / name).exists():
        subprocess.run(["espeak-ng", "-v", "fi", "-s", "140", "-w", str(D / name), text])

try:
    # ================================================================ service
    r = cli("start")
    check("service starts in the background", r.returncode == 0, r.stdout + r.stderr)
    check("app answers", wait(lambda: urllib.request.urlopen(f"http://127.0.0.1:{APP}/api/ping", timeout=2).status == 200, 20))
    TOKEN = (D / "state" / "app_token").read_text().strip()
    st = api("/api/status")[1]
    check("status watching", st.get("state") == "watching", st)
    cal = wait(lambda: (lambda c: c if c["events"] else None)(api("/api/calendar")[1]), 20)
    check("calendar feed parsed", cal and cal["events"][0]["title"].startswith("Kontio") and cal["events"][0]["now"], cal)

    # ================================================================ briefing before the meeting
    s, b = api("/api/briefings", {"title": "Kontio-palaveri", "when": now.strftime("%Y-%m-%d %H:%M"), "agenda": "Hinta ja maksuehdot\nToimitusaika", "participants": "Jari Kontio"})
    check("briefing created", s == 200 and b["agenda"] == ["Hinta ja maksuehdot", "Toimitusaika"], b)
    api(f"/api/briefings/{b['id']}/prepare", {})
    bb = wait(lambda: (lambda x: x if x["prep_status"] == "done" else None)(api(f"/api/briefings/{b['id']}")[1]), 30)
    check("briefing prepared by Claude", bb and "## Tilanne ennen kokousta" in bb["prep"], bb and bb.get("prep_error"))
    s, c = api(f"/api/briefings/{b['id']}/chat", {"question": "Mitä hinnasta on sovittu?"})
    check("pre-meeting question answered", s == 200 and "48 000" in c.get("answer", ""), c)

    # ================================================================ automatic meeting from a Zoom window
    script(*[f"Puheenvuoro {i}: Kilpailija tarjosi halvemmalla, joten lähetän raportin perjantaina." if i % 3 == 0 else f"Puheenvuoro {i}: käydään tuloksia läpi ja sovitaan aikataulusta." for i in range(40)])
    zoom = subprocess.Popen(["/usr/bin/python3.12", "-c", "import tkinter as t; r=t.Tk(); r.title('Zoom Meeting'); r.geometry('900x600+100+100'); t.Label(r, text='ZOOM TEST MEETING', font=('Arial', 40)).pack(expand=1); r.mainloop()"], env=ENV)
    st = wait(lambda: (lambda x: x if x.get("state") == "recording" else None)(api("/api/status")[1]), 30)
    check("meeting window detected, recording started", st, api("/api/status")[1])
    check("meeting named from the calendar", st and st.get("meeting") == "Kontio Oy – sopimusneuvottelu", st and st.get("meeting"))
    lv = wait(lambda: (lambda x: x if x.get("active") and x.get("enabled") else None)(api("/api/live")[1]), 10)
    check("assistant on with the matching briefing", lv and lv.get("briefing") and lv["briefing"]["title"] == "Kontio-palaveri", lv)
    check("calendar attendees attached", lv and lv.get("event") and lv["event"]["attendees"] == ["Jari Kontio", "Mira Lahti"], lv and lv.get("event"))

    browser_live = threading.Thread(target=lambda: RESULTS.append(("_browser_live", True, node(f"""
const {{ chromium }} = require("playwright");
(async () => {{
  const b = await chromium.launch(); const p = await b.newPage({{ viewport: {{ width: 1200, height: 900 }} }});
  const errs = []; p.on("pageerror", (e) => errs.push(e.message));
  await p.goto("http://127.0.0.1:{APP}/?t={TOKEN}");
  await p.waitForSelector("#live-card:not([hidden])", {{ timeout: 20000 }});
  await p.waitForSelector("#notepad-card:not([hidden])");
  await p.fill("#notepad", "Jari haluaa toimituksen tammikuussa.\\nMuista kysyä tuesta.");
  await p.waitForSelector("text=Tallennettu", {{ timeout: 8000 }});
  await p.waitForSelector(".nudge.keyword", {{ timeout: 60000 }});
  await p.waitForSelector(".nudge.heads_up, .nudge.action", {{ timeout: 60000 }});
  await p.fill("#live-q", "Mitä on sovittu toimituksesta?");
  await p.click("#live-ask");
  await p.waitForSelector(".nudge.answer", {{ timeout: 30000 }});
  const answer = await p.textContent(".nudge.answer .text");
  const agenda = await p.$$eval(".agenda span", (s) => s.map((x) => x.textContent));
  // live translation: switch on, captions arrive in English
  await p.waitForSelector("#translate-card:not([hidden]) #tr-on", {{ state: "attached" }});
  await p.click("#translate-card .switch");
  await p.waitForFunction(() => [...document.querySelectorAll("#tr-lines .tline")].some((x) => x.textContent.includes("EN: ")), null, {{ timeout: 30000 }});
  const captions = await p.$$eval("#tr-lines .tline", (r) => r.map((x) => x.textContent.replace(/\s+/g, " ").trim()));
  await p.screenshot({{ path: "{D}/shots/live.png", fullPage: true }});
  console.log(JSON.stringify({{ answer, agenda, captions, errs }}));
  await b.close();
}})().catch((e) => {{ console.log(JSON.stringify({{ error: e.message }})); process.exit(0); }});
""", timeout=180))))
    browser_live.start()
    for i in range(5):
        play("micsink", "me.wav"); time.sleep(1); play("speakers", "others.wav"); time.sleep(1)
    browser_live.join(200)
    bl = next((x[2] for x in RESULTS if x[0] == "_browser_live"), {"error": "browser step did not finish"})
    check("browser: notepad saved, keyword alert, assistant nudge, live answer",
          not bl.get("error") and bl.get("answer", "").startswith("Vastaus: Mitä on sovittu toimituksesta") and not bl.get("errs"), bl)
    check("browser: agenda chips from the briefing", bl.get("agenda") and bl["agenda"][0].startswith("Hinta ja maksuehdot"), bl.get("agenda"))
    check("live translation captions during the meeting", any("EN: Puheenvuoro" in c for c in bl.get("captions") or []), (bl.get("captions") or [])[:3])
    zoom.terminate(); zoom.wait()
    folder = (Path(D / "notes") / st["folder_id"]) if st and st.get("folder_id") else None
    done = wait(lambda: folder and (folder / "notes.md").exists() and api("/api/status")[1].get("state") == "watching", 120)
    check("window closed -> meeting ends and notes are written", done, api("/api/status")[1])
    if folder and folder.exists():
        tr = (folder / "transcript.md").read_text()
        check("transcript has both speakers (labels from channel loudness)", "] Me: " in tr and "] Others: " in tr, tr[:400])
        notes = (folder / "notes.md").read_text()
        check("notes are Finnish with chapters", "## Yhteenveto" in notes and "## Aiheet" in notes and "## Tehtävät" in notes, notes[:300])
        check("my own notes saved", (folder / "my-notes.md").read_text().startswith("Jari haluaa toimituksen"))
        meta = json.loads((folder / "meta.json").read_text())
        check("talk statistics computed", meta.get("stats", {}).get("talk_me_pct") is not None, meta.get("stats"))
        check("calendar event stored", meta.get("event", {}).get("attendees") == ["Jari Kontio", "Mira Lahti"])
        aj = json.loads((folder / "assistant.json").read_text()) if (folder / "assistant.json").exists() else {}
        kinds = {f["kind"] for f in aj.get("feed", [])}
        check("assistant feed saved (nudges, question, answer)", {"question", "answer"} <= kinds and kinds & {"heads_up", "action"}, kinds)
        lt = folder / "live-translation.en.md"
        check("live translation saved with the meeting", lt.exists() and "EN: " in lt.read_text(), lt.exists())
        check("screen recording with audio", (folder / "screen.mp4").exists() and (folder / "screen.mp4").stat().st_size > 10_000)
        try:
            import av
            with av.open(str(folder / "screen.mp4")) as cnt:
                types = sorted(s.type for s in cnt.streams)
            check("screen.mp4 has video and audio streams", types == ["audio", "video"], types)
        except Exception as e:
            check("screen.mp4 readable", False, str(e))
        reqs = [json.loads(l) for l in (D / "requests.jsonl").read_text().splitlines()]
        notes_req = [r for r in reqs if "chapters" in json.dumps(r["body"].get("output_config", {}))]
        last = json.dumps(notes_req[-1]["body"], ensure_ascii=False) if notes_req else ""
        check("notes request carries briefing, my notes, attendees, agenda status",
              all(t in last for t in ("<briefing>", "<my_notes>", "<attendees>", "<agenda_status>")), [t for t in ("<briefing>", "<my_notes>", "<attendees>", "<agenda_status>") if t not in last])
        live_reqs = [r for r in reqs if "nudges" in json.dumps(r["body"].get("output_config", {}))]
        roles = [m["role"] for m in live_reqs[-1]["body"]["messages"]] if live_reqs else []
        check("live assistant is one growing conversation", len(live_reqs) >= 2 and roles[:2] == ["user", "assistant"] and roles[-1] == "user", roles)
        check("requests use fallbacks + beta header", all(r["body"].get("fallbacks") == "default" and "server-side-fallback" in r["headers"].get("anthropic-beta", "") for r in reqs), "")
        check("webhook received the notes", wait(lambda: HOOKS and HOOKS[-1]["notes"]["title"], 10), HOOKS[-1:] if HOOKS else "none")
        check("briefing marked as used", api(f"/api/briefings/{b['id']}")[1].get("used_by") == folder.name)

    # ================================================================ manual recording, pause, safe word
    script(*["Aloitetaan manuaalinen testi ja käydään asiat läpi rauhassa."] * 20)
    api("/api/record", {"on": True, "title": "Manuaalitesti"})
    st2 = wait(lambda: (lambda x: x if x.get("state") == "recording" and x.get("meeting") == "Manuaalitesti" else None)(api("/api/status")[1]), 15)
    check("record now starts a manual recording", st2, api("/api/status")[1])
    play("micsink", "me.wav"); play("micsink", "me.wav"); time.sleep(9)
    api("/api/pause", {"on": True})
    check("pause shows as paused", wait(lambda: api("/api/status")[1].get("state") == "paused", 6))
    script(*["SALAINEN asia jota ei saa tallentaa."] * 20)
    play("speakers", "others.wav"); time.sleep(2)
    script(*["okei omerta"] * 20)
    api("/api/pause", {"on": False})
    check("resume", wait(lambda: api("/api/status")[1].get("state") == "recording", 6))
    play("micsink", "me.wav"); time.sleep(1); play("micsink", "me.wav")
    f2 = (Path(D / "notes") / st2["folder_id"]) if st2 and st2.get("folder_id") else None
    ended = wait(lambda: api("/api/status")[1].get("state") in ("watching", "auto-off") and f2 and (f2 / "meta.json").exists()
                 and json.loads((f2 / "meta.json").read_text()).get("status") in ("done", "transcribed", "too_short"), 90)
    check("safe word ends the manual recording", ended, api("/api/status")[1])
    if f2 and f2.exists():
        tr2 = (f2 / "transcript.md").read_text()
        check("pause markers in transcript", "(recording paused)" in tr2 and "(recording resumed)" in tr2, tr2)
        check("nothing said during the pause was kept", "SALAINEN" not in tr2)
        check("safe word cut: marker and no 'omerta' text", "(recording stopped by safe word)" in tr2 and "omerta" not in tr2.lower(), tr2)
        meta2 = json.loads((f2 / "meta.json").read_text())
        check("safe word time recorded", meta2.get("safe_word_at") is not None, meta2)
        check("manual recording did not borrow calendar attendees", "event" not in meta2, meta2.get("event"))

    # ================================================================ browser: meetings, tasks, assistant, settings
    ui = node(f"""
const {{ chromium }} = require("playwright");
(async () => {{
  const b = await chromium.launch(); const ctx = await b.newContext({{ viewport: {{ width: 1200, height: 900 }} }});
  await ctx.grantPermissions(["clipboard-read", "clipboard-write"], {{ origin: "http://127.0.0.1:{APP}" }});
  const p = await ctx.newPage(); const errs = []; p.on("pageerror", (e) => errs.push(e.message)); p.on("console", (m) => m.type() === "error" && errs.push(m.text()));
  const out = {{}};
  await p.goto("http://127.0.0.1:{APP}/?t={TOKEN}#/meetings");
  await p.waitForSelector(".item");
  out.list = await p.$$eval(".item .item-title", (t) => t.map((x) => x.textContent));
  out.badges = await p.$$eval(".item .badge", (t) => t.map((x) => x.textContent));
  await p.click('a.item[href*="Kontio-Oy"]');
  await p.waitForSelector(".md h2");
  out.tabs = await p.$$eval("[data-tab]", (t) => t.map((x) => x.textContent));
  out.stats = (await p.textContent(".stats")).replace(/\\s+/g, " ").trim();
  await p.click(".chapters button >> nth=1");
  out.chapterJump = await p.$eval("[data-tab=transcript]", (e) => e.getAttribute("aria-selected"));
  await p.click("[data-tab=mine]"); out.mine = (await p.textContent("#content")).trim().slice(0, 30);
  // whole-meeting audio: plays, and a transcript timestamp seeks it
  await p.click("[data-tab=transcript]");
  await p.$eval("#rec-audio", (a) => a.load());
  await p.waitForFunction(() => {{ const a = document.querySelector("#rec-audio"); return a && a.readyState >= 1 && a.duration > 0; }}, null, {{ timeout: 30000 }});
  out.audio = await p.$eval("#rec-audio", (a) => a.duration);
  await p.click(".tline .ts >> nth=2");
  out.audioSeek = await p.$eval("#rec-audio", (a) => a.currentTime);
  out.audioSeekTo = await p.textContent(".tline .ts >> nth=2");
  out.reveal = await p.$$eval("#reveal", (b) => b.length);
  // translate the finished meeting into English and switch the view
  await p.selectOption("#tr-target", "en"); await p.click("#tr-go");
  await p.waitForSelector('#lang-chips [data-lang="en"]', {{ timeout: 40000 }});
  await p.click('#lang-chips [data-lang="en"]');
  await p.click("[data-tab=transcript]");
  out.trTranscript = (await p.textContent("#content")).slice(0, 300);
  await p.click("[data-tab=notes]");
  out.trNotes = (await p.textContent("#content")).slice(0, 300);
  await p.click('#lang-chips [data-lang=""]');
  out.trBack = (await p.textContent("#content")).slice(0, 120);
  await p.click("[data-tab=video]");
  await p.waitForFunction(() => {{ const v = document.querySelector("video.screen"); return v && v.readyState >= 1 && v.duration > 0; }}, null, {{ timeout: 20000 }});
  out.video = await p.$eval("video.screen", (v) => v.duration);
  await p.click("#copy"); out.clip = (await p.evaluate(() => navigator.clipboard.readText())).slice(0, 20);
  await p.screenshot({{ path: "{D}/shots/meeting.png", fullPage: true }});
  // delete the manual (safe word) meeting with the in-page confirmation
  await p.goto("http://127.0.0.1:{APP}/#/meetings"); await p.waitForSelector(".item");
  const before = await p.$$eval(".item", (x) => x.length);
  await p.click('a.item[href*="Manuaalitesti"]'); await p.waitForSelector("#del");
  await p.click("#del"); await p.click("#del-no"); await p.waitForSelector("#del");
  await p.click("#del"); await p.click("#del-yes"); await p.waitForSelector("h1:text('Kokoukset')"); await p.waitForSelector(".item");
  out.deleted = before - (await p.$$eval(".item", (x) => x.length));
  // tasks
  await p.goto("http://127.0.0.1:{APP}/#/tasks"); await p.waitForSelector(".task");
  out.tasks = await p.$$eval(".task .t > div:first-child", (t) => t.map((x) => x.textContent));
  await p.click(".task input >> nth=0"); await p.waitForTimeout(700);
  await p.reload(); await p.waitForSelector("[data-f=done]"); await p.click("[data-f=done]"); await p.waitForTimeout(300);
  out.doneAfterReload = await p.$$eval(".task.done", (x) => x.length);
  // assistant: calendar -> prepare -> chat; ask across meetings
  await p.goto("http://127.0.0.1:{APP}/#/assistant"); await p.waitForSelector("#cal-card .event");
  out.calendar = await p.$$eval("#cal-card .ttl", (t) => t.map((x) => x.textContent));
  await p.click("#cal-card [data-prep] >> nth=0");
  await p.waitForSelector("#brief-body h2", {{ timeout: 30000 }}); out.prepTitle = await p.textContent("h1");
  out.prepAgenda = await p.$$eval("ol li", (l) => l.map((x) => x.textContent));
  await p.fill("#chat-q", "Mitä hinnasta on sovittu?"); await p.click("#chat-btn");
  await p.waitForFunction(() => [...document.querySelectorAll(".bubble.assistant")].some((e) => e.textContent.includes("48 000")), null, {{ timeout: 20000 }});
  await p.goto("http://127.0.0.1:{APP}/#/assistant"); await p.waitForSelector("#q2");
  await p.fill("#q2", "Mitä lupasin?"); await p.click("#ask-btn2");
  await p.waitForFunction(() => document.querySelector("#answer2").textContent.includes("perjantaina"), null, {{ timeout: 20000 }});
  out.ask = await p.textContent("#answer2");
  // settings: save + pairing code + update card
  await p.goto("http://127.0.0.1:{APP}/#/settings"); await p.waitForSelector("#notes_template", {{ state: "attached" }});
  await p.selectOption("#notes_template", "sales"); await p.fill("#watch_words", "kilpailija, alennus");
  await p.click("#save"); await p.waitForSelector("text=Tallennettu", {{ timeout: 8000 }});
  await p.waitForSelector(".pair-code .code", {{ timeout: 10000 }}); out.code = (await p.textContent(".pair-code .code")).replace(/\\s/g, "");
  out.updApplyHidden = await p.$eval("#upd-apply", (e) => getComputedStyle(e).display === "none");
  // speech engine card: self-test and log
  await p.click("#sp-test");
  await p.waitForFunction(() => document.querySelector("#speech-body").textContent.includes("Toimii"), null, {{ timeout: 60000 }});
  out.speech = await p.textContent("#speech-body");
  await p.click("#sp-log"); await p.waitForSelector("#sp-log-text", {{ state: "visible" }});
  out.log = await p.textContent("#sp-log-text");
  await p.screenshot({{ path: "{D}/shots/settings.png", fullPage: true }});
  out.errs = errs;
  console.log(JSON.stringify(out)); await b.close();
}})().catch((e) => {{ console.log(JSON.stringify({{ error: e.message }})); process.exit(0); }});
""")
    check("browser UI walkthrough ran", not ui.get("error"), ui.get("error"))
    check("meetings list + badges", ui.get("list") and "Kontio: hinta ja toimitus" in ui["list"], ui.get("list"))
    check("meeting tabs incl. own notes and video", ui.get("tabs") == ["Muistiinpanot", "Teksti", "Omat", "Video"], ui.get("tabs"))
    check("stats line shown", "Puheaika" in ui.get("stats", ""), ui.get("stats"))
    check("chapter click jumps to transcript", ui.get("chapterJump") == "true")
    check("video plays in the app", (ui.get("video") or 0) > 1, ui.get("video"))
    check("meeting audio plays in the app", (ui.get("audio") or 0) > 1, ui.get("audio"))
    _to = sum(int(x) * 60 ** i for i, x in enumerate(reversed((ui.get("audioSeekTo") or "0").split(":"))))
    check("transcript timestamp seeks the audio", abs((ui.get("audioSeek") or -9) - _to) < 1.5, (ui.get("audioSeek"), ui.get("audioSeekTo")))
    check("show-file button on the computer", ui.get("reveal") == 1)
    check("translate a meeting: English transcript", "EN: " in (ui.get("trTranscript") or ""), ui.get("trTranscript"))
    check("translate a meeting: English notes, original kept", "Yhteenveto" not in (ui.get("trNotes") or "x") and "EN: " not in (ui.get("trBack") or ""), (ui.get("trNotes"), ui.get("trBack")))
    _en = list((D / "notes").glob("*Kontio*/transcript.en.md"))
    check("translation files next to the originals", bool(_en) and (_en[0].parent / "notes.en.md").exists())
    _m4a = list((D / "notes").glob("*Kontio*/audio.m4a"))
    check("audio.m4a written and small", bool(_m4a) and _m4a[0].stat().st_size < (_m4a[0].parent / "audio.wav").stat().st_size / 3,
          [(f, f.stat().st_size) for f in _m4a])
    check("copy puts notes on the clipboard", (ui.get("clip") or "").startswith("# Kontio"), ui.get("clip"))
    check("delete with in-page confirmation", ui.get("deleted") == 1, ui.get("deleted"))
    check("tasks from notes listed", ui.get("tasks") and "Lähetä raportti" in ui["tasks"], ui.get("tasks"))
    check("task done persists after reload", (ui.get("doneAfterReload") or 0) >= 1)
    check("calendar on the assistant page", ui.get("calendar") and ui["calendar"][0].startswith("Kontio"), ui.get("calendar"))
    check("prepare from a calendar event (agenda from invite)", ui.get("prepAgenda") == ["Hinta ja maksuehdot", "Toimitusaika"], ui.get("prepAgenda"))
    check("ask across meetings", "perjantaina" in (ui.get("ask") or ""))
    check("settings saved to disk", 'notes_template = "sales"' in CFG.read_text() and "alennus" in CFG.read_text())
    check("pairing code shown", re.fullmatch(r"\d{6}", ui.get("code") or ""), ui.get("code"))
    check("no update button without an update", ui.get("updApplyHidden") is True)
    check("speech self-test from settings", "Toimii" in (ui.get("speech") or ""), ui.get("speech"))
    check("log viewer shows both logs", "meetnotes.log" in (ui.get("log") or "") and "speech-worker.log" in (ui.get("log") or ""))
    check("no JavaScript errors in the app", not ui.get("errs"), ui.get("errs"))

    # ================================================================ phone: pair like the Android app, then use it
    s, claim = api("/api/pair/claim", {"code": ui.get("code", "")}, port=PHONE, tls=True, token=False)
    check("Android-style pairing over HTTPS", s == 200 and claim.get("token") == TOKEN and len(claim.get("fingerprint", "")) == 64, claim)
    import ssl as _ssl
    der = _ssl.get_server_certificate(("127.0.0.1", PHONE))
    fp = hashlib.sha256(_ssl.PEM_cert_to_DER_cert(der)).hexdigest()
    check("pinned fingerprint matches the served certificate", claim.get("fingerprint") == fp)
    script(*["Kasvokkain sovittiin toimituksesta helmikuussa, hinnasta, maksuehdoista ja siitä, kuka lähettää sopimuksen ensi viikolla."] * 20)
    ph = node(f"""
const {{ chromium }} = require("playwright");
(async () => {{
  const b = await chromium.launch({{ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] }});
  const ctx = await b.newContext({{ viewport: {{ width: 390, height: 844 }}, isMobile: true, hasTouch: true, ignoreHTTPSErrors: true, permissions: ["microphone"] }});
  const p = await ctx.newPage(); const errs = []; p.on("pageerror", (e) => errs.push(e.message));
  await p.goto("https://127.0.0.1:{PHONE}/?t={TOKEN}");
  await p.waitForSelector("#ph-btn", {{ timeout: 15000 }});
  const device = await p.evaluate(() => document.body.dataset.device);
  await p.fill("#ph-title", "Puhelintesti"); await p.click("#ph-btn");
  await p.waitForFunction(() => document.querySelector("#ph-msg").textContent.startsWith("Nauhoittaa"), null, {{ timeout: 10000 }});
  await p.waitForTimeout(12000); await p.click("#ph-btn");
  await p.waitForURL(/#\\/meetings\\//, {{ timeout: 60000 }}); await p.waitForSelector(".md h2");
  const title = await p.textContent("h1");
  const sw = await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
  await p.screenshot({{ path: "{D}/shots/phone.png" }});
  console.log(JSON.stringify({{ device, title, sw, errs }})); await b.close();
}})().catch((e) => {{ console.log(JSON.stringify({{ error: e.message }})); process.exit(0); }});
""")
    check("phone app: in-person recording -> notes", not ph.get("error") and ph.get("device") == "phone" and ph.get("title"), ph)
    check("phone layout fits the screen", ph.get("sw") is True)

    # ================================================================ restart after update, then stop
    pid1 = int((D / "state" / "watch.pid").read_text())
    (D / "state" / "restart").write_text("e2e")
    new_pid = wait(lambda: (lambda p: p if p and p != pid1 else None)(int((D / "state" / "watch.pid").read_text()) if (D / "state" / "watch.pid").exists() else None), 30)
    check("service restarts itself after an update (no meeting running)", new_pid, "")
    check("app back after restart", wait(lambda: urllib.request.urlopen(f"http://127.0.0.1:{APP}/api/ping", timeout=2).status == 200, 30))
    r = cli("stop")
    check("service stops cleanly", r.returncode == 0 and "Stopped" in r.stdout, r.stdout + r.stderr)
    log = (D / "state" / "meetnotes.log").read_text()
    bad = [l for l in log.splitlines() if "Traceback" in l or " ERROR " in l]
    check("no errors or tracebacks in the service log", not bad, bad[:5])
finally:
    cli("stop", timeout=60)
    mock.terminate()
    passed = sum(1 for n, ok, _ in RESULTS if ok and not n.startswith("_"))
    total = sum(1 for n, _, _ in RESULTS if not n.startswith("_"))
    print(f"\n{passed}/{total} checks passed")

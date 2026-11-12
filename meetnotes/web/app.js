"use strict";
/* Meetnotes app: the same page runs in the desktop window and on the phone. */

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------------------------------------------------------------- pairing
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
let TOKEN = null;
(function pair() {
  if (window.MEETNOTES_DEMO) { TOKEN = "demo"; return; }  // the online demo answers /api itself
  const params = new URLSearchParams(location.search);
  const t = params.get("t");
  if (t) { TOKEN = t; store.set("mn_token", t); history.replaceState(null, "", "/" + location.hash); }
  else TOKEN = store.get("mn_token");
  if (TOKEN) {
    const link = document.createElement("link");
    link.rel = "manifest";
    link.href = "/manifest.webmanifest?t=" + encodeURIComponent(TOKEN);
    document.head.appendChild(link);
  }
  if ("serviceWorker" in navigator && isSecureContext) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();

async function api(path, body, opts = {}) {
  const init = { headers: { "X-Meetnotes-Token": TOKEN || "" }, ...opts };
  if (body !== undefined) {
    init.method = "POST";
    if (body instanceof Blob) { init.body = body; init.headers["Content-Type"] = "application/octet-stream"; }
    else { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  }
  const res = await fetch(path, init);
  let data = {};
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const err = new Error(data.error || (data.errors && Object.values(data.errors).join(", ")) || `HTTP ${res.status}`);
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.remove("show"), 2600);
}

// ---------------------------------------------------------------- helpers
const fmtDuration = (s) => {
  if (!s && s !== 0) return "";
  if (s < 60) return "alle 1 min";
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
};
const fmtClock = (sec) => {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
};
const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const today = new Date(); const y = new Date(); y.setDate(today.getDate() - 1);
  const time = d.toLocaleTimeString("fi-FI", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return `Tänään ${time}`;
  if (d.toDateString() === y.toDateString()) return `Eilen ${time}`;
  return d.toLocaleDateString("fi-FI", { weekday: "short", day: "numeric", month: "numeric" }) + " " + time;
};
const platformName = (p) => ({ Manual: "Käsin aloitettu", Phone: "Puhelin", "Phone (in person)": "Puhelin" }[p] || p || "");
const STATUS_BADGE = {
  done: ["Muistiinpanot", "ok"], recording: ["Nauhoittaa", "rec"], processing: ["Käsitellään", ""],
  transcribed: ["Odottaa muistiinpanoja", ""], too_short: ["Liian lyhyt", ""],
  summarizing: ["Kirjoitetaan…", ""], notes_failed: ["Muistiinpanot epäonnistuivat", "rec"], interrupted: ["Keskeytynyt", "rec"],
};
// What the speech engine is doing, in words (STATUS.speech comes from the service).
function speechText(sp) {
  sp = sp || {};
  if (sp.phase === "downloading") return `Ladataan puhemallia ensimmäistä kertaa (${sp.downloaded_mb || 0} Mt)…`;
  if (sp.phase === "starting" || sp.phase === "loading") return "Käynnistetään puheentunnistusta…";
  if (sp.phase === "testing") return "Testataan puheentunnistusta…";
  return "";
}
function progressText(meta) {
  const p = meta.progress || {};
  const sp = speechText(STATUS && STATUS.speech);
  if (p.step === "transcribe") return sp || `Muutetaan puhetta tekstiksi: ${fmtClock(p.done || 0)} / ${fmtClock(p.total || 0)}`;
  if (p.step === "notes") return "Claude kirjoittaa muistiinpanoja…";
  return sp || "Kirjoitetaan muistiinpanoja…";
}
const ICONS = {
  rec: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="7"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  mic: '<svg viewBox="0 0 24 24"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V21h2v-2.1A7 7 0 0 0 19 12z"/></svg>',
};

// Minimal Markdown for the notes the app writes (headings, lists, tables, emphasis).
function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*(?!\s)(.+?)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])_(?!\s)(.+?)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
function markdown(md) {
  const out = []; const lines = String(md || "").split(/\r?\n/);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^#{1,3} /.test(line)) { const n = line.match(/^#+/)[0].length; out.push(`<h${n}>${inline(line.slice(n + 1))}</h${n}>`); i++; continue; }
    if (/^\s*[-*] /.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*] /.test(lines[i])) { items.push(`<li>${inline(lines[i].replace(/^\s*[-*] /, ""))}</li>`); i++; }
      out.push(`<ul>${items.join("")}</ul>`); continue;
    }
    if (/^\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) { rows.push(lines[i]); i++; }
      const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const body = rows.filter((r, k) => !(k === 1 && /^\|[\s|:-]+\|?$/.test(r)));
      const [head, ...rest] = body;
      out.push(`<div class="table-wrap"><table><thead><tr>${cells(head).map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>` +
        rest.map((r) => `<tr>${cells(r).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("") + "</tbody></table></div>");
      continue;
    }
    if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    i++;
  }
  return out.join("\n");
}
function transcriptHtml(text) {
  const rows = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    const m = line.match(/^\[(\d\d:\d\d:\d\d)\]\s*(?:(Me|Others):)?\s*(.*)$/);
    if (!m) continue;
    const [, ts, who, said] = m;
    const note = /^\(.*\)$/.test(said);
    const label = who === "Me" ? "Minä" : who === "Others" ? "Muut" : "";
    const sayNote = note ? { "(recording paused)": "Tauko alkoi", "(recording resumed)": "Tauko päättyi", "(recording stopped by safe word)": "Kuuntelu lopetettu turvasanalla" }[said] || said : said;
    rows.push(`<div class="tline${note ? " note" : ""}"><span class="ts">${ts}</span><span class="who${who === "Me" ? " me" : ""}">${label}</span><span>${esc(sayNote)}</span></div>`);
  }
  return rows.join("") || '<p class="muted">Ei tekstiä.</p>';
}

// ---------------------------------------------------------------- state
let STATUS = null;
let TEMPLATES = [];
api("/api/templates").then((t) => { TEMPLATES = t.templates; }).catch(() => {});
let statusTimer = null;
let tickTimer = null;
let onStatus = null; // the current view's re-render hook

async function refreshStatus() {
  try {
    STATUS = await api("/api/status");
    document.body.dataset.device = STATUS.device;
    onStatus && onStatus(STATUS);
  } catch (e) {
    if (e.status === 401) return showPairing();
    STATUS = null;
    onStatus && onStatus(null);
  }
}
function startPolling() {
  clearInterval(statusTimer);
  statusTimer = setInterval(() => { if (!document.hidden) refreshStatus(); }, 2000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
}

// ---------------------------------------------------------------- router
const routes = [
  [/^#?\/?$/, viewHome, "home"],
  [/^#\/meetings$/, viewMeetings, "meetings"],
  [/^#\/meetings\/(.+)$/, viewMeeting, "meetings"],
  [/^#\/ask$/, viewAsk, "assistant"],
  [/^#\/tasks$/, viewTasks, "tasks"],
  [/^#\/assistant$/, viewAssistant, "assistant"],
  [/^#\/assistant\/(.+)$/, viewBriefing, "assistant"],
  [/^#\/settings(?:\/speech)?$/, viewSettings, "settings"],
];
function route() {
  if (!TOKEN) return showPairing();
  onStatus = null; clearInterval(tickTimer);
  const hash = location.hash || "#/";
  for (const [re, fn, nav] of routes) {
    const m = hash.match(re);
    if (m) {
      document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
      const main = $("#view");
      fn(main, ...m.slice(1).map(decodeURIComponent));
      main.focus({ preventScroll: true });
      window.scrollTo(0, 0);
      return;
    }
  }
  location.hash = "#/";
}
window.addEventListener("hashchange", () => {
  if (!PHONE_REC.active) return route();
  history.replaceState(null, "", "#/");  // keep the recorder on screen while it records
  toast("Lopeta puhelinnauhoitus ensin");
});

function showPairing() {
  $("#view").innerHTML = `
    <div class="page-head"><h1>Yhdistä laite</h1></div>
    <div class="card stack">
      <p>Tämä laite ei ole vielä yhdistetty tietokoneesi Meetnotesiin.</p>
      <ol class="steps">
        <li>Avaa tietokoneella Meetnotes ja valitse <b>Asetukset → Puhelin</b>.</li>
        <li>Kytke puhelinyhteys päälle ja skannaa QR-koodi puhelimen kameralla.</li>
        <li>Puhelin ja tietokone pitää olla samassa Wi-Fi-verkossa.</li>
      </ol>
    </div>`;
}

// ---------------------------------------------------------------- home
const STATE_TEXT = {
  watching: ["Odottaa kokousta", "Nauhoitus alkaa itsestään, kun Zoom-, Teams- tai Meet-kokous alkaa.", "on"],
  "auto-off": ["Automaattinen nauhoitus pois", "Mitään ei nauhoiteta, ellet paina Nauhoita nyt.", ""],
  recording: ["Nauhoittaa", "", "rec"],
  paused: ["Tauolla", "Ääntä ei tallenneta ennen kuin jatkat.", "paused"],
  "writing-notes": ["Kirjoittaa muistiinpanoja…", "Valmis yleensä minuutin sisällä.", "on"],
  "stopped-safe-word": ["Pysäytetty turvasanalla", "Seuraava kokous nauhoitetaan taas normaalisti.", ""],
};

function viewHome(main) {
  main.innerHTML = `
    <div class="page-head"><h1>Meetnotes</h1></div>
    <div class="stack">
      <div id="banner"></div>
      <section class="card hero" id="hero" aria-live="polite"></section>
      <section class="card" id="live-card" hidden></section>
      <section class="card" id="notepad-card" hidden>
        <h2>Omat muistiinpanot</h2>
        <p class="muted small">Kirjoita kokouksen aikana vapaasti. Avustaja yhdistää nämä tekstiin ja kirjoittaa niiden pohjalta tarkemmat muistiinpanot.</p>
        <textarea class="notepad" id="notepad" placeholder="esim. Jari haluaa toimituksen tammikuussa, kysy tuesta…"></textarea>
        <p class="hint" id="notepad-status"></p>
      </section>
      <section class="card" id="next-card" hidden></section>
      <section class="card" id="phone-card" hidden></section>
      <section>
        <div class="page-head" style="margin:8px 0 10px"><h2>Viimeisimmät</h2><a href="#/meetings" class="small">Kaikki kokoukset</a></div>
        <div class="list" id="recent"><div class="muted small">Ladataan…</div></div>
      </section>
    </div>`;
  const hero = $("#hero");
  let lastSig = "";

  function render(st) {
    const banner = $("#banner");
    if (!st) {
      banner.innerHTML = `<div class="banner error">Tietokoneeseen ei saada yhteyttä. Onko Meetnotes käynnissä ja oletko samassa Wi-Fi-verkossa?</div>`;
      lastSig = "";
      return;
    }
    banner.innerHTML = (st.error ? `<div class="banner error">Nauhoitus ei käynnistynyt: ${esc(st.error)}</div>` : "") +
      (st.update_progress && st.update_progress !== "failed" ? `<div class="banner"><span>Päivitetään Meetnotesia (${esc({ checking: "tarkistetaan", downloading: "ladataan", installing: "asennetaan", restarting: "käynnistetään uudelleen" }[st.update_progress] || st.update_progress)})…</span></div>`
        : st.update_available ? `<div class="banner"><span>Versio ${esc(st.update_available)} on saatavilla. Pieni lataus, asetukset ja muistiinpanot säilyvät.</span><a class="btn small" href="#/settings">Päivitä</a></div>` : "") +
      (st.has_api_key ? "" :
      `<div class="banner"><span>Lisää Claude API -avain, jotta muistiinpanot voidaan kirjoittaa.</span><a class="btn small" href="#/settings">Asetuksiin</a></div>`);
    renderPhoneCard(st);
    renderLiveCard(st);
    renderNotepad(st);
    if (!recordingState(st)) loadNextMeeting();
    // Re-draw only when something visible changed, so typing isn't wiped by polling.
    const backlogMin = Math.round((st.backlog || 0) / 60);
    const sig = JSON.stringify([speechText(st.speech), st.speech && st.speech.phase === "failed", st.state, st.meeting, st.platform, st.since, st.paused, st.auto, st.safe_word, st.record_screen, st.phase, backlogMin, st.update_available, st.update_progress, st.error, st.has_api_key, st.notice]);
    if (sig === lastSig) return;
    if (lastSig) loadRecent();  // a meeting started or finished: refresh the list below
    lastSig = sig;
    const recording = st.state === "recording" || st.state === "paused";
    let [title, sub, dot] = STATE_TEXT[st.state] || [st.state, "", ""];
    if (st.state === "finishing") {
      [title, sub, dot] = {
        transcribing: ["Viimeistellään tekstiä…", backlogMin >= 1 ? `Noin ${backlogMin} min ääntä vielä litteroimatta.` : "Melkein valmis.", "on"],
        video: ["Viimeistellään videota…", "Valmis hetken kuluttua.", "on"],
        notes: ["Kirjoittaa muistiinpanoja…", "Valmis yleensä minuutin sisällä.", "on"],
      }[st.phase] || ["Viimeistellään…", "", "on"];
    }
    const speechNow = (recording || st.state === "finishing") ? speechText(st.speech) : "";
    if (speechNow) sub = speechNow;
    const lag = recording && backlogMin >= 2 ? ` · teksti noin ${backlogMin} min jäljessä` : "";
    hero.innerHTML = `
      <div class="hero-top"><span class="dot ${dot}"></span><span class="hero-state">${esc(title)}</span>
        ${recording ? `<span class="timer" id="timer"></span>` : ""}</div>
      ${recording ? `<div class="hero-meeting">${esc(st.meeting || "")}</div><div class="hero-sub">${esc([platformName(st.platform), st.record_screen ? "ääni ja näyttö" : "ääni"].filter(Boolean).join(" · ") + lag)}${st.state === "paused" || speechNow ? " · " + esc(sub) : ""}</div>`
        : `<div class="hero-sub">${esc(sub)}</div>`}
      <div class="actions">
        ${recording ? `
          <button class="btn" id="pause">${st.paused ? ICONS.play + "Jatka" : ICONS.pause + "Tauko"}</button>
          <button class="btn danger" id="stop">${ICONS.stop}Lopeta nauhoitus</button>`
        : st.state === "finishing" && st.phase === "transcribing" ? `<button class="btn" id="finish-now">Lopeta odottaminen</button>`
        : (st.state === "writing-notes" || st.state === "finishing") ? "" : `
          <input type="text" id="rec-title" placeholder="Kokouksen nimi (valinnainen)" aria-label="Kokouksen nimi" style="flex:1 1 220px;width:auto">
          <button class="btn primary" id="record">${ICONS.rec}${st.device === "phone" ? "Nauhoita tietokoneella" : "Nauhoita nyt"}</button>`}
      </div>
      ${st.notice ? `<p class="hint" style="margin-top:12px">${esc(st.notice)}</p>` : ""}
      ${st.speech && st.speech.phase === "failed" ? `<div class="banner error small" style="margin-top:12px"><span>Puheentunnistus ei toimi tällä koneella. Ääni tallentuu silti.</span><a class="btn small" href="#/settings/speech">Katso syy</a></div>` : ""}
      ${st.state === "finishing" && st.phase === "transcribing" ? `<p class="hint" style="margin-top:12px">Jos odottaminen kestää liian kauan, paina Lopeta odottaminen. Muistiinpanot kirjoitetaan siitä, mitä ehdittiin litteroida, ja ääni säilyy, joten loput voi litteroida myöhemmin kokouksen sivulta.</p>` : ""}
      <div class="row" style="margin-top:18px">
        <div class="row-text"><div class="row-title">Automaattinen nauhoitus</div>
          <div class="row-desc">Aloittaa ja lopettaa itse kokousikkunan mukaan.</div></div>
        <label class="switch"><input type="checkbox" id="auto" ${st.auto ? "checked" : ""} aria-label="Automaattinen nauhoitus"><span></span></label>
      </div>
      ${st.safe_word ? `<p class="faint small" style="margin:6px 0 0">Turvasana “${esc(st.safe_word)}” lopettaa kuuntelun kesken kokouksen.</p>` : ""}`;
    $("#record") && ($("#record").onclick = () => act("/api/record", { on: true, title: $("#rec-title").value.trim() }, "Nauhoitus alkaa"));
    $("#stop") && ($("#stop").onclick = () => act("/api/record", { on: false }, "Lopetetaan ja kirjoitetaan muistiinpanot"));
    $("#finish-now") && ($("#finish-now").onclick = () => act("/api/live/finish-now", {}, "Muistiinpanot kirjoitetaan siitä, mitä ehdittiin litteroida"));
    $("#pause") && ($("#pause").onclick = () => act("/api/pause", { on: !st.paused }, st.paused ? "Jatketaan" : "Tauko"));
    $("#auto").onchange = (e) => act("/api/auto", { on: e.target.checked }, e.target.checked ? "Automaattinen nauhoitus päällä" : "Automaattinen nauhoitus pois");
    clearInterval(tickTimer);
    if (recording && st.since) {
      const since = new Date(st.since).getTime();
      const tick = () => { const el = $("#timer"); if (el) el.textContent = fmtClock((Date.now() - since) / 1000); };
      tick(); tickTimer = setInterval(tick, 1000);
    }
  }
  onStatus = render;
  if (STATUS) render(STATUS);
  refreshStatus();
  loadRecent();
}

async function act(path, body, msg) {
  try { await api(path, body); toast(msg); await refreshStatus(); }
  catch (e) { toast("Ei onnistunut: " + e.message); }
}

async function loadRecent() {
  try {
    const { meetings } = await api("/api/meetings");
    const el = $("#recent");
    if (!el) return;
    el.innerHTML = meetings.length ? meetings.slice(0, 4).map(itemHtml).join("") :
      `<div class="empty card">Ei vielä kokouksia. Ensimmäinen ilmestyy tähän kokouksen jälkeen.</div>`;
  } catch { /* status banner covers it */ }
}
function itemHtml(m) {
  const [label, cls] = STATUS_BADGE[m.status] || ["", ""];
  const meta = [fmtDate(m.started), fmtDuration(m.duration), platformName(m.platform)].filter(Boolean).join(" · ");
  return `<a class="item" href="#/meetings/${encodeURIComponent(m.id)}">
      <div class="item-main"><div class="item-title">${esc(m.title)}</div><div class="item-meta">${esc(meta)}</div></div>
      ${label ? `<span class="badge ${cls}">${label}</span>` : ""}</a>`;
}

// ---------------------------------------------------------------- notepad + next meeting
const recordingState = (st) => st && (st.state === "recording" || st.state === "paused");
let notepadTimer = null, notepadLoaded = false;
function renderNotepad(st) {
  const card = $("#notepad-card");
  if (!card) return;
  const on = recordingState(st);
  card.hidden = !on;
  if (!on) { notepadLoaded = false; return; }
  const ta = $("#notepad");
  if (!notepadLoaded) {
    notepadLoaded = true;
    api("/api/live").then((L) => { if (L.active && !ta.value) ta.value = L.my_notes || ""; }).catch(() => {});
    ta.oninput = () => {
      clearTimeout(notepadTimer);
      $("#notepad-status").textContent = "Tallennetaan…";
      notepadTimer = setTimeout(async () => {
        try { await api("/api/live/notes", { text: ta.value }); $("#notepad-status").textContent = "Tallennettu"; }
        catch (e) { $("#notepad-status").textContent = "Ei tallentunut: " + e.message; }
      }, 800);
    };
  }
}

const fmtEventTime = (iso) => {
  const d = new Date(iso); const today = new Date();
  const time = d.toLocaleTimeString("fi-FI", { hour: "2-digit", minute: "2-digit" });
  const day = d.toDateString() === today.toDateString() ? "tänään" : d.toLocaleDateString("fi-FI", { weekday: "short", day: "numeric", month: "numeric" });
  return { time, day };
};
function eventHtml(e, withPrep) {
  const { time, day } = fmtEventTime(e.start);
  const sub = [e.platform, e.attendees.slice(0, 3).join(", ") + (e.attendees.length > 3 ? ` +${e.attendees.length - 3}` : "")].filter(Boolean).join(" · ");
  const action = !withPrep ? "" : e.briefing_id ? `<a class="btn small" href="#/assistant/${encodeURIComponent(e.briefing_id)}">Valmistelu</a>`
    : `<button type="button" class="btn small primary" data-prep="${esc(e.uid)}">Valmistele</button>`;
  return `<div class="event${e.now ? " now" : ""}"><div class="time">${time}<small>${day}</small></div><div class="ttl">${esc(e.title)}</div>${action}
    ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`;
}
async function loadNextMeeting() {
  const card = $("#next-card");
  if (!card) return;
  try {
    const c = await api("/api/calendar");
    if (!c.configured) { card.hidden = true; return; }
    const next = c.events.filter((e) => !e.now).slice(0, 3);
    const now = c.events.find((e) => e.now);
    card.hidden = !next.length && !now;
    card.innerHTML = `<div class="page-head" style="margin-bottom:8px"><h2>Seuraavat kokoukset</h2><a href="#/assistant" class="small">Kaikki</a></div>
      <div class="list">${[now, ...next].filter(Boolean).map((e) => eventHtml(e, true)).join("")}</div>`;
    bindPrep(card);
  } catch { card.hidden = true; }
}
function bindPrep(root) {
  root.querySelectorAll("[data-prep]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try {
      const br = await api(`/api/calendar/${encodeURIComponent(b.dataset.prep)}/briefing`, {});
      await api(`/api/briefings/${encodeURIComponent(br.id)}/prepare`, {});
      location.hash = "#/assistant/" + encodeURIComponent(br.id);
    } catch (e) { toast("Ei onnistunut: " + e.message); b.disabled = false; }
  }));
}

// ---------------------------------------------------------------- live assistant (during the meeting)
const KIND_LABEL = { bring_up: "Ota esiin", ask: "Kysy", heads_up: "Huomio", decision: "Päätös", action: "Tehtävä",
  question: "Sinä", answer: "Avustaja", keyword: "Avainsana" };
const AGENDA_LABEL = { not_started: "", in_progress: "käsitellään", done: "käsitelty", skipped: "ohitettu" };
let liveTimer = null, liveSig = "";

function nudgeHtml(n) {
  return `<div class="nudge ${esc(n.kind)}"><span class="kind">${KIND_LABEL[n.kind] || esc(n.kind)}</span>
    <div class="text">${esc(n.text)}</div>${n.why ? `<div class="why">${esc(n.why)}</div>` : ""}
    ${n.at ? `<div class="at">${esc(String(n.at).slice(11, 16))}</div>` : ""}</div>`;
}

function renderLiveCard(st) {
  const card = $("#live-card");
  if (!card) return;
  const recording = st && (st.state === "recording" || st.state === "paused");
  card.hidden = !recording;
  if (!recording) { clearInterval(liveTimer); liveTimer = null; liveSig = ""; card.innerHTML = ""; return; }
  if (!liveTimer) { loadLive(); liveTimer = setInterval(() => { if (!document.hidden) loadLive(); }, 3000); }
}

async function loadLive() {
  const card = $("#live-card");
  if (!card) return;
  let L;
  try { L = await api("/api/live"); } catch { return; }
  if (!L.active) return;
  const a = L.assistant;
  const alerts = L.alerts || [];
  const sig = JSON.stringify([L.enabled, L.briefing, a && a.feed.length, a && a.agenda, a && a.error, a && a.calls, L.briefings, alerts.length, L.template]);
  if (sig === liveSig) return;
  liveSig = sig;
  const draft = $("#live-q") ? $("#live-q").value : "";
  const feed = [...(a ? a.feed : []), ...alerts].sort((x, y) => (y.at || "").localeCompare(x.at || ""));
  const tmplSel = TEMPLATES.length ? `<div class="ask-row" style="margin-top:4px"><select id="live-template" aria-label="Kokouksen tyyppi">
      ${TEMPLATES.map((t) => `<option value="${esc(t.id)}" ${t.id === L.template ? "selected" : ""}>Muistiinpanot: ${esc(t.name)}</option>`).join("")}</select></div>` : "";
  card.innerHTML = `
    <div class="row" style="padding-top:0">
      <div class="row-text"><div class="row-title">Avustaja</div>
        <div class="row-desc">${L.enabled ? "Seuraa keskustelua ja ehdottaa, mitä ottaa esiin. Vain sinä näet tämän." : "Pois päältä. Kytke päälle, niin se seuraa keskustelua."}</div></div>
      <label class="switch"><input type="checkbox" id="live-on" ${L.enabled ? "checked" : ""} aria-label="Avustaja päällä"><span></span></label>
    </div>
    ${L.briefing ? `<p class="brief-meta">Valmistelu: <b>${esc(L.briefing.title)}</b></p>`
      : L.briefings.length ? `<div class="ask-row" style="margin-top:4px"><select id="live-attach" aria-label="Liitä valmistelu"><option value="">Liitä valmistelu…</option>
          ${L.briefings.map((b) => `<option value="${esc(b.id)}">${esc(b.title)}</option>`).join("")}</select></div>`
      : `<p class="brief-meta">Ei valmistelua tähän kokoukseen. <a href="#/assistant">Tee valmistelu</a> ennen seuraavaa, niin avustaja tuntee agendan.</p>`}
    ${L.event ? `<p class="brief-meta">Kalenteri: <b>${esc(L.event.title)}</b>${L.event.attendees.length ? " · " + esc(L.event.attendees.join(", ")) : ""}</p>` : ""}
    ${tmplSel}
    ${a && a.agenda.length ? `<div class="agenda">${a.agenda.map((x) => `<span class="${esc(x.status)}">${esc(x.item)}${AGENDA_LABEL[x.status] ? " · " + AGENDA_LABEL[x.status] : ""}</span>`).join("")}</div>` : ""}
    <form class="ask-row" id="live-form"><input type="text" id="live-q" placeholder="Kysy avustajalta kesken kokouksen…" autocomplete="off" value="${esc(draft)}">
      <button class="btn primary" id="live-ask">Kysy</button></form>
    ${a && a.error ? `<p class="banner error small" style="margin-top:10px">Avustaja: ${esc(a.error)}</p>` : ""}
    <div id="live-feed">${feed.length ? feed.map(nudgeHtml).join("") : `<p class="hint" style="margin-top:12px">${L.enabled ? "Ei vielä ehdotuksia. Ensimmäiset tulevat, kun keskustelua on kertynyt." : ""}</p>`}</div>`;
  $("#live-on").onchange = async (e) => {
    try { await api("/api/live/enable", { on: e.target.checked }); liveSig = ""; loadLive(); }
    catch (err) { toast("Ei onnistunut: " + err.message); }
  };
  $("#live-template") && ($("#live-template").onchange = async (e) => {
    try { await api("/api/live/template", { template: e.target.value }); toast("Kokouksen tyyppi vaihdettu"); } catch (err) { toast("Ei onnistunut: " + err.message); }
  });
  $("#live-attach") && ($("#live-attach").onchange = async (e) => {
    if (!e.target.value) return;
    try { await api("/api/live/attach", { briefing_id: e.target.value }); toast("Valmistelu liitetty"); liveSig = ""; loadLive(); }
    catch (err) { toast("Ei onnistunut: " + err.message); }
  });
  $("#live-form").onsubmit = async (e) => {
    e.preventDefault();
    const q = $("#live-q").value.trim(); if (!q) return;
    const btn = $("#live-ask"); btn.disabled = true; btn.textContent = "Mietitään…";
    try { await api("/api/live/ask", { question: q }); $("#live-q").value = ""; liveSig = ""; await loadLive(); }
    catch (err) { toast("Ei onnistunut: " + err.message); btn.disabled = false; btn.textContent = "Kysy"; }
  };
}

// ---------------------------------------------------------------- phone recorder (in-person meetings)
const PHONE_REC = { active: false };
function renderPhoneCard(st) {
  const card = $("#phone-card");
  if (!card) return;
  const can = st.device === "phone" && navigator.mediaDevices && window.MediaRecorder && isSecureContext;
  card.hidden = !can;
  if (!can || card.dataset.ready) return;
  card.dataset.ready = "1";
  card.innerHTML = `
    <h2>Nauhoita tällä puhelimella</h2>
    <p class="muted small">Kasvokkaisiin kokouksiin. Puhelimen mikrofoni kuulee koko huoneen. Pidä näyttö päällä nauhoituksen ajan.</p>
    <label class="field"><span>Kokouksen nimi</span><input type="text" id="ph-title" placeholder="esim. Asiakastapaaminen" autocomplete="off"></label>
    <div class="recorder">
      <div class="big-timer" id="ph-time">0:00</div>
      <div class="meter"><i id="ph-meter"></i></div>
      <button class="rec-btn" id="ph-btn" aria-label="Aloita nauhoitus">${ICONS.mic}</button>
      <div class="muted small" id="ph-msg">Paina aloittaaksesi</div>
    </div>`;
  $("#ph-btn").onclick = () => (PHONE_REC.active ? stopPhone() : startPhone());
}

async function startPhone() {
  const btn = $("#ph-btn"), msg = $("#ph-msg");
  btn.disabled = true;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true } });
    const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus"].find((m) => MediaRecorder.isTypeSupported(m)) || "";
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : {});
    const { id } = await api("/api/phone/start", { title: $("#ph-title").value.trim(), mime: rec.mimeType || mime });
    Object.assign(PHONE_REC, { active: true, id, rec, stream, queue: Promise.resolve(), failed: false, started: Date.now() });
    rec.ondataavailable = (e) => { if (e.data && e.data.size) PHONE_REC.queue = PHONE_REC.queue.then(() => upload(id, e.data)); };
    rec.start(5000); // a piece every 5 s goes to the computer right away
    try { window.MeetnotesAndroid?.keepScreenOn(true); } catch { /* not in the app */ }
    try { PHONE_REC.wake = await navigator.wakeLock?.request("screen"); } catch { /* not supported */ }
    meterStart(stream);
    PHONE_REC.timer = setInterval(() => { $("#ph-time") && ($("#ph-time").textContent = fmtClock((Date.now() - PHONE_REC.started) / 1000)); }, 500);
    btn.classList.add("stop"); btn.innerHTML = ICONS.stop; btn.setAttribute("aria-label", "Lopeta nauhoitus");
    msg.textContent = "Nauhoittaa. Ääni tallentuu tietokoneelle koko ajan.";
    $("#ph-title").disabled = true;
  } catch (e) {
    msg.textContent = e.name === "NotAllowedError" ? "Salli mikrofoni selaimen asetuksista." : "Ei onnistunut: " + e.message;
  } finally { btn.disabled = false; }
}

async function upload(id, blob) {
  for (let attempt = 0; attempt < 8; attempt++) {
    try { await api(`/api/phone/${encodeURIComponent(id)}/chunk`, blob); PHONE_REC.failed = false; return; }
    catch (e) {
      if (e.status && e.status < 500) break;
      PHONE_REC.failed = true;
      const msg = $("#ph-msg"); if (msg) msg.textContent = "Yhteys tietokoneeseen katkesi, yritetään uudelleen…";
      await new Promise((r) => setTimeout(r, Math.min(30000, 1000 * 2 ** attempt)));
    }
  }
  const msg = $("#ph-msg"); if (msg) msg.textContent = "Osa äänestä ei siirtynyt tietokoneelle.";
}

async function stopPhone() {
  const btn = $("#ph-btn"), msg = $("#ph-msg");
  btn.disabled = true;
  msg.textContent = "Lopetetaan…";
  const { rec, stream, id } = PHONE_REC;
  await new Promise((r) => { rec.onstop = r; rec.stop(); });
  stream.getTracks().forEach((t) => t.stop());
  clearInterval(PHONE_REC.timer); meterStop();
  try { window.MeetnotesAndroid?.keepScreenOn(false); } catch { /* not in the app */ }
  try { PHONE_REC.wake?.release(); } catch { /* ignore */ }
  await PHONE_REC.queue;
  PHONE_REC.active = false;
  try {
    await api(`/api/phone/${encodeURIComponent(id)}/finish`, {});
    msg.textContent = "Tietokone tekee tekstiä ja muistiinpanoja…";
    for (;;) {
      await new Promise((r) => setTimeout(r, 2500));
      const st = await api(`/api/phone/${encodeURIComponent(id)}`);
      if (st.status === "done") { toast("Muistiinpanot valmiit"); location.hash = "#/meetings/" + encodeURIComponent(id); return; }
      if (st.status === "empty") { msg.textContent = "Puhetta oli liian vähän, nauhoitusta ei tallennettu."; break; }
      if (st.status === "error") { msg.textContent = "Käsittely epäonnistui: " + st.error; break; }
    }
  } catch (e) { msg.textContent = "Ei onnistunut: " + e.message; }
  btn.disabled = false; btn.classList.remove("stop"); btn.innerHTML = ICONS.mic;
  $("#ph-title").disabled = false;
}

function meterStart(stream) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const an = ctx.createAnalyser(); an.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(an);
    const data = new Uint8Array(an.fftSize);
    const loop = () => {
      an.getByteTimeDomainData(data);
      let peak = 0; for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
      const el = $("#ph-meter"); if (el) el.style.width = Math.min(100, (peak / 128) * 260) + "%";
      PHONE_REC.raf = requestAnimationFrame(loop);
    };
    PHONE_REC.ctx = ctx; loop();
  } catch { /* meter is optional */ }
}
function meterStop() { cancelAnimationFrame(PHONE_REC.raf); PHONE_REC.ctx?.close(); }
window.addEventListener("beforeunload", (e) => { if (PHONE_REC.active) { e.preventDefault(); e.returnValue = ""; } });

// ---------------------------------------------------------------- meetings
async function viewMeetings(main) {
  main.innerHTML = `<div class="page-head"><h1>Kokoukset</h1></div>
    <input type="text" id="filter" placeholder="Hae nimellä" aria-label="Hae kokouksia" style="margin-bottom:12px">
    <div class="list" id="all"><div class="muted">Ladataan…</div></div>`;
  try {
    const { meetings } = await api("/api/meetings");
    const draw = (q = "") => {
      const list = meetings.filter((m) => m.title.toLowerCase().includes(q.toLowerCase()));
      $("#all").innerHTML = list.length ? list.map(itemHtml).join("") :
        `<div class="empty card">${meetings.length ? "Ei osumia." : "Ei vielä kokouksia."}</div>`;
    };
    draw();
    $("#filter").oninput = (e) => draw(e.target.value);
  } catch (e) { $("#all").innerHTML = `<div class="banner error">Ei saatu ladattua: ${esc(e.message)}</div>`; }
}

async function viewMeeting(main, id) {
  main.innerHTML = `<a class="back" href="#/meetings">‹ Kokoukset</a><div class="muted">Ladataan…</div>`;
  let m;
  try { m = await api("/api/meetings/" + encodeURIComponent(id)); }
  catch { main.innerHTML = `<a class="back" href="#/meetings">‹ Kokoukset</a><div class="empty card">Kokousta ei löytynyt.</div>`; return; }
  const meta = m.meta || {};
  const title = meta.notes_title || meta.title || id;
  const hasNotes = !!m.notes;
  const tabs = [["notes", "Muistiinpanot"], ["transcript", "Teksti"]];
  if (m.my_notes) tabs.push(["mine", "Omat"]);
  if (m.has_screen) tabs.push(["video", "Video"]);
  const stats = meta.stats || {};
  const statsHtml = stats.talk_me_pct == null ? "" : `<div class="stats">
      <span>Puheaika: minä <b>${stats.talk_me_pct} %</b>, muut <b>${stats.talk_others_pct} %</b></span>
      <span>Kysymyksiä: minä <b>${stats.questions_me}</b>, muut <b>${stats.questions_others}</b></span>
      <span>Pisin monologi: <b>${fmtClock(stats.longest_monologue_me_s)}</b> / <b>${fmtClock(stats.longest_monologue_others_s)}</b></span>
      ${stats.words_per_minute ? `<span>Tahti <b>${stats.words_per_minute}</b> sanaa/min</span>` : ""}
    </div><div class="bar" aria-hidden="true"><i style="width:${stats.talk_me_pct}%"></i></div>`;
  const chapters = (m.notes_json && m.notes_json.chapters) || [];
  if (m.live_notes && !hasNotes) tabs.unshift(["live", "Väliaikaiset"]);
  const active = STATUS && STATUS.folder_id === id && ["recording", "paused", "finishing", "writing-notes"].includes(STATUS.state);
  // Missing notes of a finished meeting open on the Notes tab, where the reason and "try again" are.
  const first = hasNotes ? "notes" : m.live_notes ? "live" : active ? "transcript" : "notes";
  main.innerHTML = `
    <a class="back" href="#/meetings">‹ Kokoukset</a>
    <div class="page-head"><div><h1>${esc(title)}</h1>
      <div class="muted small">${esc([fmtDate(meta.started_at), fmtDuration(meta.duration_s), platformName(meta.platform), meta.briefing_title ? "valmistelu: " + meta.briefing_title : ""].filter(Boolean).join(" · "))}</div>${statsHtml}</div>
      <div class="actions" style="margin:0"><button class="btn small" id="copy">Kopioi</button>${hasNotes ? `<a class="btn small" id="mail" href="#">Sähköposti</a>` : ""}</div></div>
    ${m.has_audio ? `<div class="audio-row card">
      <audio id="rec-audio" controls preload="none" src="/api/meetings/${encodeURIComponent(id)}/audio.m4a?t=${encodeURIComponent(TOKEN)}"></audio>
      ${STATUS && STATUS.device === "phone"
        ? `<a class="btn small" href="/api/meetings/${encodeURIComponent(id)}/audio.m4a?t=${encodeURIComponent(TOKEN)}&download=1" download>Lataa ääni</a>`
        : `<button type="button" class="btn small" id="reveal">Näytä tiedosto</button>`}
      <span class="hint">Koko kokouksen ääni. Klikkaa tekstin aikaleimaa, niin ääni alkaa siitä kohdasta.</span></div>` : ""}
    ${chapters.length ? `<ul class="chapters" aria-label="Aiheet">${chapters.map((c) => `<li><button type="button" data-seek="${esc(c.start)}"><span class="ts">${esc(c.start)}</span><span>${esc(c.title)}</span></button></li>`).join("")}</ul>` : ""}
    <div class="tabs" role="tablist">${tabs.map(([k, l]) => `<button role="tab" data-tab="${k}" aria-selected="${k === first}">${l}</button>`).join("")}</div>
    <article class="card md" id="content"></article>
    <div class="actions" id="del-row" style="margin-top:20px"><button class="btn ghost small" id="del">Poista kokous</button></div>`;
  const show = (k) => {
    main.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === k)));
    // The topics are already shown as clickable chapters above: don't repeat them in the notes.
    const notesBody = (m.notes || "").replace(/^# .*\n/, "").replace(/^## (Aiheet|Topics|Ämnen)\n[\s\S]*?(?=^## |(?![\s\S]))/m, chapters.length ? "" : "$&");
    $("#content").innerHTML = k === "notes" ? (hasNotes ? markdown(notesBody) : notesMissing(meta, id))
      : k === "live" ? markdown(m.live_notes)
      : k === "mine" ? `<div class="answer">${esc(m.my_notes)}</div>`
      : k === "video" ? `<video class="screen" controls preload="metadata" playsinline
          src="/api/meetings/${encodeURIComponent(id)}/screen.mp4?t=${encodeURIComponent(TOKEN)}"></video>
          <p class="hint" style="margin-top:8px">Näyttö ja kokouksen ääni. Tiedosto: screen.mp4 kokouksen kansiossa.</p>`
      : transcriptHtml(m.transcript);
  };
  const toSecs = (ts) => ts.split(":").reduce((a, x) => a * 60 + Number(x), 0);
  main.addEventListener("click", (e) => {
    const ts = e.target.closest && e.target.closest(".tline .ts");
    const a = $("#rec-audio");
    if (!ts || !a) return;
    a.currentTime = toSecs(ts.textContent); a.play().catch(() => {});
  });
  if ($("#reveal")) $("#reveal").onclick = async () => {
    $("#reveal").disabled = true; toast("Valmistellaan äänitiedostoa…");
    try { await api(`/api/meetings/${encodeURIComponent(id)}/reveal`, {}); } catch (err) { toast("Ei onnistunut: " + err.message); }
    $("#reveal").disabled = false;
  };
  main.addEventListener("click", async (e) => {
    if (e.target.id !== "retry") return;
    e.target.disabled = true;
    try { await api(`/api/meetings/${encodeURIComponent(id)}/summarize`, {}); toast("Kirjoitetaan muistiinpanoja"); pollMeeting(id); }
    catch (err) { toast("Ei onnistunut: " + err.message); e.target.disabled = false; }
  });
  if (meta.status === "summarizing") pollMeeting(id);
  main.querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => {
    $("#content video")?.pause();
    if (b.dataset.tab === "video") $("#rec-audio")?.pause();
    show(b.dataset.tab);
  }));
  show(first);
  const toSec = (ts) => ts.split(":").reduce((a, x) => a * 60 + Number(x), 0);
  main.querySelectorAll("[data-seek]").forEach((b) => (b.onclick = () => {
    const ts = b.dataset.seek;
    if (m.has_screen && main.querySelector("[data-tab=video][aria-selected=true]")) { const v = $("#content video"); if (v) { v.currentTime = toSec(ts); v.play().catch(() => {}); } return; }
    if ($("#rec-audio")) { $("#rec-audio").currentTime = toSec(ts); $("#rec-audio").play().catch(() => {}); }
    show("transcript");
    const row = [...main.querySelectorAll(".tline .ts")].find((e) => e.textContent >= ts);
    if (row) { row.closest(".tline").scrollIntoView({ block: "center" }); row.closest(".tline").style.background = "var(--accent-soft)"; }
  }));
  $("#copy").onclick = async () => {
    const text = hasNotes ? m.notes : m.transcript;
    try { await navigator.clipboard.writeText(text); toast("Kopioitu leikepöydälle"); }
    catch { const r = document.createRange(); r.selectNodeContents($("#content")); getSelection().removeAllRanges(); getSelection().addRange(r); toast("Valitse ja kopioi (Ctrl+C)"); }
  };
  if ($("#mail")) {
    const body = (m.notes || "").replace(/^# .*\n/, "").replace(/\*\*/g, "").slice(0, 1800);
    $("#mail").href = "mailto:?subject=" + encodeURIComponent("Muistiinpanot: " + title) + "&body=" + encodeURIComponent(body + (m.notes.length > 1800 ? "\n\n(lyhennetty)" : ""));
  }
  $("#del").onclick = () => {
    $("#del-row").innerHTML = `<span class="small">Poistetaanko pysyvästi? Muistiinpanot, teksti ja tallenteet poistetaan.</span>
      <button class="btn danger small" id="del-yes">Poista</button><button class="btn ghost small" id="del-no">Peruuta</button>`;
    $("#del-no").onclick = () => route();
    $("#del-yes").onclick = async () => {
      try { await api(`/api/meetings/${encodeURIComponent(id)}/delete`, {}); toast("Poistettu"); location.hash = "#/meetings"; }
      catch (e) { toast("Ei onnistunut: " + e.message); }
    };
  };
}

function notesMissing(meta, id) {
  const active = STATUS && STATUS.folder_id === id && ["recording", "paused", "finishing", "writing-notes"].includes(STATUS.state);
  if (active) return `<p class="muted">Kokous on vielä käynnissä. Muistiinpanot tulevat tähän, kun se päättyy.</p>`;
  if (meta.status === "summarizing") return `<p class="muted" id="progress">${esc(progressText(meta))}</p>`;
  const why = meta.status === "notes_failed" && meta.notes_error
    ? `<div class="banner error" style="margin-bottom:12px">Muistiinpanojen kirjoitus epäonnistui: ${esc(meta.notes_error)}</div>`
    : `<p class="muted">Muistiinpanoja ei vielä ole.</p>`;
  return `${why}<button class="btn primary" id="retry">${meta.status === "notes_failed" ? "Yritä uudelleen" : "Kirjoita muistiinpanot"}</button>
    <p class="hint" style="margin-top:8px">Jos tekstiä ei ole, se tehdään ensin äänitiedostosta.</p>`;
}

let pollTimer = null;
function pollMeeting(id) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!location.hash.startsWith("#/meetings/")) return clearInterval(pollTimer);
    try {
      const m = await api("/api/meetings/" + encodeURIComponent(id));
      if (m.meta.status !== "summarizing") { clearInterval(pollTimer); route(); }
      else if ($("#progress")) $("#progress").textContent = progressText(m.meta);
    } catch { clearInterval(pollTimer); }
  }, 2500);
}

// ---------------------------------------------------------------- assistant: briefings
const fmtWhen = (w) => w || "ajankohta avoin";

async function viewAssistant(main) {
  main.innerHTML = `
    <div class="page-head"><div><h1>Avustaja</h1><div class="muted small">Valmistaudu kokoukseen etukäteen. Kokouksen aikana avustaja seuraa keskustelua ja ehdottaa, mitä ottaa esiin.</div></div></div>
    <div class="stack">
      <section class="card" id="cal-card" hidden></section>
      <section class="card">
        <h2>Kysy kokouksistasi</h2>
        <form class="ask-row" id="ask-form2" style="margin-top:8px"><input type="text" id="q2" placeholder="Mitä lupasin tehdä tällä viikolla?" autocomplete="off" required><button class="btn primary" id="ask-btn2">Kysy</button></form>
        <div class="answer" id="answer2" style="margin-top:12px" hidden></div>
      </section>
      <section class="card">
        <h2>Uusi valmistelu</h2>
        <form id="brief-form">
          <div class="grid-2">
            <label class="field"><span>Kokous</span><input type="text" id="b-title" placeholder="Kontio-palaveri" required></label>
            <label class="field"><span>Ajankohta</span><input type="text" id="b-when" placeholder="pe 14.11.2026 10:00"></label>
          </div>
          <label class="field"><span>Agenda, yksi kohta per rivi</span><textarea id="b-agenda" placeholder="Hinta ja maksuehdot\nToimitusaika\nTuki ja koulutus"></textarea></label>
          <div class="grid-2">
            <label class="field"><span>Tavoitteet</span><input type="text" id="b-goals" placeholder="Sopimus allekirjoitukseen ennen joulua"></label>
            <label class="field"><span>Osallistujat</span><input type="text" id="b-participants" placeholder="Jari Kontio, Mira"></label>
          </div>
          <label class="field"><span>Kokouksen tyyppi</span><select id="b-template"><option value="">Oletus (asetuksista)</option>${TEMPLATES.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select></label>
          <label class="field"><span>Materiaalit</span><textarea id="b-materials" placeholder="Liitä tähän sähköpostit, tarjous, luvut…"></textarea>
            <span class="hint">Avustaja hakee itse aiemmat kokoukset, joissa nimi tai osallistujat mainitaan.</span></label>
          <div class="actions"><button class="btn primary" id="b-save">Luo ja valmistele</button></div>
        </form>
      </section>
      <section><div class="page-head" style="margin:8px 0 10px"><h2>Valmistelut</h2></div><div class="list" id="briefs"><div class="muted small">Ladataan…</div></div></section>
    </div>`;
  $("#ask-form2").onsubmit = async (e) => {
    e.preventDefault();
    const q = $("#q2").value.trim(); if (!q) return;
    const btn = $("#ask-btn2"); btn.disabled = true;
    const out = $("#answer2"); out.hidden = false; out.textContent = "Luetaan muistiinpanoja…";
    try { out.textContent = (await api("/api/ask", { question: q })).answer; } catch (err) { out.textContent = "Ei onnistunut: " + err.message; }
    btn.disabled = false;
  };
  api("/api/calendar").then((c) => {
    const card = $("#cal-card"); if (!card) return;
    if (!c.configured) { card.hidden = false; card.innerHTML = `<h2>Kalenteri</h2><p class="muted small">Lisää kalenterisi ICS-osoite <a href="#/settings">asetuksiin</a>, niin tulevat kokoukset näkyvät tässä ja valmistelun saa yhdellä painalluksella. Kokoukset nimetään kalenterin mukaan.</p>`; return; }
    card.hidden = false;
    card.innerHTML = `<div class="page-head" style="margin-bottom:8px"><h2>Tulevat kokoukset</h2><button type="button" class="btn small ghost" id="cal-refresh">Päivitä</button></div>
      ${c.error ? `<p class="banner error small">${esc(c.error)}</p>` : ""}
      <div class="list">${c.events.length ? c.events.slice(0, 12).map((e) => eventHtml(e, true)).join("") : `<p class="muted small">Ei tulevia kokouksia kahteen viikkoon.</p>`}</div>`;
    bindPrep(card);
    $("#cal-refresh").onclick = async () => { $("#cal-refresh").disabled = true; try { await api("/api/calendar/refresh", {}); route(); } catch (e) { toast(e.message); } };
  }).catch(() => {});
  $("#brief-form").onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#b-save"); btn.disabled = true;
    try {
      const b = await api("/api/briefings", { title: $("#b-title").value, when: $("#b-when").value, agenda: $("#b-agenda").value,
        goals: $("#b-goals").value, participants: $("#b-participants").value, materials: $("#b-materials").value, template: $("#b-template").value });
      await api(`/api/briefings/${encodeURIComponent(b.id)}/prepare`, {});
      toast("Valmistelu alkoi");
      location.hash = "#/assistant/" + encodeURIComponent(b.id);
    } catch (err) { toast("Ei onnistunut: " + err.message); btn.disabled = false; }
  };
  try {
    const { briefings } = await api("/api/briefings");
    $("#briefs").innerHTML = briefings.length ? briefings.map((b) => `<a class="item" href="#/assistant/${encodeURIComponent(b.id)}">
        <div class="item-main"><div class="item-title">${esc(b.title)}</div>
        <div class="item-meta">${esc(fmtWhen(b.when))}${b.agenda.length ? ` · ${b.agenda.length} agendakohtaa` : ""}${b.used_by ? " · käytetty kokouksessa" : ""}</div></div>
        <span class="badge ${b.prep_status === "done" ? "ok" : b.prep_status === "failed" ? "rec" : ""}">${{ done: "Briiffi valmis", preparing: "Valmistellaan…", failed: "Epäonnistui" }[b.prep_status] || "Ei briiffiä"}</span></a>`).join("")
      : `<div class="empty card">Ei vielä valmisteluja. Luo ensimmäinen yllä, esimerkiksi perjantain Kontio-palaverille.</div>`;
  } catch (e) { $("#briefs").innerHTML = `<div class="banner error">${esc(e.message)}</div>`; }
}

let briefTimer = null;
async function viewBriefing(main, id) {
  clearInterval(briefTimer);
  main.innerHTML = `<a class="back" href="#/assistant">‹ Avustaja</a><div class="muted">Ladataan…</div>`;
  let b;
  try { b = await api("/api/briefings/" + encodeURIComponent(id)); }
  catch { main.innerHTML = `<a class="back" href="#/assistant">‹ Avustaja</a><div class="empty card">Valmistelua ei löytynyt.</div>`; return; }
  const chatHtml = (b.chat || []).map((m) => `<div class="bubble ${m.role}">${esc(m.text)}</div>`).join("");
  main.innerHTML = `
    <a class="back" href="#/assistant">‹ Avustaja</a>
    <div class="page-head"><div><h1>${esc(b.title)}</h1><div class="muted small">${esc(fmtWhen(b.when))}${b.participants.length ? " · " + esc(b.participants.join(", ")) : ""}</div></div></div>
    <div class="stack">
      ${b.agenda.length ? `<section class="card"><h2>Agenda</h2><ol style="margin:8px 0 0;padding-left:20px">${b.agenda.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>
        ${b.goals ? `<p class="brief-meta" style="margin-top:10px">Tavoite: ${esc(b.goals)}</p>` : ""}</section>` : ""}
      <section class="card md" id="brief">
        <div class="page-head" style="margin-bottom:8px"><h2>Briiffi</h2><button class="btn small" id="reprep">${b.prep_status === "done" ? "Päivitä" : "Valmistele"}</button></div>
        <div id="brief-body">${b.prep_status === "preparing" ? `<p class="muted">Avustaja lukee aiemmat kokoukset ja materiaalit…</p>`
          : b.prep_status === "failed" ? `<div class="banner error">Valmistelu epäonnistui: ${esc(b.prep_error)}</div>`
          : b.prep ? markdown(b.prep) : `<p class="muted">Ei vielä briiffiä. Paina Valmistele.</p>`}</div>
      </section>
      <section class="card">
        <h2>Kysy ennen kokousta</h2>
        <p class="muted small">Esimerkiksi “Mitä Kontion kanssa on sovittu hinnasta?” tai “Mitkä asiat jäivät viimeksi auki?”</p>
        <div class="chat" id="chat">${chatHtml}</div>
        <form class="ask-row" id="chat-form"><input type="text" id="chat-q" placeholder="Kysy…" autocomplete="off" required><button class="btn primary" id="chat-btn">Kysy</button></form>
      </section>
      <div class="actions" id="del-row"><button class="btn ghost small" id="bdel">Poista valmistelu</button></div>
    </div>`;
  const prepare = async () => {
    try { await api(`/api/briefings/${encodeURIComponent(id)}/prepare`, {}); toast("Valmistellaan"); poll(); }
    catch (err) { toast("Ei onnistunut: " + err.message); }
  };
  const poll = () => {
    clearInterval(briefTimer);
    briefTimer = setInterval(async () => {
      if (!location.hash.startsWith("#/assistant/")) return clearInterval(briefTimer);
      try { const x = await api("/api/briefings/" + encodeURIComponent(id)); if (x.prep_status !== "preparing") { clearInterval(briefTimer); route(); } }
      catch { clearInterval(briefTimer); }
    }, 2500);
  };
  if (b.prep_status === "preparing") poll();
  $("#reprep").onclick = prepare;
  $("#chat-form").onsubmit = async (e) => {
    e.preventDefault();
    const q = $("#chat-q").value.trim(); if (!q) return;
    const chat = $("#chat"); chat.insertAdjacentHTML("beforeend", `<div class="bubble user">${esc(q)}</div><div class="bubble assistant" id="pending">Mietitään…</div>`);
    $("#chat-q").value = ""; $("#chat-btn").disabled = true;
    try { const { answer } = await api(`/api/briefings/${encodeURIComponent(id)}/chat`, { question: q }); $("#pending").textContent = answer; $("#pending").id = ""; }
    catch (err) { $("#pending").textContent = "Ei onnistunut: " + err.message; $("#pending").id = ""; }
    $("#chat-btn").disabled = false;
  };
  $("#bdel").onclick = () => {
    $("#del-row").innerHTML = `<span class="small">Poistetaanko valmistelu?</span><button class="btn danger small" id="del-yes">Poista</button><button class="btn ghost small" id="del-no">Peruuta</button>`;
    $("#del-no").onclick = () => route();
    $("#del-yes").onclick = async () => { try { await api(`/api/briefings/${encodeURIComponent(id)}/delete`, {}); location.hash = "#/assistant"; } catch (err) { toast("Ei onnistunut: " + err.message); } };
  };
}

// ---------------------------------------------------------------- tasks
async function viewTasks(main) {
  main.innerHTML = `<div class="page-head"><div><h1>Tehtävät</h1><div class="muted small">Kaikkien kokousten tehtävät ja jatkotoimet yhdessä. Rastita tehdyt, niin avustaja tietää ne seuraavassa valmistelussa.</div></div></div>
    <div class="tabs" role="tablist"><button role="tab" data-f="mine" aria-selected="true">Omat</button><button role="tab" data-f="all" aria-selected="false">Kaikki</button><button role="tab" data-f="done" aria-selected="false">Tehdyt</button></div>
    <section class="card" id="tasks"><div class="muted">Ladataan…</div></section>`;
  let tasks = [];
  let filter = "mine";
  const draw = () => {
    const list = tasks.filter((t) => filter === "done" ? t.done : filter === "mine" ? t.mine && !t.done : !t.done);
    $("#tasks").innerHTML = list.length ? list.map((t) => `<label class="task${t.done ? " done" : ""}"><input type="checkbox" data-id="${t.id}" ${t.done ? "checked" : ""}>
        <div class="t"><div>${esc(t.text)}</div><div class="m">${esc([t.owner && !t.mine ? t.owner : "", t.due, t.meeting, fmtDate(t.date + "T12:00:00").replace(/ 12\.00$/, "")].filter(Boolean).join(" · "))}</div></div></label>`).join("")
      : `<p class="muted">${filter === "done" ? "Ei tehtyjä tehtäviä." : "Ei avoimia tehtäviä."}</p>`;
    $("#tasks").querySelectorAll("input").forEach((c) => (c.onchange = async () => {
      try { await api(`/api/tasks/${c.dataset.id}`, { done: c.checked }); const t = tasks.find((x) => x.id === c.dataset.id); if (t) t.done = c.checked; setTimeout(draw, 300); }
      catch (e) { toast("Ei onnistunut: " + e.message); c.checked = !c.checked; }
    }));
  };
  main.querySelectorAll("[data-f]").forEach((b) => (b.onclick = () => { filter = b.dataset.f; main.querySelectorAll("[data-f]").forEach((x) => x.setAttribute("aria-selected", String(x === b))); draw(); }));
  try { tasks = (await api("/api/tasks")).tasks; draw(); }
  catch (e) { $("#tasks").innerHTML = `<div class="banner error">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- ask
function viewAsk(main) {
  const examples = ["Mitä lupasin tehdä tällä viikolla?", "Mitä päätettiin budjetista?", "Mitkä asiat jäivät auki viime kokouksissa?"];
  main.innerHTML = `
    <div class="page-head"><h1>Kysy kokouksistasi</h1></div>
    <div class="card">
      <form id="ask-form">
        <label class="field" style="margin-top:0"><span class="sr-only">Kysymys</span>
          <textarea id="q" placeholder="Kysy mitä tahansa aiemmista kokouksistasi" required></textarea></label>
        <div class="actions" style="margin-top:12px"><button class="btn primary" id="ask-btn">Kysy</button></div>
      </form>
      <div class="chips">${examples.map((e) => `<button class="chip" type="button">${esc(e)}</button>`).join("")}</div>
    </div>
    <section class="card" id="answer-card" hidden style="margin-top:16px"><div class="answer" id="answer"></div></section>`;
  main.querySelectorAll(".chip").forEach((c) => (c.onclick = () => { $("#q").value = c.textContent; $("#q").focus(); }));
  $("#ask-form").onsubmit = async (e) => {
    e.preventDefault();
    const q = $("#q").value.trim(); if (!q) return;
    const btn = $("#ask-btn"); btn.disabled = true; btn.textContent = "Mietitään…";
    $("#answer-card").hidden = false; $("#answer").textContent = "Luetaan muistiinpanoja…";
    try { const { answer } = await api("/api/ask", { question: q }); $("#answer").textContent = answer; }
    catch (err) { $("#answer").textContent = "Ei onnistunut: " + err.message; }
    btn.disabled = false; btn.textContent = "Kysy";
  };
}

// ---------------------------------------------------------------- settings
const LANGS = [["", "Tunnista automaattisesti"], ["fi", "Suomi"], ["en", "Englanti"], ["sv", "Ruotsi"], ["de", "Saksa"], ["et", "Viro"]];
const NOTE_LANGS = [["", "Sama kuin kokouksessa"], ["Finnish", "Suomi"], ["English", "Englanti"], ["Swedish", "Ruotsi"]];
const MODELS = [["small", "Small, nopea"], ["medium", "Medium"], ["large-v3-turbo", "Large v3 turbo, paras suomelle"], ["large-v3", "Large v3, hidas"], ["base", "Base, kevyin"]];
const opts = (list, val) => list.map(([v, l]) => `<option value="${esc(v)}" ${v === val ? "selected" : ""}>${esc(l)}</option>`).join("");
const toggle = (id, title, desc, on) => `
  <div class="row"><div class="row-text"><div class="row-title">${title}</div><div class="row-desc">${desc}</div></div>
  <label class="switch"><input type="checkbox" id="${id}" ${on ? "checked" : ""} aria-label="${esc(title)}"><span></span></label></div>`;

async function viewSettings(main) {
  main.innerHTML = `<div class="page-head"><h1>Asetukset</h1></div><div class="muted">Ladataan…</div>`;
  let s;
  try { s = await api("/api/settings"); } catch (e) { main.innerHTML = `<div class="banner error">${esc(e.message)}</div>`; return; }
  const onComputer = (STATUS?.device || "computer") === "computer";
  main.innerHTML = `
    <div class="page-head"><h1>Asetukset</h1></div>
    <form class="stack" id="settings">
      ${onComputer ? `<section class="card" id="pair-card">
        <h2>Puhelin</h2>
        <p class="muted small">Ohjaa nauhoitusta ja lue muistiinpanoja puhelimella, kun se on samassa Wi-Fi-verkossa.</p>
        ${toggle("phone_access", "Puhelinyhteys", "Salattu yhteys. Vain koodilla yhdistetyt laitteet pääsevät sisään.", s.phone_access)}
        <div id="pair"></div>
      </section>` : ""}

      ${window.MeetnotesAndroid ? `<section class="card">
        <h2>Android-sovellus</h2>
        <p class="muted small">Tämä puhelin on yhdistetty tietokoneeseesi. Yhteys on salattu, ja sovellus hyväksyy vain sen tietokoneen varmenteen.</p>
        <div class="actions"><button type="button" class="btn" id="unpair">Yhdistä toiseen tietokoneeseen</button></div>
      </section>` : ""}

      <section class="card" id="speech-card">
        <h2>Puheentunnistus</h2>
        <div id="speech-body" class="muted small">Haetaan tilaa…</div>
        <div class="actions"><button type="button" class="btn small" id="sp-test">Testaa nyt</button><button type="button" class="btn small" id="sp-log">Näytä loki</button><button type="button" class="btn small" id="sp-reset" hidden>Kokeile alkuperäistä asetusta</button></div>
        <div id="sp-log-box" hidden><pre class="log" id="sp-log-text"></pre><div class="actions"><button type="button" class="btn small" id="sp-copy">Kopioi loki</button></div></div>
        <p class="hint" style="margin-top:10px">Puheentunnistus toimii omassa erillisessä prosessissaan. Jos se kaatuu, sovellus vaihtaa itse varmempaan asetukseen ja jatkaa samasta kohdasta.</p>
      </section>

      <section class="card" id="update-card">
        <h2>Päivitykset</h2>
        <div id="update-body" class="muted small">Tarkistetaan…</div>
        <div class="actions"><button type="button" class="btn small" id="upd-check">Tarkista nyt</button><button type="button" class="btn small primary" id="upd-apply" hidden>Päivitä nyt</button></div>
        <p class="hint" style="margin-top:10px">Päivitys lataa vain ohjelman (noin 0,3 Mt). Python, puhemalli, asetukset, API-avain ja muistiinpanot säilyvät. Käynnissä oleva kokous viedään loppuun ennen uudelleenkäynnistystä.</p>
      </section>

      <section class="card">
        <h2>Claude API -avain</h2>
        <p class="muted small">Tarvitaan muistiinpanojen kirjoittamiseen. Tallennetaan Windowsin tunnistetietojen hallintaan, ei tiedostoon.</p>
        <label class="field"><span>${s.has_api_key ? "Avain on tallennettu. Vaihda uuteen:" : "API-avain"}</span>
          <input type="password" id="api_key" placeholder="sk-ant-…" autocomplete="off"></label>
      </section>

      <section class="card">
        <h2>Kieli ja tunnistus</h2>
        <div class="grid-2">
          <label class="field"><span>Puhuttu kieli</span><select id="language">${opts(LANGS, s.language)}</select></label>
          <label class="field"><span>Muistiinpanojen kieli</span><select id="notes_language">${opts(NOTE_LANGS, s.notes_language)}</select></label>
        </div>
        <label class="field"><span>Puheentunnistuksen malli</span><select id="whisper_model">${opts(MODELS, s.whisper_model)}</select>
          <span class="hint">Suuremmat mallit ovat tarkempia mutta hitaampia. Malli ladataan ensimmäisellä kerralla.</span></label>
        <label class="field"><span>Puhujien erottelu</span><select id="transcribe_mode">${opts([["mixed", "Nopea: kerran, puhuja äänenvoimakkuudesta"], ["separate", "Tarkka: oma ja muiden ääni erikseen (2× laskenta)"]], s.transcribe_mode)}</select>
          <span class="hint">Kuulokkeilla nopea tila erottaa puhujat yhtä hyvin.</span></label>
        <label class="field"><span>Nimet ja termit</span><input type="text" id="vocabulary" value="${esc(s.vocabulary.join(", "))}" placeholder="Sami, Kesko, Kubernetes">
          <span class="hint">Pilkuilla erotettuna. Auttaa kirjoittamaan nimet oikein.</span></label>
      </section>

      <section class="card">
        <h2>Kalenteri</h2>
        <p class="muted small">Liitä kalenterisi ICS-osoite, niin tulevat kokoukset näkyvät sovelluksessa, kokoukset nimetään kalenterin mukaan ja valmistelun saa yhdellä painalluksella. Vain luku, mitään ei kirjoiteta kalenteriin.</p>
        <label class="field"><span>ICS-osoitteet, yksi per rivi</span><textarea id="calendar_urls" placeholder="https://calendar.google.com/calendar/ical/…/basic.ics">${esc((s.calendar_urls || []).join("\n"))}</textarea>
          <span class="hint">Google: Kalenterin asetukset → Integroi kalenteri → Salainen osoite iCal-muodossa. Outlook: Asetukset → Jaetut kalenterit → Julkaise kalenteri → ICS.</span></label>
      </section>

      <section class="card">
        <h2>Muistiinpanojen tyyppi</h2>
        <label class="field" style="margin-top:4px"><span>Oletusmalli</span><select id="notes_template">${opts(TEMPLATES.map((t) => [t.id, t.name]), s.notes_template)}</select>
          <span class="hint">Automaattinen tunnistaa kokouksen tyypin. Myyntikokoukseen tulee esimerkiksi osiot Asiakkaan tarpeet, Vastaväitteet ja Kaupan tilanne. Tyypin voi vaihtaa myös kokouksen aikana.</span></label>
        <label class="field"><span>Avainsanat, jotka merkitään heti</span><input type="text" id="watch_words" value="${esc((s.watch_words || []).join(", "))}" placeholder="kilpailija, alennus, irtisanominen">
          <span class="hint">Kun joku sanoo näistä jonkin, se näkyy heti kotisivun syötteessä. Ei maksa mitään.</span></label>
        <label class="field"><span>Webhook-osoite</span><input type="text" id="webhook_url" value="${esc(s.webhook_url || "")}" placeholder="https://hooks.zapier.com/…">
          <span class="hint">Muistiinpanot lähetetään JSON-muodossa tähän osoitteeseen jokaisen kokouksen jälkeen. Sopii Zapieriin, n8n:ään tai omaan skriptiin, joka vie ne esimerkiksi CRM:ään tai Slackiin.</span></label>
      </section>

      <section class="card">
        <h2>Avustaja kokouksen aikana</h2>
        <p class="muted small">Seuraa keskustelua Clauden avulla ja ehdottaa, mitä ottaa esiin. Jokainen päivitys on yksi API-kutsu, joten tunnin kokous maksaa suunnilleen 0,5–1 €.</p>
        ${toggle("assistant_live", "Avustaja päällä kokouksissa", "Käynnistyy itse jokaisessa kokouksessa. Voit kytkeä sen myös kokouksen aikana kotisivulta.", s.assistant_live)}
        <div class="grid-2">
          <label class="field"><span>Tyyli</span><select id="assistant_style">${opts([["quiet", "Hiljainen: vastaa ja merkitsee päätökset"], ["balanced", "Tasapainoinen, suositus"], ["active", "Aktiivinen: paljon ehdotuksia"]], s.assistant_style)}</select></label>
          <label class="field"><span>Päivitysväli</span><select id="assistant_interval">${opts([["30", "30 s"], ["45", "45 s, suositus"], ["60", "60 s"], ["90", "90 s"], ["120", "2 min"]], String(s.assistant_interval))}</select></label>
        </div>
        <label class="field"><span>Malli kokouksen aikana</span><select id="assistant_model">${opts([["claude-opus-5-5", "Claude Opus 5.5, tarkin"], ["claude-sonnet-5-5", "Claude Sonnet 5.5, nopeampi ja halvempi"]], s.assistant_model)}</select>
          <span class="hint">Valmistelu ja muistiinpanot käyttävät aina Opus 5.5:tä.</span></label>
      </section>

      <section class="card">
        <h2>Näytön tallennus</h2>
        <p class="muted small">Tallentaa kokouksen ajalta videon näytöstäsi kokouksen äänellä. Muut osallistujat eivät näe sitä, eikä näytön reunaan tule merkkiä.</p>
        ${toggle("record_screen", "Tallenna myös näyttö", "Video tallentuu kokouksen kansioon (screen.mp4). Noin 0,2–0,5 Gt tunnissa.", s.record_screen)}
        <div class="grid-2">
          <label class="field"><span>Näyttö</span><select id="screen_monitor">${opts([["1", "Päänäyttö"], ["2", "Toinen näyttö"], ["0", "Kaikki näytöt"]], String(s.screen_monitor))}</select></label>
          <label class="field"><span>Kuvia sekunnissa</span><select id="screen_fps">${opts([["2", "2, pienin tiedosto"], ["5", "5, suositus"], ["10", "10, sujuvin"]], String(s.screen_fps))}</select></label>
        </div>
        <p class="hint" style="margin-top:12px">Tauon aikana video on musta, ja turvasanan jälkeen tallennus loppuu kuten äänikin.</p>
      </section>

      <section class="card">
        <h2>Turvasana</h2>
        <p class="muted small">Kun sanot tämän kokouksen aikana, kuuntelu loppuu siihen kokoukseen. Vain oma mikrofonisi voi lopettaa sen.</p>
        <div class="grid-2">
          <label class="field"><span>Sana</span><input type="text" id="safe_word" value="${esc(s.safe_word)}" placeholder="omerta"></label>
          <label class="field"><span>Mitä tapahtuu</span><select id="safe_word_action">${opts([["stop", "Säilytä sitä edeltävä osa"], ["discard", "Poista koko kokous"]], s.safe_word_action)}</select></label>
        </div>
      </section>

      <section class="card">
        <h2>Yksityisyys</h2>
        ${toggle("keep_audio", "Säilytä äänitiedosto", "Muuten vain teksti ja muistiinpanot jäävät talteen.", s.keep_audio)}
        ${onComputer ? toggle("autostart", "Käynnisty kirjautuessa", "Taustapalvelu käynnistyy itse, kun kirjaudut Windowsiin.", s.autostart) : ""}
        ${onComputer ? toggle("tray_icon", "Kuvake ilmaisinalueella", "Pieni kuvake tehtäväpalkin oikeassa reunassa, kun ikkuna on auki.", s.tray_icon) : ""}
        <label class="field"><span>Älä koskaan nauhoita kokouksia, joiden nimessä on</span>
          <input type="text" id="ignore_titles" value="${esc(s.ignore_titles.join(", "))}" placeholder="1:1, kehityskeskustelu"></label>
        <p class="hint" style="margin-top:12px">Muistiinpanot: ${esc(s.notes_dir)}</p>
      </section>

      <div class="save-bar"><button class="btn primary" id="save">Tallenna</button></div>
    </form>`;

  let pairTab = "android";
  async function loadPair() {
    const el = $("#pair"); if (!el) return;
    if (!$("#phone_access").checked) { el.innerHTML = ""; return; }
    try {
      const p = await api("/api/pair");
      if (!p.url) { el.innerHTML = `<p class="banner error small">${esc(p.error || "Tietokone ei ole Wi-Fi-verkossa.")}</p>`; return; }
      const code = p.code ? p.code.slice(0, 3) + " " + p.code.slice(3) : "";
      const mins = Math.max(1, Math.round(p.code_expires_in / 60));
      el.innerHTML = `
        <div class="tabs" role="tablist" style="margin-top:14px">
          <button type="button" role="tab" data-pair="android" aria-selected="${pairTab === "android"}">Android-sovellus</button>
          <button type="button" role="tab" data-pair="browser" aria-selected="${pairTab === "browser"}">Selain (iPhone)</button>
        </div>
        ${pairTab === "android" ? `
        <div class="pair-code">
          <div><div class="label">Osoite</div><div class="value">${esc(p.address || p.ip)}</div></div>
          <div><div class="label">Koodi</div><div class="value code">${esc(code)}</div></div>
        </div>
        <ol class="steps">
          <li>Jos Windows kysyy palomuurilupaa, valitse <b>Yksityiset verkot</b> ja Salli.</li>
          <li>Avaa puhelimessa Meetnotes-sovellus ja syötä osoite ja koodi.</li>
          <li>Koodi toimii kerran ja vaihtuu ${mins} min kuluttua.</li>
        </ol>
        <p class="hint">Varmenteen sormenjälki: <span class="url">${esc((p.fingerprint || "").slice(0, 16))}…</span></p>`
        : `
        <div class="pair"><div class="qr" aria-label="QR-koodi puhelimelle">${p.qr_svg}</div>
          <div><ol class="steps">
            <li>Jos Windows kysyy palomuurilupaa, valitse <b>Yksityiset verkot</b> ja Salli.</li>
            <li>Skannaa koodi puhelimen kameralla.</li>
            <li>Selain varoittaa yhteydestä kerran, koska varmenne on tämän tietokoneen oma. Valitse “Lisätiedot” → “Jatka”.</li>
            <li>Lisää sivu aloitusnäytölle: iPhone: Jaa → Lisää Koti-valikkoon.</li>
          </ol><p class="url">${esc(p.url)}</p></div></div>`}`;
      el.querySelectorAll("[data-pair]").forEach((b) => (b.onclick = () => { pairTab = b.dataset.pair; loadPair(); }));
    } catch (e) { el.innerHTML = `<p class="banner error small">${esc(e.message)}</p>`; }
  }
  clearInterval(viewSettings.pairTimer);
  viewSettings.pairTimer = setInterval(() => {  // keep the code current while the page is open
    if (!document.getElementById("pair")) return clearInterval(viewSettings.pairTimer);
    if (pairTab === "android" && $("#phone_access")?.checked && !document.hidden) loadPair();
  }, 30000);
  if ($("#phone_access")) {
    loadPair();
    $("#phone_access").onchange = async (e) => {
      try { await api("/api/settings", { phone_access: e.target.checked }); toast(e.target.checked ? "Puhelinyhteys päällä" : "Puhelinyhteys pois"); }
      catch (err) { toast("Ei onnistunut: " + err.message); }
      loadPair();
    };
  }

  $("#unpair") && ($("#unpair").onclick = () => window.MeetnotesAndroid.unpair());
  const android = window.MeetnotesAndroid;
  const drawUpdate = (u) => {
    const body = $("#update-body"); if (!body) return;
    const apkCode = android && android.versionCode ? Number(android.versionCode()) : 0;
    const apkNew = apkCode && u.android && u.android.version_code > apkCode && u.android.url;
    body.innerHTML = !u.configured ? `Asennettu versio <b>${esc(u.current)}</b>. Päivitysosoitetta ei ole asetettu, joten uusia versioita ei etsitä.`
      : u.progress && u.progress !== "failed" && u.progress !== "done" ? `<b>${esc({ checking: "Tarkistetaan…", downloading: "Ladataan…", installing: "Asennetaan…", restarting: "Käynnistetään uudelleen…" }[u.progress] || u.progress)}</b>`
      : `Asennettu <b>${esc(u.current)}</b>${u.latest ? `, uusin <b>${esc(u.latest)}</b>` : ""}${u.checked ? ` <span class="faint">(tarkistettu ${esc(fmtDate(u.checked))})</span>` : ""}.
        ${u.available ? `<div style="margin-top:6px">Uusi versio saatavilla${u.notes ? ": " + esc(u.notes) : "."}</div>` : u.latest ? `<div style="margin-top:6px">Ajan tasalla.</div>` : ""}
        ${u.error ? `<div class="banner error small" style="margin-top:8px">Tarkistus ei onnistunut: ${esc(u.error)}</div>` : ""}
        ${u.progress === "failed" ? `<div class="banner error small" style="margin-top:8px">Päivitys epäonnistui: ${esc(u.progress_error)}</div>` : ""}
        ${apkNew ? `<div class="banner" style="margin-top:8px"><span>Android-sovelluksesta on uusi versio ${esc(u.android.version)}.</span><button type="button" class="btn small" id="apk-get">Lataa</button></div>` : ""}`;
    $("#upd-apply").hidden = !u.available || (u.progress && !["failed", "done", ""].includes(u.progress));
    $("#apk-get") && ($("#apk-get").onclick = () => android.openUrl(u.android.url));
  };
  const loadUpdate = async (force) => {
    try { drawUpdate(await api(force ? "/api/update/check" : "/api/update", force ? {} : undefined)); } catch (e) { $("#update-body").textContent = e.message; }
  };
  loadUpdate(false);
  $("#upd-check").onclick = () => loadUpdate(true);
  const DEV = { cpu: "suoritin", cuda: "näytönohjain", auto: "automaattinen" };
  const drawSpeech = (d) => {
    const sp = d.speech || {}, setup = d.setup || {};
    const now = speechText(sp);
    const lines = [];
    if (now) lines.push(`<p><strong>${esc(now)}</strong></p>`);
    else if (sp.phase === "failed") lines.push(`<div class="banner error small">Puheentunnistus ei toimi: ${esc(sp.error || "")}</div>`);
    else if (d.test && d.test.ok) lines.push(`<p>Toimii. Testattu ${esc(fmtDate(d.test.at))}, malli ${esc(d.test.model)} (${esc(DEV[d.test.device] || d.test.device)}), ${esc(String(d.test.seconds))} s.</p>`);
    else if (d.test && d.test.error) lines.push(`<div class="banner error small">Viimeisin testi epäonnistui: ${esc(d.test.error)}</div>`);
    else lines.push(`<p>Ei vielä testattu tällä koneella.</p>`);
    if (setup.fallback) lines.push(`<p>Käytössä on varmempi asetus: malli ${esc(setup.model)}, ${esc(DEV[setup.device] || setup.device)}. Syy: ${esc(setup.reason)}</p>`);
    $("#speech-body").innerHTML = lines.join("");
    $("#sp-reset").hidden = !setup.fallback;
    $("#sp-test").disabled = !!now;
  };
  let speechPoll = null;
  const loadSpeech = async () => {
    try { const d = await api("/api/speech"); drawSpeech(d); return d; } catch (e) { $("#speech-body").textContent = e.message; }
  };
  loadSpeech();
  const watchSpeech = () => {
    clearInterval(speechPoll);
    speechPoll = setInterval(async () => {
      if (!$("#speech-card")) return clearInterval(speechPoll);
      const d = await loadSpeech();
      if (d && !d.testing) { clearInterval(speechPoll); toast(d.test && d.test.ok ? "Puheentunnistus toimii" : "Testi epäonnistui"); }
    }, 1500);
  };
  $("#sp-test").onclick = async () => {
    try { await api("/api/speech/test", {}); $("#speech-body").innerHTML = "<p><strong>Testataan puheentunnistusta…</strong></p>"; watchSpeech(); }
    catch (e) { toast("Ei onnistunut: " + e.message); }
  };
  $("#sp-reset").onclick = async () => {
    try { await api("/api/speech/reset", {}); toast("Testataan alkuperäisellä asetuksella"); watchSpeech(); } catch (e) { toast(e.message); }
  };
  $("#sp-log").onclick = async () => {
    try {
      const d = await api("/api/diagnostics");
      $("#sp-log-text").textContent = d.text; $("#sp-log-box").hidden = false;
      $("#sp-log-text").scrollTop = $("#sp-log-text").scrollHeight;
    } catch (e) { toast(e.message); }
  };
  $("#sp-copy").onclick = async () => {
    try { await navigator.clipboard.writeText($("#sp-log-text").textContent); toast("Kopioitu"); }
    catch { const r = document.createRange(); r.selectNodeContents($("#sp-log-text")); getSelection().removeAllRanges(); getSelection().addRange(r); toast("Valittu, paina Ctrl+C"); }
  };
  if (location.hash === "#/settings/speech") setTimeout(() => $("#speech-card").scrollIntoView({ behavior: "smooth" }), 50);
  $("#upd-apply").onclick = async () => {
    $("#upd-apply").disabled = true;
    try { await api("/api/update/apply", {}); toast("Päivitys alkoi"); } catch (e) { toast("Ei onnistunut: " + e.message); $("#upd-apply").disabled = false; return; }
    const poll = setInterval(async () => {
      let u;
      try { u = await api("/api/update"); } catch { return; }  // the service restarts: wait for it to come back
      drawUpdate(u);
      if (u.progress === "failed") { clearInterval(poll); $("#upd-apply").disabled = false; }
      if (u.progress === "restarting") { toast("Asennettu, odotetaan uudelleenkäynnistystä"); }
      if (u.current && u.installed && u.current === u.installed) { clearInterval(poll); toast("Päivitetty versioon " + u.current); setTimeout(() => location.reload(), 1200); }
    }, 2500);
  };
  $("#settings").onsubmit = async (e) => {
    e.preventDefault();
    const val = (id) => $("#" + id)?.value;
    const chk = (id) => $("#" + id)?.checked;
    const data = {
      language: val("language"), notes_language: val("notes_language"), whisper_model: val("whisper_model"),
      transcribe_mode: val("transcribe_mode"),
      vocabulary: val("vocabulary"), safe_word: val("safe_word"), safe_word_action: val("safe_word_action"),
      keep_audio: chk("keep_audio"), ignore_titles: val("ignore_titles"),
      record_screen: chk("record_screen"), screen_monitor: Number(val("screen_monitor")), screen_fps: Number(val("screen_fps")),
      assistant_live: chk("assistant_live"), assistant_style: val("assistant_style"), assistant_interval: Number(val("assistant_interval")),
      assistant_model: val("assistant_model"),
      calendar_urls: val("calendar_urls"), notes_template: val("notes_template"), watch_words: val("watch_words"), webhook_url: val("webhook_url"),
    };
    if ($("#autostart")) data.autostart = chk("autostart");
    if ($("#tray_icon")) data.tray_icon = chk("tray_icon");
    const key = val("api_key"); if (key) data.api_key = key;
    const btn = $("#save"); btn.disabled = true;
    try { await api("/api/settings", data); toast("Tallennettu"); $("#api_key").value = ""; refreshStatus(); }
    catch (err) { toast("Ei tallennettu: " + err.message); }
    btn.disabled = false;
  };
}

// ---------------------------------------------------------------- go
route();
if (TOKEN) { refreshStatus(); startPolling(); }

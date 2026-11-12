"""Tasks across meetings: action items and your follow-ups from every notes.json,
with done/open state kept in <notes>/_tasks.json."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .library import meetings


def task_id(meeting_id: str, text: str) -> str:
    return hashlib.sha1(f"{meeting_id}|{text.strip().lower()}".encode()).hexdigest()[:12]


def _state(root: Path) -> dict:
    try:
        return json.loads((root / "_tasks.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(root: Path, state: dict) -> None:
    from .config import write_json
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "_tasks.json", state)


def all_tasks(root: Path, limit_meetings: int = 60) -> list[dict]:
    """Newest meetings first. `mine` = assigned to the user (follow-ups, or owner 'Minä'/'Me')."""
    state = _state(root)
    out = []
    for m in meetings(root)[:limit_meetings]:
        f = m.path / "notes.json"
        if not f.exists():
            continue
        try:
            notes = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        date = (m.meta.get("started_at") or "")[:10]
        for item in notes.get("action_items") or []:
            text = str(item.get("task", "")).strip()
            if not text:
                continue
            owner = str(item.get("owner", "")).strip()
            tid = task_id(m.path.name, text)
            out.append({"id": tid, "text": text, "owner": owner, "due": str(item.get("due", "")),
                        "meeting": m.title, "meeting_id": m.path.name, "date": date,
                        "mine": owner.lower() in ("minä", "me", "i", "myself", "jag"), "done": state.get(tid, False)})
        for text in notes.get("my_followups") or []:
            text = str(text).strip()
            if not text:
                continue
            tid = task_id(m.path.name, text)
            if any(t["id"] == tid for t in out):
                continue
            out.append({"id": tid, "text": text, "owner": "Minä", "due": "", "meeting": m.title,
                        "meeting_id": m.path.name, "date": date, "mine": True, "done": state.get(tid, False)})
    return out


def set_done(root: Path, tid: str, done: bool) -> None:
    state = _state(root)
    if done:
        state[tid] = True
    else:
        state.pop(tid, None)
    _save_state(root, state)


def open_tasks_text(root: Path, keywords: list[str] | None = None, limit: int = 30) -> str:
    """Open tasks for a briefing: all of the user's open tasks, plus others'
    tasks that mention a keyword. One line each, for Claude."""
    keys = [k.lower() for k in (keywords or []) if len(k) >= 3]
    lines = []
    for t in all_tasks(root):
        if t["done"]:
            continue
        hay = (t["text"] + " " + t["owner"] + " " + t["meeting"]).lower()
        if t["mine"] or any(k in hay for k in keys):
            lines.append(f"- {t['text']} (owner: {t['owner'] or 'unassigned'}, due: {t['due'] or 'not set'}, "
                         f"from: {t['meeting']} {t['date']})")
        if len(lines) >= limit:
            break
    return "\n".join(lines)

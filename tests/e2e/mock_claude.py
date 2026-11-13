"""A local stand-in for the Anthropic Messages API (streaming SSE).
Answers by request type and logs every request to requests.jsonl."""
import json, re, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("requests.jsonl")


def text_of(content):
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))


def respond(req):
    fmt = ((req.get("output_config") or {}).get("format") or {}).get("schema") or {}
    props = fmt.get("properties", {})
    system = text_of(req.get("system", ""))
    msgs = req.get("messages", [])
    last = text_of(msgs[-1]["content"]) if msgs else ""
    alltext = " ".join(text_of(m["content"]) for m in msgs)
    if "chapters" in props:  # meeting notes
        lines = re.findall(r"\[(\d\d:\d\d:\d\d)\] (?:(Me|Others): )?(.+)", alltext)
        said = [t for _, _, t in lines if not t.startswith("(")]
        first = lines[0][0] if lines else "00:00:00"
        mid = lines[len(lines) // 2][0] if lines else "00:00:00"
        mine = re.search(r"<my_notes>\n(.*?)\n</my_notes>", alltext, re.S)
        prev = "<previous_meeting" in alltext
        tmpl_sales = "sales or customer meeting" in alltext
        return json.dumps({
            "language": "fi", "title": "Kontio: hinta ja toimitus" if "Kontio" in alltext else "Testikokous",
            "summary": "Sovittiin raportin toimituksesta. " + (said[0] if said else "") + (f" Omista muistiinpanoista: {mine.group(1).splitlines()[0]}" if mine else ""),
            "key_points": said[:3] or ["(ei puhetta)"], "decisions": ["Raportti lähetetään perjantaina"],
            "action_items": [{"task": "Lähetä raportti", "owner": "Minä", "due": "perjantai"},
                             {"task": "Kokoa tulokset", "owner": "Sami", "due": "maanantai"}],
            "open_questions": ["Kuka vastaa julkaisusta?"], "my_followups": ["Varaa maanantain palaveri"],
            "participants": ["Jari Kontio"] if "Jari Kontio" in alltext else [],
            "chapters": [{"start": first, "title": "Raportti"}, {"start": mid, "title": "Tulokset"}],
            "followups_from_last_time": [{"item": "Lähetä raportti", "status": "open", "note": "ei mainittu"}] if prev else [],
            "sections": [{"title": "Asiakkaan tarpeet", "items": ["Toimitus tammikuussa"]}] if tmpl_sales else [],
        }, ensure_ascii=False)
    if "lines" in props and "translate meeting transcript lines" in system:  # transcript translation
        items = json.loads(last)["lines"]
        return json.dumps({"lines": [{"id": x["id"], "text": "EN: " + x["text"]} for x in items]}, ensure_ascii=False)
    if "Translate these meeting notes" in system:
        return "\n".join(("# EN " + l[2:]) if l.startswith("# ") else l for l in last.splitlines()).replace("Yhteenveto", "Summary")
    if "nudges" in props:  # live assistant
        agenda = re.findall(r"^\d+\. (.+)$", system, re.M)
        nudges = []
        if "Kilpailija" in last or "kilpailija" in last:
            nudges.append({"kind": "heads_up", "text": "Kilpailija mainittiin: älä laske hintaa ilman vastinetta.", "why": "Materiaalit"})
        if "perjantaina" in last:
            nudges.append({"kind": "action", "text": "Sinulle: raportti perjantaina.", "why": "juuri sanottu"})
        q = re.search(r"<question>\n(.*?)\n</question>", last, re.S)
        return json.dumps({"agenda": [{"item": a, "status": "in_progress" if i == 0 else "not_started"} for i, a in enumerate(agenda)],
                           "nudges": nudges, "answer": f"Vastaus: {q.group(1)}" if q else ""}, ensure_ascii=False)
    if "talking_points" in props:  # briefing
        return json.dumps({"language": "fi", "summary": "Kontiolle on lähetetty tarjous.", "history": ["20.10. tarjous 48 000 €"],
                           "open_items": ["Maksuehdot"], "talking_points": ["Vahvista hinta"], "questions": ["Kuka päättää?"],
                           "risks": []}, ensure_ascii=False)
    if "Question:" in last:  # ask across meetings
        return "Kokouksissa sovittiin, että lähetät raportin perjantaina."
    return "Hinnasta on sovittu 48 000 € (Kontio: tarjous, 20.10.)."


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        with open(LOG, "a") as f:
            f.write(json.dumps({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items() if k.lower() in ("anthropic-beta", "x-api-key")},
                                "body": body}, ensure_ascii=False) + "\n")
        if not self.path.startswith("/v1/messages"):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        text = respond(body)
        events = [
            ("message_start", {"type": "message_start", "message": {"id": "msg_e2e", "type": "message", "role": "assistant",
             "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None,
             "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ]
        for i in range(0, len(text), 40):
            events.append(("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i:i + 40]}}))
        events += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                   ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 120}}),
                   ("message_stop", {"type": "message_stop"})]
        payload = "".join(f"event: {e}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for e, d in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()

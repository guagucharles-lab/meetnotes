"""Meeting types: extra instructions and sections for the notes writer.

The standard notes (summary, key points, decisions, action items, follow-ups,
open questions) are always written. A template adds what that kind of meeting
needs, as extra sections. Users can add their own in config.toml:

    [[templates]]
    id = "retro"
    name = "Retrospektiivi"
    instructions = "Focus on what went well and what to change."
    sections = ["Mikä meni hyvin", "Mitä muutetaan", "Kokeilut ensi sprintille"]
"""
from __future__ import annotations

BUILTIN = {
    "auto": {"name": "Automaattinen", "instructions": "", "sections": []},
    "general": {"name": "Yleinen kokous", "instructions": "", "sections": []},
    "sales": {
        "name": "Myynti / asiakas",
        "instructions": "This is a sales or customer meeting. Capture the customer's needs and objections, "
                        "pricing and commercial terms discussed, competitors mentioned, and where the deal stands.",
        "sections": ["Asiakkaan tarpeet", "Vastaväitteet ja huolet", "Hinta ja ehdot", "Kilpailijat", "Kaupan tilanne ja seuraava askel"],
    },
    "one_on_one": {
        "name": "1:1",
        "instructions": "This is a one-on-one. Capture how the other person is doing, feedback given in both "
                        "directions, goals and growth topics, and anything to follow up next time. Keep it humane and specific.",
        "sections": ["Kuulumiset", "Palaute", "Tavoitteet ja kehittyminen", "Seuraavalla kerralla"],
    },
    "standup": {
        "name": "Standup / tilannekatsaus",
        "instructions": "This is a status meeting. Capture progress per person or workstream, blockers, and risks to the schedule.",
        "sections": ["Edistyminen", "Esteet", "Riskit aikataululle"],
    },
    "interview": {
        "name": "Haastattelu",
        "instructions": "This is an interview. Capture the candidate's background, concrete examples they gave, "
                        "strengths and concerns, questions they asked, and the suggested next step. Stick to what was said.",
        "sections": ["Tausta", "Konkreettiset esimerkit", "Vahvuudet", "Huolet", "Kandidaatin kysymykset", "Seuraava askel"],
    },
    "project": {
        "name": "Projektikokous",
        "instructions": "This is a project meeting. Capture scope changes, schedule and budget status, risks and dependencies.",
        "sections": ["Laajuus ja muutokset", "Aikataulu ja budjetti", "Riskit ja riippuvuudet"],
    },
    "board": {
        "name": "Hallitus / johtoryhmä",
        "instructions": "This is a governance meeting. Record formal decisions precisely, who proposed and who objected, "
                        "and items deferred to a later meeting.",
        "sections": ["Viralliset päätökset", "Siirretyt asiat"],
    },
}

AUTO_HINT = ("Choose the kind of meeting from these if it clearly applies and add its sections; otherwise add no extra sections: "
             + "; ".join(f"{t['name']}: {', '.join(t['sections'])}" for k, t in BUILTIN.items() if t["sections"]))


def all_templates(custom: list[dict] | None = None) -> dict[str, dict]:
    out = dict(BUILTIN)
    for t in custom or []:
        tid = str(t.get("id") or "").strip()
        if tid:
            out[tid] = {"name": str(t.get("name") or tid), "instructions": str(t.get("instructions") or ""),
                        "sections": [str(s) for s in t.get("sections") or []]}
    return out


def instructions_for(template_id: str, custom: list[dict] | None = None) -> str:
    """Text appended to the notes prompt for this template."""
    t = all_templates(custom).get(template_id or "auto") or BUILTIN["auto"]
    if template_id in ("", "auto"):
        return AUTO_HINT
    parts = []
    if t["instructions"]:
        parts.append(t["instructions"])
    if t["sections"]:
        parts.append("Add these sections, in this order, with the given titles: " + ", ".join(t["sections"])
                     + ". Leave a section's items empty if nothing was said about it.")
    return " ".join(parts)
